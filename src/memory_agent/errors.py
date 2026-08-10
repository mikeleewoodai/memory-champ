"""Error codes from contracts/mcp-tools.json.

Every failure a caller can provoke has a code here. The codes are part of the
contract, so they are stable strings rather than exception class names.
"""

from __future__ import annotations


class MemoryAgentError(Exception):
    code = "INTERNAL"
    http = 500
    retryable = False

    def __init__(self, message: str = "", **detail):
        super().__init__(message or self.__doc__ or self.code)
        self.message = message or (self.__doc__ or self.code).strip()
        self.detail = detail

    def to_dict(self) -> dict:
        out = {"error": self.code, "message": self.message, "retryable": self.retryable}
        if self.detail:
            out["detail"] = self.detail
        return out


class ScopeRequired(MemoryAgentError):
    """No scope was supplied and the server has no default."""

    code, http = "SCOPE_REQUIRED", 400


class ProceduralWriteRequiresProposal(MemoryAgentError):
    """Procedural memory cannot be written directly. Use memory_propose_procedure."""

    code, http = "PROCEDURAL_WRITE_REQUIRES_PROPOSAL", 400


class InvalidFieldValue(MemoryAgentError):
    """A field carried a value outside its allowed set.

    Exists so a constrained field fails the way every other refusal here does -
    a code plus the allowed values - instead of surfacing a raw SQLite CHECK
    violation. A caller that cannot see the allowed set can only discover it by
    triggering the error, and a DB constraint string is not an API.
    """

    code, http = "INVALID_FIELD_VALUE", 400


class ApprovalSignatureRequired(MemoryAgentError):
    """Approving or rejecting requires an Ed25519 signature from a reviewer key."""

    code, http = "APPROVAL_SIGNATURE_REQUIRED", 403


class ApprovalSignatureInvalid(MemoryAgentError):
    """The signature did not verify, or a field inside it disagreed with the request."""

    code, http = "APPROVAL_SIGNATURE_INVALID", 403


class ApprovalKeyUnknown(MemoryAgentError):
    """key_id is not a known reviewer key, or the key is revoked."""

    code, http = "APPROVAL_KEY_UNKNOWN", 403


class ApprovalChallengeInvalid(MemoryAgentError):
    """The nonce is unknown, expired, or already consumed. Re-list for a fresh one."""

    code, http, retryable = "APPROVAL_CHALLENGE_INVALID", 409, True


class ApprovalCandidateChanged(MemoryAgentError):
    """The candidate changed after it was signed. The reviewer approved something else."""

    code, http = "APPROVAL_CANDIDATE_CHANGED", 409


class NoReviewerKeysConfigured(MemoryAgentError):
    """No reviewer keys are configured, so no approval could ever be verified."""

    code, http = "NO_REVIEWER_KEYS_CONFIGURED", 503


class IdempotencyConflict(MemoryAgentError):
    """The same idempotency key was reused in a scope with a different payload."""

    code, http = "IDEMPOTENCY_CONFLICT", 409


class CycleNotFound(MemoryAgentError):
    """The cycle does not exist, or its working set has expired."""

    code, http = "CYCLE_NOT_FOUND", 404


class BlastRadiusExceeded(MemoryAgentError):
    """The forget selector matched more records than max_records. Nothing changed."""

    code, http = "BLAST_RADIUS_EXCEEDED", 400


class ConfirmRequired(MemoryAgentError):
    """hard_delete needs confirm=true. Nothing changed."""

    code, http = "CONFIRM_REQUIRED", 400


class WriteRateExceeded(MemoryAgentError):
    """A session exceeded its write rate. Usually a runaway loop that ignored a warning."""

    code, http, retryable = "WRITE_RATE_EXCEEDED", 429, True


class VectorUnavailable(MemoryAgentError):
    """sqlite-vec is not loaded and require_vector_extension is true."""

    code, http, retryable = "VECTOR_UNAVAILABLE", 503, True


class StoreBusy(MemoryAgentError):
    """Could not acquire the SQLite write lock within the busy timeout."""

    code, http, retryable = "STORE_BUSY", 503, True


ALL_ERRORS = [
    ScopeRequired, ProceduralWriteRequiresProposal, ApprovalSignatureRequired,
    ApprovalSignatureInvalid, ApprovalKeyUnknown, ApprovalChallengeInvalid,
    ApprovalCandidateChanged, NoReviewerKeysConfigured, IdempotencyConflict,
    CycleNotFound, BlastRadiusExceeded, ConfirmRequired, WriteRateExceeded,
    VectorUnavailable, StoreBusy,
]
