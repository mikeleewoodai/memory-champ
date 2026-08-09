"""Hybrid retrieval: RRF over keyword and vector, then recency and importance.

    score = w_rel·relevance + w_rec·recency + w_imp·importance
    relevance = RRF(vector_rank, bm25_rank)
    recency   = exp(-ln2 · hours_since_last_access / half_life)

Reciprocal-rank fusion rather than normalising cosine against BM25: the two are
not on comparable scales, and any normalisation constant drifts with corpus size.
RRF needs one constant and is stable.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .approval import utcnow
from .config import RetrievalPolicy
from .embedding import Embedder, TokenCounter


@dataclass
class Scored:
    record: dict
    total: float = 0.0
    relevance: float = 0.0
    recency: float = 0.0
    importance: float = 0.0
    vector_similarity: float | None = None
    keyword_rank: int | None = None
    in_context_block: bool = False
    rank: int = 0


@dataclass
class RecallResult:
    scored: list[Scored] = field(default_factory=list)
    context_block: str = ""
    token_count: int = 0
    truncated: bool = False
    omitted_count: int = 0
    excluded_sensitive_count: int = 0
    contradictions: list[dict] = field(default_factory=list)
    degraded: dict | None = None


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def recency_score(last_accessed: str | None, half_life_hours: float, now: datetime) -> float:
    dt = _parse(last_accessed)
    if dt is None or half_life_hours <= 0:
        return 0.0
    hours = max((now - dt).total_seconds() / 3600.0, 0.0)
    return math.exp(-math.log(2) * hours / half_life_hours)


class Retriever:
    def __init__(self, store, embedder: Embedder, policy: RetrievalPolicy,
                 tokens: TokenCounter | None = None):
        self.store = store
        self.embedder = embedder
        self.policy = policy
        self.tokens = tokens or TokenCounter()

    def recall(self, *, scope: str, query: str, types: list[str], strategy: str, k: int,
               max_tokens: int, filters: dict, include_sensitive: bool,
               include_superseded: bool, weights=None, now: datetime | None = None) -> RecallResult:
        now = now or utcnow()
        w = weights or self.policy.weights
        pool = max(self.policy.candidate_pool, k)
        degraded: dict | None = None

        if strategy == "recent":
            ranked = {rid: (None, rank) for rid, rank in self.store.recent(scope, types, pool, filters)}
        else:
            kw: list[tuple[str, int]] = []
            vec: list[tuple[str, int, float]] = []
            if strategy in ("hybrid", "keyword"):
                kw = self.store.search_keyword(scope, query, types, pool, filters, include_superseded)
            if strategy in ("hybrid", "semantic"):
                if self.store.vector_ok:
                    qv = self.embedder.embed([query])[0]
                    vec = self.store.search_vector(scope, qv, types, pool, filters, include_superseded)
                else:
                    # A thin result here means "could not look properly", not
                    # "nothing is known". Saying so is the whole point of NF6.
                    degraded = {"reason": "vector_unavailable",
                                "detail": self.store.vector_error or "sqlite-vec not loaded"}
                    if strategy == "semantic":
                        kw = self.store.search_keyword(scope, query, types, pool, filters, include_superseded)
            ranked = {}
            for rid, rank in kw:
                ranked.setdefault(rid, [None, None])
                ranked[rid][1] = rank
            for rid, rank, sim in vec:
                ranked.setdefault(rid, [None, None])
                ranked[rid][0] = (rank, sim)
            ranked = {rid: (v[0], v[1]) for rid, v in ranked.items()}

        scored: list[Scored] = []
        rrf_k = self.policy.rrf_k
        for rid, (vinfo, krank) in ranked.items():
            rec = self.store.get_record(rid)
            if rec is None:
                continue
            relevance = 0.0
            if vinfo:
                relevance += 1.0 / (rrf_k + vinfo[0])
            if krank:
                relevance += 1.0 / (rrf_k + krank)
            s = Scored(
                record=rec,
                relevance=relevance,
                recency=recency_score(rec.get("last_accessed_at") or rec["created_at"],
                                      self.policy.recency_half_life_hours, now),
                importance=float(rec.get("importance") or 0.0),
                vector_similarity=vinfo[1] if vinfo else None,
                keyword_rank=krank,
            )
            s.total = w.relevance * s.relevance + w.recency * s.recency + w.importance * s.importance
            s.total *= self._procedure_penalty(rec)
            scored.append(s)

        scored.sort(key=lambda s: s.total, reverse=True)
        scored = scored[:k]
        for i, s in enumerate(scored, 1):
            s.rank = i

        block, used, truncated, excluded = self._context_block(scored, max_tokens, include_sensitive)
        result = RecallResult(
            scored=scored, context_block=block, token_count=used, truncated=truncated,
            omitted_count=sum(1 for s in scored if not s.in_context_block) - excluded,
            excluded_sensitive_count=excluded,
            contradictions=self._contradictions(scored), degraded=degraded,
        )
        self.store.touch([s.record["id"] for s in scored])
        return result

    def _procedure_penalty(self, rec: dict) -> float:
        """Demote a procedure that keeps being used and keeps failing, before a
        human gets round to retiring it."""
        if rec["type"] != "procedural":
            return 1.0
        inv = rec.get("invocations") or 0
        if inv < self.policy.procedure_min_invocations_to_judge:
            return 1.0
        rate = (rec.get("successes") or 0) / inv
        return 0.5 if rate < self.policy.procedure_min_success_rate else 1.0

    def _context_block(self, scored: list[Scored], max_tokens: int,
                       include_sensitive: bool) -> tuple[str, int, bool, int]:
        """Build the injectable block under a hard token ceiling.

        Measured, not estimated. Nothing is emitted that would push the count
        over budget, which is the guarantee that makes recall safe to call on
        every iteration of a loop.
        """
        header = "## Recalled memory\n"
        parts: list[str] = []
        used = self.tokens.count(header)
        truncated = False
        excluded = 0

        for s in scored:
            rec = s.record
            if (not include_sensitive and self.policy.redact_sensitive_in_context_block
                    and (rec.get("sensitivity") or "none") != "none"):
                excluded += 1
                continue
            piece = _render(rec)
            cost = self.tokens.count(piece)
            if used + cost > max_tokens:
                truncated = True
                continue  # a later, smaller record may still fit
            parts.append(piece)
            used += cost
            s.in_context_block = True

        if not parts:
            # Empty string, never a sentence. A model handed "no memories found"
            # treats it as a retrieved fact and reasons from it.
            return "", 0, truncated, excluded
        return header + "\n".join(parts), used, truncated, excluded

    def _contradictions(self, scored: list[Scored]) -> list[dict]:
        by_triple: dict[tuple, list[dict]] = {}
        out = []
        for s in scored:
            rec = s.record
            if rec["type"] != "semantic" or not rec.get("subject"):
                continue
            key = (rec["subject"], rec["predicate"])
            for other in by_triple.get(key, []):
                if other["object"] != rec.get("object"):
                    out.append({"a": other["id"], "b": rec["id"],
                                "note": f"{key[0]} {key[1]}: "
                                        f"{other['object']!r} vs {rec.get('object')!r}"})
            by_triple.setdefault(key, []).append(rec)
        return out


def _render(rec: dict) -> str:
    t = rec["type"]
    if t == "semantic":
        conf = rec.get("confidence")
        return f"[fact, confidence {conf:.2g}] {rec['content']}" if conf is not None \
            else f"[fact] {rec['content']}"
    if t == "procedural":
        inv, suc = rec.get("invocations") or 0, rec.get("successes") or 0
        steps = json.loads(rec.get("steps") or "[]")
        lines = "\n".join(f"  {s['n']}. {s['instruction']}" for s in steps)
        head = f"[procedure, {suc}/{inv} successful] When {rec.get('trigger_text') or rec['content']}:"
        return f"{head}\n{lines}" if lines else head
    outcome = rec.get("outcome") or "unknown"
    return f"[episode, {outcome}] {rec['content']}"
