"""memory-agent CLI — key management and the review queue.

This is where the reviewer's private key is used, and the only place it is ever
read. The server never sees it. Approving from a chat session is fine because
the signature, not the caller, is what the server trusts; this command exists so
producing that signature is one line rather than a chore.

    memory-agent keygen ~/.memory-agent/approval
    memory-agent fingerprint ~/.memory-agent/approval.pub
    memory-agent review list --scope acme.crm
    memory-agent review approve p_01J9X2 --reviewer mike --key ~/.memory-agent/approval
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from . import approval as A
from .config import Policy
from .errors import MemoryAgentError
from .service import MemoryService


def _require_bcrypt(what: str) -> None:
    """OpenSSH private-key encryption uses bcrypt's KDF, and `cryptography` does
    not vendor it. Without it, both writing and reading a passphrase-protected
    key raise UnsupportedAlgorithm from deep inside the serialisation code.

    This is a hard stop, never a fallback. The tempting 'degrade gracefully'
    move - write the key unencrypted and warn - would hand back a private key
    the user believes is protected, which is worse than no key at all.
    """
    try:
        import bcrypt  # noqa: F401,PLC0415
    except ImportError:
        raise SystemExit(
            f"{what} needs the `bcrypt` package, which is missing.\n"
            "  pip install bcrypt\n"
            "It is a declared dependency, so a fresh install has it; seeing this "
            "means the environment drifted.") from None


PASSPHRASE_ATTEMPTS = 3


def _load_key(path: str) -> "A.Ed25519PrivateKey":
    data = Path(path).expanduser().read_bytes()
    try:
        return A.load_private_key(data)
    except TypeError:
        # Encrypted key: prompt rather than failing. require_passphrase in policy
        # is the setting that makes this the norm. Reaching here also *proves*
        # the file parsed as a well-formed encrypted key, which is what lets the
        # handler below be certain a later failure is the passphrase.
        pass

    _require_bcrypt("Reading a passphrase-protected key")
    for left in range(PASSPHRASE_ATTEMPTS - 1, -1, -1):
        pw = getpass.getpass(f"passphrase for {path}: ").encode()
        try:
            return A.load_private_key(data, password=pw)
        except ValueError:
            # cryptography reports a failed decrypt as "Corrupt data: broken
            # checksum". Raw, that is a traceback telling someone who mistyped
            # that their signing key is damaged - which sends them looking for a
            # backup instead of typing it again. The key parsed fine seconds ago.
            if left:
                print(f"wrong passphrase, {left} attempt(s) left", file=sys.stderr)
                continue
            raise SystemExit(
                f"wrong passphrase for {path}.\n"
                f"The key file itself is intact - it parsed as a well-formed "
                f"encrypted key before the passphrase was tried.\n"
                f"  memory-agent fingerprint {path}.pub    # confirm which key this is"
            ) from None


def cmd_keygen(args) -> int:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    out = Path(args.path).expanduser()
    if out.exists() and not args.force:
        print(f"{out} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    # Checked before the passphrase prompt, not after. Failing afterwards asks
    # the user to type a passphrase twice and then throws it away.
    if args.passphrase:
        _require_bcrypt("Encrypting a private key")
    out.parent.mkdir(parents=True, exist_ok=True)

    priv = Ed25519PrivateKey.generate()
    enc = serialization.NoEncryption()
    if args.passphrase:
        pw = getpass.getpass("passphrase: ").encode()
        if pw != getpass.getpass("confirm: ").encode():
            print("passphrases did not match", file=sys.stderr)
            return 1
        enc = serialization.BestAvailableEncryption(pw)
    out.write_bytes(priv.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, enc))
    out.chmod(0o600)

    pub_text = A.openssh_public(priv.public_key())
    Path(f"{out}.pub").write_text(pub_text + "\n", encoding="utf-8")
    print(f"private key: {out}  (mode 600 — this never goes in policy.yaml)")
    print(f"public key:  {out}.pub\n")
    print("Add to policy.yaml under learning.approval.reviewers:\n")
    print(f"      - id: {args.id}")
    print("        alg: ed25519")
    print(f'        public_key: "{pub_text}"')
    print(f'        key_id: "{A.fingerprint(priv.public_key())}"')
    return 0


def cmd_fingerprint(args) -> int:
    text = Path(args.path).expanduser().read_text(encoding="utf-8")
    pub = A.load_public_key(text)
    print(A.fingerprint(pub))
    return 0


def _service(args) -> MemoryService:
    return MemoryService(Policy.load(getattr(args, "policy", None)))


def cmd_review_list(args) -> int:
    svc = _service(args)
    try:
        out = svc.review_proposals(action="list", scope=args.scope, limit=args.limit)
    finally:
        svc.close()
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    if not out["proposals"]:
        print("nothing pending")
        return 0
    for p in out["proposals"]:
        c = p["candidate"]
        print(f"\n{p['proposal_id']}  [{p['kind']}]  {p['scope']}  from {p['proposed_by']} at {p['proposed_at']}")
        print(f"  why: {p['rationale']}")
        if c.get("trigger"):
            print(f"  when: {c['trigger']}")
        for step in c.get("steps", []):
            print(f"    {step['n']}. {step['instruction']}")
        if c.get("content") and not c.get("trigger"):
            print(f"  fact: {c['content']}")
    print(f"\n{out['queue_depth']} pending. Approve with:")
    print(f"  memory-agent review approve <id> --reviewer <you> --key ~/.memory-agent/approval")
    return 0


def _decide(args, decision: str) -> int:
    svc = _service(args)
    try:
        listing = svc.review_proposals(action="list", scope=args.scope, limit=200)
        wanted = {p["proposal_id"]: p for p in listing["proposals"]}
        priv = _load_key(args.key)
        key_id = A.fingerprint(priv.public_key())

        signatures, ids = [], []
        for pid in args.proposal_ids:
            entry = wanted.get(pid)
            if entry is None:
                print(f"{pid}: not pending, skipping", file=sys.stderr)
                continue
            # Sign the bytes the server just issued, with the reviewer name and
            # decision substituted in. The nonce inside ties this signature to
            # this listing, so it cannot be reused later or against another
            # proposal.
            payload = A.build_payload(
                scope=entry["scope"], proposal_id=pid,
                candidate_sha256=entry["candidate_sha256"], decision=decision,
                reviewer=args.reviewer, nonce=entry["nonce"],
                expires=entry["challenge_expires_at"])
            signatures.append({"proposal_id": pid, "alg": "ed25519", "key_id": key_id,
                               "signed_payload": payload, "sig": A.sign_payload(priv, payload)})
            ids.append(pid)

        if not ids:
            print("nothing to do")
            return 1
        result = svc.review_proposals(
            action=decision, proposal_ids=ids, reviewed_by=args.reviewer,
            signatures=signatures, note=args.note)
    except MemoryAgentError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1
    finally:
        svc.close()

    for a in result.get("approved", []):
        print(f"approved {a['proposal_id']} -> record {a['record_id']}  (signature verified)")
    for pid in result.get("rejected", []):
        print(f"rejected {pid}")
    for s in result.get("skipped", []):
        print(f"skipped {s['proposal_id']}: {s['reason']} — {s.get('detail','')}", file=sys.stderr)
    return 1 if result.get("skipped") else 0


def cmd_verify(args) -> int:
    """Re-check every stored approval. Anything listed must be treated as
    unapproved until re-signed."""
    svc = _service(args)
    try:
        problems = svc.reverify_approvals(args.scope)
    finally:
        svc.close()
    if not problems:
        print("all stored approvals verify")
        return 0
    print(f"{len(problems)} approval(s) FAILED verification:\n", file=sys.stderr)
    for p in problems:
        print(f"  {p['record_id']} ({p['scope']}): {p['problem']}", file=sys.stderr)
        print(f"    signed hash {p['signed_hash'][:16]}… current {p['current_hash'][:16]}…", file=sys.stderr)
    return 1


def cmd_stats(args) -> int:
    svc = _service(args)
    try:
        out = svc.stats(scope=args.scope, include_scope_breakdown=True, include_top_accessed=5)
    finally:
        svc.close()
    if args.json:
        print(json.dumps(out, indent=2))
        return 0
    c = out["counts"]
    print(f"records: {c['total']}  " + "  ".join(f"{k}={v}" for k, v in c["by_type"].items()))
    print(f"status:  " + "  ".join(f"{k}={v}" for k, v in c["by_status"].items()))
    print(f"vectors: {out['embedding']['model']} coverage {out['embedding']['coverage']:.0%}")
    print(f"queue:   {out['queue']['pending_proposals']} pending")
    for w in out["warnings"]:
        print(f"  ! {w}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="memory-agent", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--policy", help="path to policy.yaml (or set MEMORY_AGENT_POLICY)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("keygen", help="create a reviewer signing key")
    k.add_argument("path", nargs="?", default="~/.memory-agent/approval")
    k.add_argument("--id", default="me", help="reviewer id to put in policy.yaml")
    k.add_argument("--passphrase", action="store_true", help="encrypt the private key")
    k.add_argument("--force", action="store_true")
    k.set_defaults(func=cmd_keygen)

    f = sub.add_parser("fingerprint", help="print a public key's key_id")
    f.add_argument("path")
    f.set_defaults(func=cmd_fingerprint)

    r = sub.add_parser("review", help="the approval queue")
    rsub = r.add_subparsers(dest="review_cmd", required=True)

    rl = rsub.add_parser("list", help="show pending proposals")
    rl.add_argument("--scope")
    rl.add_argument("--limit", type=int, default=50)
    rl.add_argument("--json", action="store_true")
    rl.set_defaults(func=cmd_review_list)

    for name, decision in (("approve", "approve"), ("reject", "reject")):
        rd = rsub.add_parser(name, help=f"{name} proposals (signs with your key)")
        rd.add_argument("proposal_ids", nargs="+")
        rd.add_argument("--reviewer", required=True, help="your reviewer id, as in policy.yaml")
        rd.add_argument("--key", default="~/.memory-agent/approval", help="private key path")
        rd.add_argument("--scope")
        rd.add_argument("--note")
        rd.set_defaults(func=lambda a, d=decision: _decide(a, d))

    v = sub.add_parser("verify", help="re-check every stored approval signature")
    v.add_argument("--scope")
    v.set_defaults(func=cmd_verify)

    s = sub.add_parser("stats", help="memory health")
    s.add_argument("--scope")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_stats)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
