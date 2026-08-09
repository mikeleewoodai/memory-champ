"""Policy loading. Defaults mirror contracts/policy.example.yaml exactly, so the
acceptance criteria in spec §10 hold with no config file at all."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .approval import ReviewerKey, fingerprint, load_public_key
from .errors import NoReviewerKeysConfigured


@dataclass
class RetrievalWeights:
    relevance: float = 0.5
    recency: float = 0.2
    importance: float = 0.3


@dataclass
class RetrievalPolicy:
    weights: RetrievalWeights = field(default_factory=RetrievalWeights)
    recency_half_life_hours: float = 72.0
    rrf_k: int = 60
    strategy: str = "hybrid"
    k: int = 12
    max_tokens: int = 1500
    min_confidence: float = 0.0
    types: tuple[str, ...] = ("semantic", "procedural")
    candidate_pool: int = 100
    redact_sensitive_in_context_block: bool = True
    procedure_min_success_rate: float = 0.5
    procedure_min_invocations_to_judge: int = 5


@dataclass
class LoopSafetyPolicy:
    repeat_threshold: int = 3
    lookback_cycles: int = 20
    lookback_minutes: int = 60
    max_writes_per_session_per_minute: int = 120


@dataclass
class ApprovalPolicy:
    require_signature: bool = True
    challenge_ttl_seconds: int = 600
    reviewers: list[ReviewerKey] = field(default_factory=list)
    reverify_on_daemon_run: bool = True


@dataclass
class LearningPolicy:
    gates: dict = field(default_factory=lambda: {"episodic": "auto", "semantic": "auto", "procedural": "proposal"})
    daemon_may_approve: bool = False
    proposal_expiry_days: int = 30
    approval: ApprovalPolicy = field(default_factory=ApprovalPolicy)


@dataclass
class ForgettingPolicy:
    default_ttl_days: dict = field(default_factory=lambda: {"episodic": 90, "semantic": None, "procedural": None})
    volatility_recheck_days: dict = field(default_factory=lambda: {"stable": 365, "slow": 90, "volatile": 14})
    consolidate_episodes_older_than_days: int = 30
    consolidate_min_cluster_size: int = 3
    tombstone_retention_days: int = 180
    hard_delete_requires_explicit_call: bool = True


@dataclass
class Policy:
    db_path: str = "./memory.db"
    busy_timeout_ms: int = 5000
    require_vector_extension: bool = True
    embedding_provider: str = "sentence-transformers"
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dimensions: int = 384
    cycle_ttl_hours: float = 24.0
    reap_abandoned_cycles: bool = True
    max_observations_per_cycle: int = 512
    retrieval: RetrievalPolicy = field(default_factory=RetrievalPolicy)
    loop_safety: LoopSafetyPolicy = field(default_factory=LoopSafetyPolicy)
    learning: LearningPolicy = field(default_factory=LearningPolicy)
    forgetting: ForgettingPolicy = field(default_factory=ForgettingPolicy)
    log_content: bool = False
    max_records_per_scope: int = 250_000

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Policy":
        path = path or os.environ.get("MEMORY_AGENT_POLICY")
        if not path or not Path(path).exists():
            return cls()
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls.from_dict(raw, base_dir=Path(path).parent)

    @classmethod
    def from_dict(cls, raw: dict, base_dir: Path | None = None) -> "Policy":
        p = cls()
        storage = raw.get("storage", {})
        db = storage.get("path", p.db_path)
        # Relative DB paths resolve against the policy file, not the process cwd -
        # a daemon started from elsewhere must not create a second empty database.
        if base_dir and not os.path.isabs(db) and db != ":memory:":
            db = str((base_dir / db).resolve())
        p.db_path = db
        p.busy_timeout_ms = storage.get("busy_timeout_ms", p.busy_timeout_ms)
        p.require_vector_extension = storage.get("require_vector_extension", p.require_vector_extension)

        emb = raw.get("embedding", {})
        p.embedding_provider = emb.get("provider", p.embedding_provider)
        p.embedding_model = emb.get("model", p.embedding_model)
        p.embedding_dimensions = emb.get("dimensions", p.embedding_dimensions)

        r = raw.get("retrieval", {})
        w = r.get("weights", {})
        p.retrieval.weights = RetrievalWeights(
            w.get("relevance", 0.5), w.get("recency", 0.2), w.get("importance", 0.3))
        p.retrieval.recency_half_life_hours = r.get("recency_half_life_hours", 72.0)
        p.retrieval.rrf_k = r.get("rrf_k", 60)
        d = r.get("defaults", {})
        p.retrieval.strategy = d.get("strategy", "hybrid")
        p.retrieval.k = d.get("k", 12)
        p.retrieval.max_tokens = d.get("max_tokens", 1500)
        p.retrieval.min_confidence = d.get("min_confidence", 0.0)
        p.retrieval.types = tuple(d.get("types", ("semantic", "procedural")))
        p.retrieval.candidate_pool = r.get("candidate_pool", 100)
        p.retrieval.redact_sensitive_in_context_block = r.get("redact_sensitive_in_context_block", True)
        p.retrieval.procedure_min_success_rate = r.get("procedure_min_success_rate", 0.5)
        p.retrieval.procedure_min_invocations_to_judge = r.get("procedure_min_invocations_to_judge", 5)

        wm = raw.get("working_memory", {})
        p.cycle_ttl_hours = wm.get("cycle_ttl_hours", p.cycle_ttl_hours)
        p.reap_abandoned_cycles = wm.get("reap_abandoned_cycles", True)
        p.max_observations_per_cycle = wm.get("max_observations_per_cycle", 512)

        ls = raw.get("loop_safety", {})
        p.loop_safety = LoopSafetyPolicy(
            ls.get("repeat_threshold", 3), ls.get("lookback_cycles", 20),
            ls.get("lookback_minutes", 60), ls.get("max_writes_per_session_per_minute", 120))

        ln = raw.get("learning", {})
        p.learning.gates = ln.get("gates", p.learning.gates)
        p.learning.daemon_may_approve = ln.get("daemon_may_approve", False)
        p.learning.proposal_expiry_days = ln.get("proposal_expiry_days", 30)
        ap = ln.get("approval", {})
        p.learning.approval = ApprovalPolicy(
            require_signature=ap.get("require_signature", True),
            challenge_ttl_seconds=ap.get("challenge_ttl_seconds", 600),
            reviewers=_load_reviewers(ap.get("reviewers", [])),
            reverify_on_daemon_run=ap.get("reverify_on_daemon_run", True),
        )

        fg = raw.get("forgetting", {})
        p.forgetting.default_ttl_days = fg.get("default_ttl_days", p.forgetting.default_ttl_days)
        p.forgetting.volatility_recheck_days = fg.get("volatility_recheck_days", p.forgetting.volatility_recheck_days)
        p.forgetting.consolidate_episodes_older_than_days = fg.get("consolidate_episodes_older_than_days", 30)
        p.forgetting.consolidate_min_cluster_size = fg.get("consolidate_min_cluster_size", 3)
        p.forgetting.tombstone_retention_days = fg.get("tombstone_retention_days", 180)
        p.forgetting.hard_delete_requires_explicit_call = fg.get("hard_delete_requires_explicit_call", True)

        obs = raw.get("observability", {})
        p.log_content = obs.get("log_content", False)
        p.max_records_per_scope = raw.get("limits", {}).get("max_records_per_scope", p.max_records_per_scope)
        return p

    def require_reviewers(self) -> None:
        """Refuse to run with signatures required and nobody able to sign, rather
        than discovering it at the first approval."""
        if self.learning.approval.require_signature and not self.learning.approval.reviewers:
            raise NoReviewerKeysConfigured(
                "learning.approval.reviewers is empty but require_signature is true"
            )


def _load_reviewers(entries: list[dict]) -> list[ReviewerKey]:
    keys: list[ReviewerKey] = []
    for e in entries:
        text = (e.get("public_key") or "").strip()
        # The template ships placeholders; treat them as "not configured yet"
        # rather than crashing on a key nobody has replaced.
        if not text or "REPLACE_WITH" in text:
            continue
        pub = load_public_key(text)
        actual = fingerprint(pub)
        declared = e.get("key_id")
        if declared and declared != actual and "REPLACE_WITH" not in declared:
            raise ValueError(
                f"reviewer {e.get('id')}: key_id {declared} does not match the public key ({actual})"
            )
        keys.append(ReviewerKey(
            id=e.get("id", "unknown"), public_key=pub, key_id=actual,
            retired=bool(e.get("retired")), revoked=bool(e.get("revoked")),
        ))
    return keys
