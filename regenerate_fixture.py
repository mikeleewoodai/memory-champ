#!/usr/bin/env python
"""Re-sign the published approval fixture in contracts/examples/records.json.

Run this whenever the signed payload format or the set of fields covered by
candidate_sha256 changes. Those are contract changes, and the fixture is the
golden test for them, so it has to move in step or every implementation that
trusts it starts rejecting genuine approvals.

The reviewer key is derived from a seed published in the fixture itself, on
purpose, so anyone can reproduce this. It is a test vector, not a trust anchor
- a signature only needs the *public* half to verify, so nothing is weakened by
the private half being reproducible. Never put this key in a real policy.yaml.

Edits are surgical string replacements against the raw file rather than a
json.dump round-trip, which would reflow the whole document and bury the four
values that actually changed in a few hundred lines of noise.
"""
import base64
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from memory_agent import approval as A

FIXTURE = ROOT / "contracts" / "examples" / "records.json"
SEED_PHRASE = b"memory-agent example reviewer key -- DO NOT USE"

# Where a signature block lives, and how to reach the record it attests to.
BLOCKS = [
    ("procedural.record",
     lambda d: d["procedural"]["record"],
     lambda d: d["procedural"]["record"]["approval"]["signature"]),
    ("recall_response.records[1].record",
     lambda d: d["recall_response"]["response"]["records"][1]["record"],
     lambda d: d["recall_response"]["response"]["records"][1]["record"]["approval"]["signature"]),
]


def main() -> int:
    seed = hashlib.sha256(SEED_PHRASE).digest()
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    pub = priv.public_key()
    key_id = A.fingerprint(pub)

    raw = FIXTURE.read_text(encoding="utf-8", newline="")
    data = json.loads(raw)

    fixture = data["approval_signature_fixture"]
    if fixture["key_id"] != key_id:
        print(f"  key_id changed: {fixture['key_id']} -> {key_id}")
        raw = raw.replace(fixture["key_id"], key_id)

    for label, get_record, get_sig in BLOCKS:
        record, sig = get_record(data), get_sig(data)

        new_hash = A.candidate_hash(record)
        old_payload = sig["signed_payload"]
        fields = A.parse_payload(old_payload) if old_payload.startswith(A.PAYLOAD_VERSION) \
            else _parse_any_version(old_payload)

        new_payload = A.build_payload(
            scope=fields["scope"], proposal_id=fields["proposal"],
            candidate_sha256=new_hash, decision=fields["decision"],
            reviewer=fields["reviewer"], nonce=fields["nonce"], expires=fields["expires"])
        new_sig = base64.b64encode(priv.sign(new_payload.encode("utf-8"))).decode()

        print(f"\n{label}")
        print(f"  candidate_sha256 {sig['candidate_sha256'][:16]}... -> {new_hash[:16]}...")
        print(f"  payload version  {old_payload.split(chr(10))[0]} -> {A.PAYLOAD_VERSION}")
        print(f"  signature        {sig['sig'][:16]}... -> {new_sig[:16]}...")

        for old, new in ((json.dumps(old_payload)[1:-1], json.dumps(new_payload)[1:-1]),
                         (sig["sig"], new_sig),
                         (sig["candidate_sha256"], new_hash)):
            if old == new:
                continue
            count = raw.count(old)
            assert count >= 1, f"{label}: could not find value to replace: {old[:60]}"
            raw = raw.replace(old, new)

    FIXTURE.write_text(raw, encoding="utf-8", newline="")

    # Prove the file we just wrote actually verifies, the same way a consumer would.
    d = json.loads(FIXTURE.read_text(encoding="utf-8"))
    f = d["approval_signature_fixture"]
    loaded = A.load_public_key(f["reviewer_public_key_openssh"])
    assert A.fingerprint(loaded) == f["key_id"], "fixture key_id does not match its public key"
    for label, get_record, get_sig in BLOCKS:
        record, sig = get_record(d), get_sig(d)
        loaded.verify(base64.b64decode(sig["sig"]), sig["signed_payload"].encode("utf-8"))
        assert A.candidate_hash(record) == sig["candidate_sha256"], f"{label}: hash mismatch"
        assert f"candidate_sha256: {sig['candidate_sha256']}" in sig["signed_payload"], \
            f"{label}: payload hash disagrees with the indexed one"
        assert "content" in A.CANDIDATE_FIELDS
        # the whole point: mutating content must break the hash
        tampered = dict(record, content=record["content"] + " (tampered)")
        assert A.candidate_hash(tampered) != sig["candidate_sha256"], \
            f"{label}: content is STILL not covered by the signature"
    print("\nfixture re-signed, verifies, and content is covered")
    return 0


def _parse_any_version(payload: str) -> dict:
    """Read an older payload well enough to carry its bindings forward."""
    out = {}
    for line in payload.split("\n")[1:]:
        if not line:
            continue
        k, _, v = line.partition(": ")
        out[k] = v
    return out


if __name__ == "__main__":
    raise SystemExit(main())
