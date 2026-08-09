"""Ed25519 approval signatures — the procedural gate.

`reviewed_by` is a claim any caller can type. This module is the proof. The
private key lives with the reviewer and never reaches the server, so an agent
holding the tool still cannot manufacture a decision.

Two rules here are load-bearing and easy to get wrong:

1.  The signed payload is fixed-field text, not JSON. Canonical JSON is a
    footgun - key order, unicode escaping, number formatting - and a verifier
    that re-serialises can disagree with the signer over bytes that look
    identical.

2.  Verification uses the payload *as stored*, never one rebuilt from current
    field values. Rebuilding would prove only that a row is self-consistent
    with itself. Storing what was signed is what makes an approval verifiable
    years later and what makes post-approval edits detectable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .errors import (
    ApprovalCandidateChanged,
    ApprovalChallengeInvalid,
    ApprovalKeyUnknown,
    ApprovalSignatureInvalid,
)

# v2 because candidate_sha256 changed meaning: it now covers `content`. A v1
# signature and a v2 signature over the same candidate are different hashes, so
# refusing v1 outright is honest - the alternative is a v1 approval failing its
# hash check and being reported as tampering it never committed.
PAYLOAD_VERSION = "memory-agent-approval-v2"
PAYLOAD_FIELDS = ("scope", "proposal", "candidate_sha256", "decision", "reviewer", "nonce", "expires")

# Fields of a proposal candidate covered by candidate_sha256. Envelope fields the
# server assigns are excluded - including them would make the hash unreproducible
# by the reviewer, who signs before the record exists.
#
# `content` is covered, and must stay covered. It is the only indexed field, it is
# what recall returns, and it is therefore the text an agent actually follows.
# Leaving it out meant an approved procedure's instructions could be rewritten
# without invalidating the signature, which defeats the point of signing.
CANDIDATE_FIELDS = ("content", "trigger", "preconditions", "steps", "success_signal", "failure_signal")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# keys
# ---------------------------------------------------------------------------
def load_public_key(text: str) -> Ed25519PublicKey:
    """Accept an OpenSSH 'ssh-ed25519 AAAA...' line or raw base64.

    OpenSSH form is supported so an existing SSH key can be reused and there is
    no new key material to manage.
    """
    text = text.strip()
    if text.startswith("ssh-ed25519 "):
        key = serialization.load_ssh_public_key(text.encode())
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("not an ed25519 key")
        return key
    raw = base64.b64decode(text, validate=True)
    if len(raw) != 32:
        raise ValueError(f"expected 32 raw public key bytes, got {len(raw)}")
    return Ed25519PublicKey.from_public_bytes(raw)


def load_private_key(data: bytes, password: bytes | None = None) -> Ed25519PrivateKey:
    """Accept an OpenSSH private key file or 32 raw seed bytes."""
    if data.lstrip().startswith(b"-----BEGIN"):
        key = serialization.load_ssh_private_key(data, password=password)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("not an ed25519 key")
        return key
    if len(data) == 32:
        return Ed25519PrivateKey.from_private_bytes(data)
    raise ValueError("expected an OpenSSH private key or 32 raw seed bytes")


def fingerprint(key: Ed25519PublicKey) -> str:
    """OpenSSH-style SHA256 fingerprint: 'SHA256:' + unpadded base64 digest."""
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def openssh_public(key: Ed25519PublicKey) -> str:
    return key.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()


# ---------------------------------------------------------------------------
# canonical forms
# ---------------------------------------------------------------------------
def candidate_hash(candidate: dict) -> str:
    """SHA256 over the candidate's decision-relevant fields.

    UTF-8 JSON, keys sorted, no insignificant whitespace, non-ASCII unescaped.
    A verifier that disagrees here rejects the reviewer's genuine approvals, so
    this is pinned in the schema too.
    """
    subset = {k: candidate[k] for k in CANDIDATE_FIELDS if k in candidate and candidate[k] is not None}
    return hashlib.sha256(
        json.dumps(subset, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def build_payload(
    *, scope: str, proposal_id: str, candidate_sha256: str, decision: str,
    reviewer: str, nonce: str, expires: str,
) -> str:
    if decision not in ("approve", "reject"):
        raise ValueError(f"decision must be approve or reject, got {decision!r}")
    for name, value in (("reviewer", reviewer), ("scope", scope), ("proposal", proposal_id), ("nonce", nonce)):
        # A newline in any field would let a caller forge extra lines and change
        # what the reviewer is actually attesting to.
        if "\n" in value or "\r" in value:
            raise ValueError(f"{name} must not contain a newline")
    return (
        f"{PAYLOAD_VERSION}\n"
        f"scope: {scope}\n"
        f"proposal: {proposal_id}\n"
        f"candidate_sha256: {candidate_sha256}\n"
        f"decision: {decision}\n"
        f"reviewer: {reviewer}\n"
        f"nonce: {nonce}\n"
        f"expires: {expires}\n"
    )


def parse_payload(payload: str) -> dict[str, str]:
    """Parse a signed payload strictly. Anything unexpected is a rejection.

    Strictness matters: this runs on attacker-influenced input, and a lenient
    parser is how a payload gets read one way by the verifier and another by
    whatever consumes it next.
    """
    lines = payload.split("\n")
    if not lines or lines[0] != PAYLOAD_VERSION:
        raise ApprovalSignatureInvalid("unrecognised payload version")
    if lines[-1] != "":
        raise ApprovalSignatureInvalid("payload must end with a newline")
    fields: dict[str, str] = {}
    for line in lines[1:-1]:
        m = re.fullmatch(r"([a-z0-9_]+): (.*)", line)
        if not m:
            raise ApprovalSignatureInvalid("malformed payload line")
        if m.group(1) in fields:
            raise ApprovalSignatureInvalid(f"duplicate field {m.group(1)}")
        fields[m.group(1)] = m.group(2)
    if set(fields) != set(PAYLOAD_FIELDS):
        raise ApprovalSignatureInvalid(
            f"payload fields {sorted(fields)} != {sorted(PAYLOAD_FIELDS)}"
        )
    return fields


def new_nonce() -> str:
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------
# signing (reviewer side - runs in the CLI, never on the server)
# ---------------------------------------------------------------------------
def sign_payload(private_key: Ed25519PrivateKey, payload: str) -> str:
    return base64.b64encode(private_key.sign(payload.encode())).decode()


@dataclass(frozen=True)
class ReviewerKey:
    id: str
    public_key: Ed25519PublicKey
    key_id: str
    retired: bool = False
    revoked: bool = False

    @property
    def may_decide(self) -> bool:
        """Retired keys still verify old signatures but authorise nothing new."""
        return not (self.retired or self.revoked)


class Verifier:
    """Server side. Holds public keys only."""

    def __init__(self, reviewers: list[ReviewerKey]):
        self._by_id = {r.key_id: r for r in reviewers}

    @property
    def configured(self) -> bool:
        return bool(self._by_id)

    def key(self, key_id: str) -> ReviewerKey:
        key = self._by_id.get(key_id)
        if key is None or key.revoked:
            raise ApprovalKeyUnknown(f"unknown or revoked key {key_id}")
        return key

    def verify_bytes(self, key_id: str, payload: str, sig_b64: str) -> ReviewerKey:
        """Signature check only. Used for re-verifying stored approvals, where a
        retired key must still validate what it signed while it was current."""
        key = self.key(key_id)
        try:
            key.public_key.verify(base64.b64decode(sig_b64, validate=True), payload.encode())
        except (InvalidSignature, ValueError, TypeError) as exc:
            raise ApprovalSignatureInvalid("signature does not verify") from exc
        return key

    def verify_decision(
        self, *, key_id: str, payload: str, sig_b64: str, expect_scope: str,
        expect_proposal: str, expect_decision: str, expect_reviewer: str,
        expect_candidate_sha256: str, challenge_nonce: str | None, now: datetime | None = None,
    ) -> dict[str, str]:
        """Full check for a new decision.

        Order matters: verify the signature over the stored bytes FIRST, then
        read fields out of those bytes. Parsing before verifying would mean
        acting on attacker-controlled structure.
        """
        key = self.verify_bytes(key_id, payload, sig_b64)
        if not key.may_decide:
            raise ApprovalKeyUnknown(f"key {key_id} is retired and cannot authorise new decisions")

        fields = parse_payload(payload)
        now = now or utcnow()

        # Every mismatch below is a distinct attack, and each gets its own error
        # so the failure is diagnosable rather than a generic "invalid".
        if fields["candidate_sha256"] != expect_candidate_sha256:
            raise ApprovalCandidateChanged(
                "the candidate changed after it was signed",
                signed=fields["candidate_sha256"], current=expect_candidate_sha256,
            )
        for field, expected in (
            ("scope", expect_scope), ("proposal", expect_proposal),
            ("decision", expect_decision), ("reviewer", expect_reviewer),
        ):
            if fields[field] != expected:
                raise ApprovalSignatureInvalid(
                    f"signed {field} does not match the request",
                    signed=fields[field], requested=expected,
                )
        if challenge_nonce is not None and fields["nonce"] != challenge_nonce:
            raise ApprovalChallengeInvalid("nonce does not match an open challenge")
        try:
            expires = datetime.strptime(fields["expires"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise ApprovalSignatureInvalid("malformed expires field") from exc
        if expires <= now:
            raise ApprovalChallengeInvalid("challenge expired", expired_at=fields["expires"])
        return fields


def challenge_expiry(ttl_seconds: int, now: datetime | None = None) -> str:
    return rfc3339((now or utcnow()) + timedelta(seconds=ttl_seconds))
