"""The nine tools.

Plain Python taking and returning dicts that match contracts/mcp-tools.json.
server.py is a thin adapter over this, so the tools are testable without MCP and
the daemon can call the same code paths a host does.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from . import approval as A
from .approval import rfc3339, utcnow
from .config import Policy
from .embedding import Embedder, TokenCounter, build_embedder
from .errors import (
    ApprovalCandidateChanged,
    ApprovalChallengeInvalid,
    ApprovalKeyUnknown,
    ApprovalSignatureInvalid,
    ApprovalSignatureRequired,
    BlastRadiusExceeded,
    ConfirmRequired,
    CycleNotFound,
    MemoryAgentError,
    ProceduralWriteRequiresProposal,
    ScopeRequired,
    WriteRateExceeded,
)
from .retrieval import Retriever, Scored
from .store import Store

SCHEMA_VERSION = "1.0"


def loop_signature(goal: str, action: str | None = None) -> str:
    norm = " ".join(goal.lower().split())
    return hashlib.blake2b(f"{norm}|{action or ''}".encode(), digest_size=8).hexdigest()


class MemoryService:
    def __init__(self, policy: Policy | None = None, store: Store | None = None,
                 embedder: Embedder | None = None):
        self.policy = policy or Policy()
        self.embedder = embedder or build_embedder(
            self.policy.embedding_provider, self.policy.embedding_model, self.policy.embedding_dimensions)
        self.store = store or Store(
            self.policy.db_path, dimensions=self.embedder.dimensions,
            busy_timeout_ms=self.policy.busy_timeout_ms,
            require_vector=self.policy.require_vector_extension)
        self.tokens = TokenCounter()
        self.retriever = Retriever(self.store, self.embedder, self.policy.retrieval, self.tokens)
        self.verifier = A.Verifier(self.policy.learning.approval.reviewers)
        if self.store.meta("embedding_model", "") != self.embedder.name:
            self.store.set_meta("embedding_model", self.embedder.name)

    def close(self) -> None:
        self.store.close()

    # =====================================================================
    # working memory
    # =====================================================================
    def open_cycle(self, *, scope: str, session_id: str, goal: str, parent_cycle_id: str | None = None,
                   preload: dict | None = None, idempotency_key: str | None = None,
                   ttl_hours: float | None = None, now: datetime | None = None) -> dict:
        _require_scope(scope)
        now = now or utcnow()
        sig = loop_signature(goal)

        if idempotency_key:
            existing = self.store._q(
                "SELECT cycle_id FROM working_set WHERE session_id=? AND scope=? AND goal=? "
                "AND status='open'", (session_id, scope, goal))
            if existing:
                cid = existing[0]["cycle_id"]
                ws = self.store.get_cycle(cid)
                return {"cycle_id": cid, "session_id": session_id, "opened_at": ws["opened_at"],
                        "expires_at": ws["expires_at"], "reused": True, "preloaded": None,
                        "loop_warning": self._loop_warning(scope, sig, now)}

        warning = self._loop_warning(scope, sig, now)
        cid = self.store.open_cycle(
            session_id=session_id, scope=scope, goal=goal, loop_signature=sig,
            parent_cycle_id=parent_cycle_id, ttl_hours=ttl_hours or self.policy.cycle_ttl_hours, now=now)

        preload = preload if preload is not None else {}
        preloaded = None
        if preload.get("enabled", True):
            preloaded = self.recall(
                scope=scope, query=preload.get("query") or goal,
                types=list(preload.get("types") or self.policy.retrieval.types),
                k=preload.get("k", self.policy.retrieval.k),
                max_tokens=preload.get("max_tokens", self.policy.retrieval.max_tokens),
                cycle_id=cid, now=now)

        ws = self.store.get_cycle(cid)
        return {"cycle_id": cid, "session_id": session_id, "opened_at": ws["opened_at"],
                "expires_at": ws["expires_at"], "reused": False, "preloaded": preloaded,
                "loop_warning": warning}

    def _loop_warning(self, scope: str, sig: str, now: datetime) -> dict | None:
        ls = self.policy.loop_safety
        since = now - timedelta(minutes=ls.lookback_minutes)
        similar = self.store.similar_cycles(scope, sig, since, ls.lookback_cycles)
        # The incoming cycle counts as one repeat; prior ones are in `similar`.
        repeats = len(similar) + 1
        if repeats < ls.repeat_threshold:
            return None
        outcomes = [c["outcome"] for c in similar]
        if any(o == "success" for o in outcomes):
            return None  # it worked before, so this is legitimate repetition
        return {
            "loop_signature": sig,
            "repeats": repeats,
            "window_minutes": ls.lookback_minutes,
            "similar_cycle_ids": [c["cycle_id"] for c in similar],
            "last_outcomes": outcomes,
            "advice": (f"This goal has been attempted {repeats} times in "
                       f"{ls.lookback_minutes} minutes with no success "
                       f"(outcomes: {', '.join(outcomes) or 'none recorded'}). "
                       f"Change approach or escalate rather than retrying."),
        }

    def close_cycle(self, *, cycle_id: str, outcome: str, summary: str | None = None,
                    reward: float | None = None, importance: float | None = None,
                    tags: list[str] | None = None, promote_observations: bool = False,
                    now: datetime | None = None) -> dict:
        ws = self.store.get_cycle(cycle_id)
        if ws is None:
            raise CycleNotFound(f"no cycle {cycle_id}")
        now = now or utcnow()
        if ws["status"] != "open":
            return {"cycle_id": cycle_id, "closed_at": ws["closed_at"], "already_closed": True,
                    "summary_record_id": None, "episodic_record_ids": [], "steps_recorded": 0}

        obs = self.store.observations(cycle_id)
        written: list[str] = []
        summary_id = None
        base_importance = importance if importance is not None else (
            0.7 if outcome == "failure" else 0.5)  # failures teach more

        if summary:
            summary_id, _ = self._write_episodic(
                scope=ws["scope"], session_id=ws["session_id"], cycle_id=cycle_id,
                step_no=_next_step(obs), content=summary, goal=ws["goal"], outcome=outcome,
                reward=reward, importance=base_importance, tags=tags, now=now,
                parent_cycle_id=ws["parent_cycle_id"])
            written.append(summary_id)

        if promote_observations:
            for o in obs:
                rid, created = self._write_episodic(
                    scope=ws["scope"], session_id=ws["session_id"], cycle_id=cycle_id,
                    step_no=o["step_no"], content=o["text"], goal=ws["goal"], outcome=outcome,
                    importance=base_importance, tags=tags, now=now,
                    parent_cycle_id=ws["parent_cycle_id"],
                    idempotency_key=f"{cycle_id}:{o['step_no']}")
                if created:
                    written.append(rid)

        self.store.close_cycle(cycle_id, outcome, rfc3339(now))
        opened = datetime.strptime(ws["opened_at"], "%Y-%m-%dT%H:%M:%SZ")
        return {
            "cycle_id": cycle_id, "closed_at": rfc3339(now), "already_closed": False,
            "summary_record_id": summary_id, "episodic_record_ids": written,
            "duration_ms": int((now.replace(tzinfo=None) - opened).total_seconds() * 1000),
            "steps_recorded": len(obs),
        }

    def reap_cycles(self, now: datetime | None = None) -> int:
        """Close cycles that were opened and never closed. Their observations are
        still promoted - losing the trace of a crash loses the most useful data
        a failure produces."""
        now = now or utcnow()
        reaped = 0
        for ws in self.store.stale_cycles(now):
            for o in self.store.observations(ws["cycle_id"]):
                self._write_episodic(
                    scope=ws["scope"], session_id=ws["session_id"], cycle_id=ws["cycle_id"],
                    step_no=o["step_no"], content=o["text"], goal=ws["goal"], outcome="abandoned",
                    importance=0.6, now=now, parent_cycle_id=ws["parent_cycle_id"],
                    idempotency_key=f"{ws['cycle_id']}:{o['step_no']}")
            self.store.close_cycle(ws["cycle_id"], "abandoned", rfc3339(now), status="abandoned")
            reaped += 1
        return reaped

    # =====================================================================
    # retrieval
    # =====================================================================
    def recall(self, *, scope: str, query: str, types: list[str] | None = None,
               strategy: str | None = None, k: int | None = None, max_tokens: int | None = None,
               tags: list[str] | None = None, time_window: dict | None = None,
               min_confidence: float | None = None, session_id: str | None = None,
               cycle_id: str | None = None, include_sensitive: bool = False,
               include_superseded: bool = False, weights: dict | None = None,
               now: datetime | None = None) -> dict:
        _require_scope(scope)
        rp = self.policy.retrieval
        types = list(types or rp.types)
        strategy = strategy or rp.strategy
        k = k or rp.k
        max_tokens = max_tokens or rp.max_tokens
        tw = time_window or {}
        filters = {"tags": tags, "session_id": session_id,
                   "min_confidence": min_confidence if min_confidence is not None else rp.min_confidence,
                   "time_from": tw.get("from"), "time_to": tw.get("to")}

        w = rp.weights
        if weights:
            from .config import RetrievalWeights
            w = RetrievalWeights(weights.get("relevance", w.relevance),
                                 weights.get("recency", w.recency),
                                 weights.get("importance", w.importance))

        started = utcnow()
        res = self.retriever.recall(
            scope=scope, query=query, types=types, strategy=strategy, k=k, max_tokens=max_tokens,
            filters=filters, include_sensitive=include_sensitive,
            include_superseded=include_superseded, weights=w, now=now)

        if cycle_id:
            self.store.set_retrieved(cycle_id, [
                {"record_id": s.record["id"], "score": round(s.total, 6),
                 "injected": s.in_context_block, "retrieved_at": rfc3339(started)}
                for s in res.scored])

        return {
            "schema_version": SCHEMA_VERSION,
            "records": [_scored_json(s) for s in res.scored],
            "context_block": res.context_block,
            "token_count": res.token_count,
            "truncated": res.truncated,
            "omitted_count": max(res.omitted_count, 0),
            "excluded_sensitive_count": res.excluded_sensitive_count,
            "contradictions": res.contradictions,
            "query_echo": {"query": query, "scope": scope, "types": types, "strategy": strategy,
                           "k": k, "max_tokens": max_tokens,
                           "weights": {"relevance": w.relevance, "recency": w.recency,
                                       "importance": w.importance,
                                       "recency_half_life_hours": rp.recency_half_life_hours}},
            "latency_ms": int((utcnow() - started).total_seconds() * 1000),
            "degraded": res.degraded,
        }

    # =====================================================================
    # learning
    # =====================================================================
    def remember(self, *, scope: str, type: str, content: str, payload: dict | None = None,
                 tags: list[str] | None = None, importance: float | None = None,
                 confidence: float | None = None, provenance: dict | None = None,
                 expires_at: str | None = None, idempotency_key: str | None = None,
                 supersedes: str | None = None, episodic: dict | None = None,
                 semantic: dict | None = None, now: datetime | None = None) -> dict:
        _require_scope(scope)
        if type == "procedural":
            raise ProceduralWriteRequiresProposal(
                "procedural memory is written through memory_propose_procedure and a human gate")
        if type not in ("episodic", "semantic"):
            raise MemoryAgentError(f"unknown record type {type!r}")
        now = now or utcnow()
        warnings: list[str] = []

        if type == "episodic":
            if not episodic:
                raise MemoryAgentError("episodic writes require the `episodic` block")
            self._rate_limit(episodic["session_id"], now)
            rid, created = self._write_episodic(
                scope=scope, content=content, payload=payload, tags=tags,
                importance=importance, confidence=confidence, provenance=provenance,
                expires_at=expires_at, idempotency_key=idempotency_key, now=now, **episodic)
            contradictions = []
        else:
            sem = dict(semantic or {})
            contradictions = self._detect_contradictions(scope, sem, content)
            ttl = self.policy.forgetting.default_ttl_days.get("semantic")
            if expires_at is None and ttl:
                expires_at = rfc3339(now + timedelta(days=ttl))
            emb = self.embedder.embed([content])[0]
            rid, created = self.store.write_record(
                rtype="semantic", scope=scope, content=content, payload=payload, tags=tags,
                importance=importance if importance is not None else 0.5,
                confidence=confidence if confidence is not None else 0.5,
                provenance=provenance, expires_at=expires_at, idempotency_key=idempotency_key,
                embedding=emb, embedding_model=self.embedder.name, attrs=sem, now=rfc3339(now))

        if supersedes and created:
            self.store.supersede(supersedes, rid)
        if contradictions:
            warnings.append(f"{len(contradictions)} existing record(s) contradict this write; "
                            "both remain active and recall will surface the conflict")

        return {"record_id": rid, "created": created, "deduped": not created,
                "superseded_record_id": supersedes if (supersedes and created) else None,
                "contradictions_detected": contradictions,
                "embedded": self.store.vector_ok, "warnings": warnings}

    def _write_episodic(self, *, scope: str, session_id: str, cycle_id: str, step_no: int,
                        content: str, goal: str | None = None, action: dict | None = None,
                        observation: str | None = None, outcome: str = "unknown",
                        reward: float | None = None, error: dict | None = None,
                        duration_ms: int | None = None, parent_cycle_id: str | None = None,
                        payload: dict | None = None, tags: list[str] | None = None,
                        importance: float | None = None, confidence: float | None = None,
                        provenance: dict | None = None, expires_at: str | None = None,
                        idempotency_key: str | None = None,
                        now: datetime | None = None) -> tuple[str, bool]:
        now = now or utcnow()
        if importance is None:
            importance = 0.7 if outcome == "failure" else 0.5
        ttl = self.policy.forgetting.default_ttl_days.get("episodic")
        if expires_at is None and ttl:
            expires_at = rfc3339(now + timedelta(days=ttl))
        return self.store.write_record(
            rtype="episodic", scope=scope, content=content, payload=payload, tags=tags,
            importance=importance, confidence=confidence if confidence is not None else 1.0,
            provenance=provenance or {"source": "host"}, expires_at=expires_at,
            idempotency_key=idempotency_key, embedding=self.embedder.embed([content])[0],
            embedding_model=self.embedder.name, now=rfc3339(now),
            attrs={"session_id": session_id, "cycle_id": cycle_id, "step_no": step_no,
                   "parent_cycle_id": parent_cycle_id, "goal": goal, "action": action,
                   "observation": observation, "outcome": outcome, "reward": reward,
                   "error": error, "duration_ms": duration_ms})

    def _rate_limit(self, session_id: str, now: datetime) -> None:
        cap = self.policy.loop_safety.max_writes_per_session_per_minute
        if self.store.writes_in_last_minute(session_id, now) >= cap:
            raise WriteRateExceeded(
                f"session {session_id} exceeded {cap} writes/minute; "
                "this is almost always a runaway loop that ignored a loop_warning")

    def _detect_contradictions(self, scope: str, sem: dict, content: str) -> list[dict]:
        """Report, never resolve. The agent has no basis for picking a winner."""
        if not sem.get("subject"):
            return []
        out = []
        for existing in self.store.find_semantic_by_triple(scope, sem["subject"], sem["predicate"]):
            if existing["object"] != sem.get("object"):
                out.append({"record_id": existing["id"], "existing_content": existing["content"],
                            "basis": "triple"})
        return out

    def forget(self, *, scope: str, selector: dict, reason: str, max_records: int,
               mode: str = "tombstone", dry_run: bool = False, confirm: bool = False,
               now: datetime | None = None) -> dict:
        _require_scope(scope)
        if mode == "hard_delete" and not dry_run and not confirm:
            raise ConfirmRequired("hard_delete requires confirm=true")

        if "record_ids" in selector:
            ids = [r["id"] for r in self.store._q(
                "SELECT id FROM records WHERE scope=? AND id IN (%s)"
                % ",".join("?" * len(selector["record_ids"])),
                (scope, *selector["record_ids"]))]
        elif "query" in selector:
            q = selector["query"]
            res = self.retriever.recall(
                scope=scope, query=q["text"], types=["episodic", "semantic", "procedural"],
                strategy="hybrid", k=max_records + 1, max_tokens=1, filters={},
                include_sensitive=True, include_superseded=False, now=now)
            threshold = q.get("min_similarity", 0.8)
            ids = [s.record["id"] for s in res.scored
                   if s.vector_similarity is None or s.vector_similarity >= threshold]
        else:
            ids = self.store.select_for_forget(scope, selector.get("filter", {}))

        matched = len(ids)
        if matched > max_records:
            # A selector matching more than the caller expected is a bug, not a
            # big job. Abort without touching anything.
            raise BlastRadiusExceeded(
                f"selector matched {matched} records, max_records is {max_records}; nothing changed",
                matched_count=matched, max_records=max_records)

        if dry_run:
            return {"mode": mode, "dry_run": True, "matched_count": matched, "affected_count": 0,
                    "affected_ids": ids[:1000], "aborted": False, "aborted_reason": None,
                    "recoverable_until": None}

        now = now or utcnow()
        if mode == "tombstone":
            affected = self.store.set_status(ids, "tombstoned")
            recoverable = rfc3339(now + timedelta(days=self.policy.forgetting.tombstone_retention_days))
        elif mode == "redact":
            affected = self.store.redact(ids, rfc3339(now))
            recoverable = None
        else:
            affected = self.store.hard_delete(ids)
            recoverable = None
        return {"mode": mode, "dry_run": False, "matched_count": matched, "affected_count": affected,
                "affected_ids": ids[:1000], "aborted": False, "aborted_reason": None,
                "recoverable_until": recoverable}

    # =====================================================================
    # reasoning
    # =====================================================================
    def reflect(self, *, scope: str, modes: list[str] | None = None, window: dict | None = None,
                auto_commit: bool = False, max_proposals: int = 50, max_episodes: int = 5000,
                now: datetime | None = None) -> dict:
        _require_scope(scope)
        modes = modes or ["consolidate", "contradictions"]
        window = window or {}
        now = now or utcnow()
        started = now
        since = window.get("from")
        if not since and window.get("lookback_days"):
            since = rfc3339(now - timedelta(days=window["lookback_days"]))

        episodes = self.store.episodes_for_reflection(
            scope, since, window.get("session_id"), max_episodes)
        capped = len(episodes) > max_episodes
        episodes = episodes[:max_episodes]

        semantic_written: list[dict] = []
        proposals: list[dict] = []
        contradictions: list[dict] = []
        revised = 0

        if "consolidate" in modes and episodes:
            for cluster in _cluster(episodes, self.policy.forgetting.consolidate_min_cluster_size):
                fact = _distil(cluster)
                if not fact:
                    continue
                derived = [e["id"] for e in cluster]
                if auto_commit:
                    rid, created = self.store.write_record(
                        rtype="semantic", scope=scope, content=fact,
                        importance=0.6, confidence=min(0.4 + 0.1 * len(cluster), 0.9),
                        provenance={"source": "daemon", "agent": "memory-agent.daemon",
                                    "derived_from": derived},
                        embedding=self.embedder.embed([fact])[0], embedding_model=self.embedder.name,
                        idempotency_key="reflect:" + hashlib.blake2b(fact.encode(), digest_size=8).hexdigest(),
                        attrs={"corroborations": len(cluster)}, now=rfc3339(now))
                    if created:
                        semantic_written.append({"record_id": rid, "content": fact,
                                                 "derived_from": derived})
                else:
                    pid, dup = self.store.create_proposal(
                        scope=scope, kind="semantic", candidate={"content": fact},
                        rationale=f"distilled from {len(cluster)} episodes",
                        proposed_by="memory-agent.daemon", source="daemon",
                        dedupe_key="sem:" + hashlib.blake2b(fact.encode(), digest_size=8).hexdigest(),
                        now=rfc3339(now))
                    if not dup and len(proposals) < max_proposals:
                        proposals.append({"proposal_id": pid, "kind": "semantic", "summary": fact,
                                          "rationale": f"distilled from {len(cluster)} episodes"})

        if "contradictions" in modes:
            for c in self.store.triple_conflicts(scope):
                contradictions.append({"a": c["ra"], "b": c["rb"], "basis": "triple",
                                       "note": f"{c['subject']} {c['predicate']}: "
                                               f"{c['oa']!r} vs {c['ob']!r}"})

        if "promote" in modes:
            for cluster in _repeat_successes(episodes, self.policy.forgetting.consolidate_min_cluster_size):
                action = cluster[0]["action_name"]
                cand = {
                    "trigger": f"A step that would call {action}",
                    "preconditions": [],
                    "steps": [{"n": 1, "instruction": f"Use {action}; it has succeeded "
                                                      f"{len(cluster)} times in this scope."}],
                    "success_signal": "The action returns without error",
                    "failure_signal": "The action errors or is rejected",
                }
                pid, dup = self.store.create_proposal(
                    scope=scope, kind="procedural", candidate=cand,
                    rationale=f"{action} succeeded {len(cluster)} times with no failures",
                    proposed_by="memory-agent.daemon", source="daemon",
                    dedupe_key=f"proc:{action}", now=rfc3339(now))
                if not dup and len(proposals) < max_proposals:
                    proposals.append({"proposal_id": pid, "kind": "procedural",
                                      "summary": cand["trigger"],
                                      "rationale": f"{action} succeeded {len(cluster)} times"})

        if "importance" in modes:
            revised = len(self.store._q(
                "UPDATE records SET importance = min(1.0, importance + 0.1) "
                "WHERE scope=? AND status='active' AND access_count >= 5 AND importance < 0.9 "
                "RETURNING id", (scope,)))

        return {"episodes_examined": len(episodes), "capped": capped,
                "semantic_written": semantic_written, "proposals_created": proposals,
                "contradictions": contradictions, "importance_revised": revised,
                "duration_ms": int((utcnow() - started).total_seconds() * 1000)}

    # =====================================================================
    # the gate
    # =====================================================================
    def propose_procedure(self, *, scope: str, content: str, trigger: str, steps: list[dict],
                          rationale: str, proposed_by: str, preconditions: list[str] | None = None,
                          success_signal: str | None = None, failure_signal: str | None = None,
                          evidence_record_ids: list[str] | None = None, source: str = "host",
                          dedupe_key: str | None = None, supersedes: str | None = None,
                          now: datetime | None = None) -> dict:
        _require_scope(scope)
        now = now or utcnow()
        candidate = {
            "content": content, "trigger": trigger, "preconditions": preconditions or [],
            "steps": steps, "success_signal": success_signal, "failure_signal": failure_signal,
            "evidence_record_ids": evidence_record_ids or [], "supersedes": supersedes,
        }
        pid, dup = self.store.create_proposal(
            scope=scope, kind="procedural", candidate=candidate, rationale=rationale,
            proposed_by=proposed_by, source=source, dedupe_key=dedupe_key, now=rfc3339(now))
        return {
            "proposal_id": pid,
            "state": "pending",  # the only value this can ever be
            "dedupe_hit": dup,
            "queue_depth": self.store.queue_depth(scope),
            "expires_at": rfc3339(now + timedelta(days=self.policy.learning.proposal_expiry_days)),
        }

    def review_proposals(self, *, action: str, scope: str | None = None,
                         proposal_ids: list[str] | None = None, reviewed_by: str | None = None,
                         signatures: list[dict] | None = None, note: str | None = None,
                         state: str = "pending", kind: str | None = None, limit: int = 50,
                         challenge_ttl_seconds: int | None = None, caller: str = "host",
                         now: datetime | None = None) -> dict:
        now = now or utcnow()
        if action == "list":
            return self._list_proposals(scope, state, kind, limit, challenge_ttl_seconds, now)

        if action not in ("approve", "reject"):
            raise MemoryAgentError(f"unknown action {action!r}")
        if caller == "daemon" and not self.policy.learning.daemon_may_approve:
            raise ApprovalSignatureRequired(
                "the daemon may propose but never decide; and it holds no reviewer key")
        if not proposal_ids or not reviewed_by:
            raise MemoryAgentError("approve/reject require proposal_ids and reviewed_by")
        if self.policy.learning.approval.require_signature:
            self.policy.require_reviewers()
            if not signatures:
                raise ApprovalSignatureRequired(
                    "approve/reject require a signature for every proposal id")

        by_id = {s["proposal_id"]: s for s in (signatures or [])}
        approved, rejected, skipped = [], [], []

        for pid in proposal_ids:
            try:
                result = self._decide_one(pid, action, reviewed_by, by_id.get(pid), note, now)
            except MemoryAgentError as exc:
                skipped.append({"proposal_id": pid, "reason": _skip_reason(exc),
                                "detail": exc.message[:1024]})
                continue
            if result is None:
                continue
            if action == "approve":
                approved.append(result)
            else:
                rejected.append(pid)

        return {"action": action, "approved": approved, "rejected": rejected, "skipped": skipped,
                "queue_depth": self.store.queue_depth(scope)}

    def _list_proposals(self, scope, state, kind, limit, ttl, now) -> dict:
        ttl = ttl or self.policy.learning.approval.challenge_ttl_seconds
        expires = A.challenge_expiry(ttl, now)
        out = []
        for p in self.store.list_proposals(scope, state, kind, limit):
            candidate = json.loads(p["candidate"])
            entry = {
                "proposal_id": p["id"], "scope": p["scope"], "kind": p["kind"], "state": p["state"],
                "candidate": candidate, "rationale": p["rationale"],
                "evidence_record_ids": candidate.get("evidence_record_ids", []),
                "proposed_by": p["proposed_by"], "proposed_at": p["proposed_at"], "source": p["source"],
            }
            if p["state"] == "pending":
                chash = A.candidate_hash(candidate)
                nonce = A.new_nonce()
                self.store.issue_challenge(
                    nonce=nonce, proposal_id=p["id"], scope=p["scope"], candidate_sha256=chash,
                    expires_at=expires, now=rfc3339(now))
                entry.update({
                    "candidate_sha256": chash,
                    "signing_payload": A.build_payload(
                        scope=p["scope"], proposal_id=p["id"], candidate_sha256=chash,
                        decision="approve", reviewer="<your-reviewer-id>", nonce=nonce,
                        expires=expires),
                    "nonce": nonce,
                    "challenge_expires_at": expires,
                })
            out.append(entry)
        return {"action": "list", "proposals": out, "queue_depth": self.store.queue_depth(scope)}

    def _decide_one(self, pid: str, action: str, reviewed_by: str, sig: dict | None,
                    note: str | None, now: datetime) -> dict | None:
        prop = self.store.get_proposal(pid)
        if prop is None:
            raise MemoryAgentError("not_found")
        if prop["state"] != "pending":
            raise MemoryAgentError("already_decided")

        candidate = json.loads(prop["candidate"])
        current_hash = A.candidate_hash(candidate)

        if self.policy.learning.approval.require_signature:
            if sig is None:
                raise ApprovalSignatureRequired(f"no signature supplied for {pid}")
            fields = A.parse_payload(sig["signed_payload"])
            ch = self.store.consume_challenge(fields["nonce"], pid, reviewed_by, now)
            if ch is None:
                raise ApprovalChallengeInvalid("unknown challenge")
            if ch["consumed_at"] is not None:
                raise ApprovalChallengeInvalid("challenge already used")
            if ch["candidate_sha256"] != current_hash:
                raise ApprovalCandidateChanged("candidate changed since the challenge was issued")
            self.verifier.verify_decision(
                key_id=sig["key_id"], payload=sig["signed_payload"], sig_b64=sig["sig"],
                expect_scope=prop["scope"], expect_proposal=pid, expect_decision=action,
                expect_reviewer=reviewed_by, expect_candidate_sha256=current_hash,
                challenge_nonce=fields["nonce"], now=now)
            sig_row = {"alg": sig.get("alg", "ed25519"), "key_id": sig["key_id"],
                       "signed_payload": sig["signed_payload"], "sig": sig["sig"]}
        else:
            sig_row = {"alg": "ed25519", "key_id": "SHA256:unsigned",
                       "signed_payload": "", "sig": ""}

        if action == "reject":
            self.store.decide_proposal(pid, state="rejected", reviewed_by=reviewed_by, note=note,
                                       sig=sig_row, now=rfc3339(now))
            return {"proposal_id": pid}

        rid = self._materialise(prop, candidate, reviewed_by, note, sig_row, current_hash, now)
        self.store.decide_proposal(pid, state="approved", reviewed_by=reviewed_by, note=note,
                                   sig=sig_row, materialised=rid, now=rfc3339(now))
        return {"proposal_id": pid, "record_id": rid, "signature_verified": True,
                "key_id": sig_row["key_id"]}

    def _materialise(self, prop, candidate, reviewed_by, note, sig_row, chash, now) -> str:
        if prop["kind"] == "semantic":
            rid, _ = self.store.write_record(
                rtype="semantic", scope=prop["scope"], content=candidate["content"],
                importance=0.6, confidence=0.8,
                provenance={"source": "human", "agent": reviewed_by},
                embedding=self.embedder.embed([candidate["content"]])[0],
                embedding_model=self.embedder.name, now=rfc3339(now), attrs={})
            return rid
        content = candidate.get("content") or candidate["trigger"]
        rid, _ = self.store.write_record(
            rtype="procedural", scope=prop["scope"], content=content, importance=0.8, confidence=0.9,
            provenance={"source": "human", "agent": reviewed_by,
                        "derived_from": candidate.get("evidence_record_ids", [])},
            embedding=self.embedder.embed([content])[0], embedding_model=self.embedder.name,
            now=rfc3339(now),
            attrs={"trigger": candidate["trigger"], "preconditions": candidate.get("preconditions", []),
                   "steps": candidate["steps"], "success_signal": candidate.get("success_signal"),
                   "failure_signal": candidate.get("failure_signal"), "approval_state": "approved",
                   "reviewed_by": reviewed_by, "reviewed_at": rfc3339(now), "rationale": note,
                   "signature": {**sig_row, "candidate_sha256": chash},
                   "sig_verified_at": rfc3339(now), "supersedes": candidate.get("supersedes")})
        if candidate.get("supersedes"):
            self.store.supersede(candidate["supersedes"], rid)
        return rid

    def reverify_approvals(self, scope: str | None = None) -> list[dict]:
        """Re-check stored approvals. A procedure edited after approval fails here
        and must be treated as unapproved."""
        bad = []
        for row in self.store.approved_procedures(scope):
            live = A.candidate_hash(self.store.procedure_candidate(row["id"]))
            problem = None
            if live != row["candidate_sha256"]:
                problem = "content changed after approval"
            else:
                try:
                    self.verifier.verify_bytes(row["sig_key_id"], row["sig_payload"], row["sig_value"])
                except MemoryAgentError as exc:
                    problem = exc.message
            if problem:
                bad.append({"record_id": row["id"], "scope": row["scope"], "problem": problem,
                            "signed_hash": row["candidate_sha256"], "current_hash": live,
                            "key_id": row["sig_key_id"]})
            else:
                self.store._q("UPDATE procedural_attrs SET sig_verified_at=? WHERE record_id=?",
                              (rfc3339(utcnow()), row["id"]))
        return bad

    # =====================================================================
    # introspection
    # =====================================================================
    def stats(self, *, scope: str | None = None, include_scope_breakdown: bool = False,
              include_top_accessed: int = 0) -> dict:
        s = self.store.stats(scope)
        warnings: list[str] = []
        if not self.store.vector_ok:
            warnings.append("vector extension not loaded; recall is keyword-only")
        if s["active"] and s["embedded"] < s["active"]:
            warnings.append(f"{s['active'] - s['embedded']} active records have no current embedding")
        if s["pending_proposals"]:
            oldest = s["oldest_pending_at"]
            warnings.append(f"{s['pending_proposals']} proposal(s) pending review"
                            + (f", oldest {oldest}" if oldest else ""))
        if s["stale_cycles"]:
            warnings.append(f"{s['stale_cycles']} cycle(s) opened and never closed")
        if s["failing_procedures"]:
            warnings.append(f"{s['failing_procedures']} procedure(s) failing more often than they succeed")
        if not self.policy.learning.approval.reviewers:
            warnings.append("no reviewer keys configured; no procedure can be approved")
        if scope and s["total"] > self.policy.max_records_per_scope:
            warnings.append(f"scope holds {s['total']} records, above the configured ceiling")

        out = {
            "scope": scope,
            "counts": {"by_type": s["by_type"], "by_status": s["by_status"], "total": s["total"]},
            "embedding": {"model": self.embedder.name, "dimensions": self.embedder.dimensions,
                          "coverage": round(s["embedded"] / s["active"], 4) if s["active"] else 1.0,
                          "pending_reembed": max(s["active"] - s["embedded"], 0)},
            "queue": {"pending_proposals": s["pending_proposals"],
                      "oldest_pending_at": s["oldest_pending_at"]},
            "cycles": {"open": s["open_cycles"], "stale": s["stale_cycles"], "abandoned_last_7d": 0},
            "health": {"failing_procedures": s["failing_procedures"], "unresolved_contradictions":
                       len(self.store.triple_conflicts(scope)) if scope else 0,
                       "sensitive_records": s["sensitive"], "db_size_bytes": s["db_size_bytes"],
                       "last_daemon_run_at": self.store.meta("last_daemon_run_at") or None},
            "warnings": warnings,
        }
        if include_top_accessed:
            out["top_accessed"] = [
                {"record_id": r["id"], "content": r["content"], "access_count": r["access_count"]}
                for r in self.store.top_accessed(scope, include_top_accessed)]
        if include_scope_breakdown:
            out["scopes"] = self.store.scopes()
        return out


# ---------------------------------------------------------------------------
def _require_scope(scope: str | None) -> None:
    if not scope:
        raise ScopeRequired("scope is the isolation boundary; there is no wildcard")


def _next_step(obs: list[dict]) -> int:
    return max((o["step_no"] for o in obs), default=-1) + 1


def record_json(rec: dict) -> dict:
    """Render a stored row as the record its schema describes.

    Column names are not field names - `trigger_text` is a column, `trigger` is
    the contract - and the type-specific blocks have to be reassembled. Getting
    this wrong is invisible until a client validates, which is why the
    conformance tests check it.
    """
    out = {"schema_version": SCHEMA_VERSION, "id": rec["id"], "type": rec["type"],
           "scope": rec["scope"], "content": rec["content"], "created_at": rec["created_at"],
           "status": rec["status"], "importance": rec["importance"],
           "confidence": rec["confidence"], "access_count": rec["access_count"]}
    for key in ("updated_at", "last_accessed_at", "expires_at", "superseded_by",
                "idempotency_key", "embedding_model"):
        if rec.get(key) is not None:
            out[key] = rec[key]
    for key in ("payload", "tags", "provenance"):
        if rec.get(key):
            out[key] = json.loads(rec[key])

    if rec["type"] == "semantic":
        for key in ("subject", "predicate", "object", "valid_from", "valid_to",
                    "corroborations", "volatility", "sensitivity"):
            if rec.get(key) is not None:
                out[key] = rec[key]
        if rec.get("contradicts"):
            out["contradicts"] = json.loads(rec["contradicts"])

    elif rec["type"] == "episodic":
        for key in ("session_id", "cycle_id", "parent_cycle_id", "step_no", "goal",
                    "observation", "outcome", "reward", "duration_ms"):
            if rec.get(key) is not None:
                out[key] = rec[key]
        if rec.get("action_class") and rec.get("action_name"):
            action = {"class": rec["action_class"], "name": rec["action_name"]}
            if rec.get("action_input"):
                action["input"] = json.loads(rec["action_input"])
            out["action"] = action
        if rec.get("error"):
            out["error"] = json.loads(rec["error"])

    elif rec["type"] == "procedural":
        out["trigger"] = rec["trigger_text"]
        out["steps"] = json.loads(rec["steps"])
        if rec.get("preconditions"):
            out["preconditions"] = json.loads(rec["preconditions"])
        for key in ("success_signal", "failure_signal", "supersedes"):
            if rec.get(key) is not None:
                out[key] = rec[key]
        # The approval travels with the procedure so a host can verify the
        # signature itself before following it, rather than trusting the server
        # that handed it over.
        approval = {"state": rec["approval_state"], "reviewed_by": rec["reviewed_by"],
                    "reviewed_at": rec["reviewed_at"],
                    "signature": {"alg": rec["sig_alg"], "key_id": rec["sig_key_id"],
                                  "signed_payload": rec["sig_payload"], "sig": rec["sig_value"],
                                  "candidate_sha256": rec["candidate_sha256"],
                                  "verified_at": rec.get("sig_verified_at")}}
        if rec.get("rationale"):
            approval["rationale"] = rec["rationale"]
        out["approval"] = approval
        out["usage"] = {"invocations": rec["invocations"], "successes": rec["successes"],
                        "last_used_at": rec.get("last_used_at")}
    return out


def _scored_json(s: Scored) -> dict:
    return {"rank": s.rank, "record": record_json(s.record), "in_context_block": s.in_context_block,
            "score": {"total": round(s.total, 6), "relevance": round(s.relevance, 6),
                      "recency": round(s.recency, 6), "importance": s.importance,
                      "vector_similarity": (round(s.vector_similarity, 6)
                                            if s.vector_similarity is not None else None),
                      "keyword_rank": s.keyword_rank}}


def _skip_reason(exc: MemoryAgentError) -> str:
    return {
        "APPROVAL_SIGNATURE_REQUIRED": "signature_missing",
        "APPROVAL_SIGNATURE_INVALID": "signature_invalid",
        "APPROVAL_KEY_UNKNOWN": "unknown_key",
        "APPROVAL_CHALLENGE_INVALID": "challenge_expired",
        "APPROVAL_CANDIDATE_CHANGED": "candidate_changed",
    }.get(exc.code, {"already_decided": "already_decided",
                     "not_found": "not_found"}.get(exc.message, "signature_invalid"))


def _cluster(episodes: list[dict], min_size: int) -> list[list[dict]]:
    """Group episodes by cycle, then keep groups big enough to be a pattern
    rather than a one-off."""
    groups: dict[str, list[dict]] = {}
    for e in episodes:
        groups.setdefault(e["cycle_id"], []).append(e)
    return [g for g in groups.values() if len(g) >= min_size]


def _distil(cluster: list[dict]) -> str | None:
    outcomes = [e["outcome"] for e in cluster]
    if not cluster:
        return None
    verdict = "succeeded" if outcomes.count("success") > len(outcomes) / 2 else "did not succeed"
    head = cluster[0]["content"].strip().rstrip(".")
    return f"A {len(cluster)}-step attempt beginning '{head[:160]}' {verdict}."


def _repeat_successes(episodes: list[dict], min_size: int) -> list[list[dict]]:
    by_action: dict[str, list[dict]] = {}
    for e in episodes:
        if e.get("action_name"):
            by_action.setdefault(e["action_name"], []).append(e)
    return [g for g in by_action.values()
            if len(g) >= min_size and all(e["outcome"] == "success" for e in g)]
