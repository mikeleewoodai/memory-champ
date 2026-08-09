import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from memory_agent import approval as A  # noqa: E402
from memory_agent.config import ApprovalPolicy, Policy  # noqa: E402
from memory_agent.embedding import HashingEmbedder  # noqa: E402
from memory_agent.service import MemoryService  # noqa: E402
from memory_agent.store import Store  # noqa: E402

SCOPE = "proj.a"


@pytest.fixture
def reviewer():
    """A reviewer keypair. The private half stands in for what lives on the
    human's machine and never reaches the server."""
    priv = Ed25519PrivateKey.generate()
    return priv, A.fingerprint(priv.public_key())


@pytest.fixture
def policy(reviewer):
    priv, key_id = reviewer
    p = Policy()
    p.db_path = ":memory:"
    p.require_vector_extension = False
    p.embedding_provider = "hashing"
    p.learning.approval = ApprovalPolicy(
        reviewers=[A.ReviewerKey("mike", priv.public_key(), key_id)])
    return p


@pytest.fixture
def svc(policy):
    embedder = HashingEmbedder(384)
    service = MemoryService(policy, Store(":memory:", dimensions=384), embedder)
    yield service
    service.close()


@pytest.fixture
def approve(svc, reviewer):
    """Do what a human does: list, sign the issued payload, submit."""
    priv, key_id = reviewer

    def _approve(proposal_id, *, decision="approve", reviewer_id="mike",
                 tamper_payload=None, sign_key=None, scope=SCOPE):
        entry = next(p for p in svc.review_proposals(action="list", scope=scope)["proposals"]
                     if p["proposal_id"] == proposal_id)
        payload = tamper_payload or A.build_payload(
            scope=entry["scope"], proposal_id=proposal_id,
            candidate_sha256=entry["candidate_sha256"], decision=decision,
            reviewer=reviewer_id, nonce=entry["nonce"], expires=entry["challenge_expires_at"])
        sig = A.sign_payload(sign_key or priv, payload)
        return svc.review_proposals(
            action=decision, proposal_ids=[proposal_id], reviewed_by=reviewer_id,
            signatures=[{"proposal_id": proposal_id, "alg": "ed25519", "key_id":
                         A.fingerprint((sign_key or priv).public_key()),
                         "signed_payload": payload, "sig": sig}]), payload, sig

    return _approve


@pytest.fixture
def a_procedure(svc):
    def _propose(scope=SCOPE, dedupe_key=None, trigger="Sending any invoice to Acme"):
        return svc.propose_procedure(
            scope=scope, content=f"Invoicing: {trigger}", trigger=trigger,
            steps=[{"n": 1, "instruction": "Export the invoice as PDF, not Word."}],
            rationale="The client corrected us after a failed send.",
            proposed_by="crm-builder", dedupe_key=dedupe_key)["proposal_id"]

    return _propose
