"""memory-agent CLI — key management and the review queue.

This is where the reviewer's private key is used, and the only place it is ever
read. The server never sees it. Approving from a chat session is fine because
the signature, not the caller, is what the server trusts; this command exists so
producing that signature is one line rather than a chore.

    memory-agent init                       # key + policy + host config, once
    memory-agent fingerprint ~/.memory-agent/approval.pub
    memory-agent review list --scope acme.crm
    memory-agent review approve p_01J9X2 --reviewer me --key ~/.memory-agent/approval
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import approval as A
from .config import Policy, default_home
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
PASSPHRASE_ENV = "MEMORY_AGENT_PASSPHRASE"
NONINTERACTIVE_ENV = "MEMORY_AGENT_NONINTERACTIVE"
PROMPT_TIMEOUT_ENV = "MEMORY_AGENT_PROMPT_TIMEOUT"
DEFAULT_PROMPT_TIMEOUT = 60.0

_NO_PROMPT_HELP = (
    "  Supply the passphrase without a terminal:\n"
    "    --no-passphrase            write/read the key unencrypted\n"
    "    --passphrase-file PATH     read it from the first line of a file\n"
    f"    {PASSPHRASE_ENV}=...  read it from the environment"
)


def _noninteractive() -> bool:
    """Whether to refuse to prompt at all.

    `sys.stdin.isatty()` is consulted but NOT trusted on its own: agent
    harnesses and CI runners routinely hand the process a pty, so isatty()
    answers True with no human behind it. The env vars are the reliable signal;
    isatty only catches plain redirection.
    """
    if os.environ.get(NONINTERACTIVE_ENV) or os.environ.get("CI"):
        return True
    try:
        return not sys.stdin.isatty()
    except (ValueError, AttributeError):
        return True


def _prompt_hidden(prompt: str) -> bytes:
    """getpass, with a warning first and a deadline after.

    Two failure modes this exists for, both hit on Windows:

    1. `getpass` writes its prompt with `msvcrt.putwch` - straight to the
       console device, not to stdout or stderr. Anything capturing pipes sees
       *nothing*, so an unanswered prompt looks like a silent hang with no clue
       what is being asked for. The notice below goes to stdout, flushed,
       before the prompt exists.
    2. `msvcrt.getwch()` then blocks forever. A daemon thread and a deadline
       turn "hangs until killed" into a bounded failure that explains itself.
       The thread is abandoned rather than joined: it is parked on a console
       read that will never return, and the process is on its way out anyway.
    """
    if _noninteractive():
        raise SystemExit(
            f"{prompt.strip().rstrip(':')} is needed, but this is not an "
            f"interactive terminal.\n{_NO_PROMPT_HELP}")

    try:
        timeout = float(os.environ.get(PROMPT_TIMEOUT_ENV) or DEFAULT_PROMPT_TIMEOUT)
    except ValueError:
        timeout = DEFAULT_PROMPT_TIMEOUT

    print(f"\n[prompt] {prompt.strip()}\n"
          f"         Input is hidden. On Windows this prompt goes to the console\n"
          f"         device, so an automated caller will not see it at all.\n"
          f"{_NO_PROMPT_HELP}\n", flush=True)

    box: list[str] = []

    def _read() -> None:
        try:
            box.append(getpass.getpass(prompt))
        except Exception:  # noqa: BLE001 - surfaced as an empty box below
            pass

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive() or not box:
        print(f"\nNo passphrase after {timeout:.0f}s - giving up rather than hanging.\n"
              f"{_NO_PROMPT_HELP}", file=sys.stderr, flush=True)
        sys.stderr.flush()
        sys.stdout.flush()
        os._exit(1)
    return box[0].encode()


def _is_encrypted(key_path: Path) -> bool:
    """Whether an OpenSSH private key needs a passphrase.

    `load_private_key` raises TypeError when a password is required, which is
    the only reliable test: an OpenSSH key file carries the identical
    "BEGIN OPENSSH PRIVATE KEY" header whether or not it is encrypted, so there
    is nothing in the text to match on.
    """
    try:
        A.load_private_key(Path(key_path).read_bytes())
        return False
    except TypeError:
        return True
    except Exception:  # noqa: BLE001 - unreadable key is not this function's call
        return False


def _supplied_passphrase(args) -> bytes | None:
    """A passphrase from --passphrase-file or the environment, without prompting."""
    path = getattr(args, "passphrase_file", None) if args is not None else None
    if path:
        pw = Path(path).expanduser().read_text(encoding="utf-8").split("\n", 1)[0].strip()
        if not pw:
            raise SystemExit(f"{path} is empty - no passphrase on its first line.")
        return pw.encode()
    from_env = os.environ.get(PASSPHRASE_ENV)
    return from_env.encode() if from_env else None


def resolve_passphrase(args, *, confirm: bool, purpose: str) -> bytes | None:
    """The one place that decides where a new key's passphrase comes from.

    Order: an explicit --no-passphrase, then --passphrase-file, then the
    environment, then a prompt. Automation is expected to use one of the first
    three - the prompt is an interactive convenience, not the contract.
    """
    if getattr(args, "no_passphrase", False):
        return None
    supplied = _supplied_passphrase(args)
    if supplied is not None:
        return supplied

    pw = _prompt_hidden(f"{purpose} passphrase: ")
    if confirm and pw != _prompt_hidden("confirm passphrase: "):
        raise SystemExit("passphrases did not match")
    return pw


def _load_key(path: str, args=None) -> "A.Ed25519PrivateKey":
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
    supplied = _supplied_passphrase(args)
    for left in range(PASSPHRASE_ATTEMPTS - 1, -1, -1):
        if supplied is not None:
            # A passphrase given by file or env gets one attempt, not three.
            # Retrying identical bytes twice more only buries the real error.
            pw, left = supplied, 0
        else:
            pw = _prompt_hidden(f"passphrase for {path}: ")
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


def _write_keypair(out: Path, pw: bytes | None):
    """Generate an Ed25519 reviewer key at `out`, returning the private key.

    Takes an already-resolved passphrase rather than a bool, so the decision
    about where it came from lives in resolve_passphrase alone and this
    function behaves identically whether it was typed, read from a file, or
    taken from the environment.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    # Checked before anything is written, not after. Failing afterwards leaves a
    # key on disk that is not protected the way the caller asked for.
    if pw:
        _require_bcrypt("Encrypting a private key")
    out.parent.mkdir(parents=True, exist_ok=True)

    priv = Ed25519PrivateKey.generate()
    enc = serialization.NoEncryption()
    if pw:
        enc = serialization.BestAvailableEncryption(pw)
    out.write_bytes(priv.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, enc))
    out.chmod(0o600)
    Path(f"{out}.pub").write_text(A.openssh_public(priv.public_key()) + "\n", encoding="utf-8")
    return priv


def cmd_keygen(args) -> int:
    out = Path(args.path).expanduser()
    if out.exists() and not args.force:
        print(f"{out} already exists (use --force to overwrite)", file=sys.stderr)
        return 1

    pw = resolve_passphrase(args, confirm=True, purpose="new reviewer key")
    priv = _write_keypair(out, pw)
    pub_text = A.openssh_public(priv.public_key())
    print(f"private key: {out}  (mode 600 — this never goes in policy.yaml)")
    print(f"public key:  {out}.pub\n")
    print("Add to policy.yaml under learning.approval.reviewers:\n")
    print(f"      - id: {args.id}")
    print("        alg: ed25519")
    print(f'        public_key: "{pub_text}"')
    print(f'        key_id: "{A.fingerprint(priv.public_key())}"')
    return 0


POLICY_TEMPLATE = """\
# memory-agent policy — written by `memory-agent init` on {when}.
#
# Only the reviewer key is set here. Every other value falls back to the
# built-in defaults, which mirror contracts/policy.example.yaml exactly — so a
# short file like this is a complete configuration, not a stub. Copy that
# example over this file when you want the full annotated set of knobs.
#
# The database lives beside this file: storage.path defaults to ./memory.db and
# relative paths resolve against the policy file, not the working directory.

schema_version: "1.0"

learning:
  approval:
    require_signature: true
    # Require the passphrase on every approval rather than caching it. Set to
    # match how the key below was created.
    require_passphrase: {require_passphrase}
    reviewers:
      - id: {reviewer_id}
        alg: ed25519
        # Public half only. The private key never appears in this file.
        public_key: "{public_key}"
        key_id: "{key_id}"
        added_at: "{when}"
"""


def _mcp_block(server_name: str = "memory-champ") -> str:
    """The host config, with this interpreter's absolute path baked in.

    sys.executable is the point: it is the interpreter that just ran init, which
    is by construction the one with the package installed. Hand-writing this
    path is the single most common way the server ends up unstartable.
    """
    return json.dumps(
        {"mcpServers": {server_name: {"command": sys.executable,
                                      "args": ["-m", "memory_agent.server"]}}},
        indent=2)


def cmd_init(args) -> int:
    """One command from nothing to a running server: key, policy, host config."""
    home = Path(args.home).expanduser() if args.home else default_home()
    key_path = home / "approval"
    policy_path = home / "policy.yaml"

    home.mkdir(parents=True, exist_ok=True)

    # A reviewer key is never regenerated implicitly. Replacing it invalidates
    # every signature ever made with the old one - approvals stop verifying and
    # `memory-agent verify` starts reporting them as tampered. Keeping it is the
    # only safe default, and --force is scoped to the policy file for the same
    # reason: there is no flag here that destroys a key.
    if key_path.exists():
        pub_text = Path(f"{key_path}.pub").read_text(encoding="utf-8").strip()
        pub = A.load_public_key(pub_text)
        key_id = A.fingerprint(pub)
        # Whether the existing key is encrypted decides require_passphrase below.
        # Detected by trying to load it without one: OpenSSH keys carry the same
        # "BEGIN OPENSSH PRIVATE KEY" header either way, so there is no header
        # string to grep for - a load that demands a password is the only signal.
        pw = b"x" if _is_encrypted(key_path) else None
        print(f"reviewer key: {key_path}  (already present, kept)")
    else:
        # resolve_passphrase handles the no-terminal case itself: --no-passphrase,
        # --passphrase-file, or the env var short-circuit the prompt entirely, and
        # a prompt that nobody can answer fails on a deadline instead of hanging.
        pw = resolve_passphrase(args, confirm=True, purpose="new reviewer key")
        priv = _write_keypair(key_path, pw)
        pub_text = A.openssh_public(priv.public_key())
        key_id = A.fingerprint(priv.public_key())
        how = "passphrase-protected" if pw else "UNENCRYPTED"
        print(f"reviewer key: {key_path}  (created, mode 600, {how})")

    if policy_path.exists() and not args.force:
        print(f"policy:       {policy_path}  (already present, left alone — --force to rewrite)")
    else:
        policy_path.write_text(POLICY_TEMPLATE.format(
            when=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            reviewer_id=args.id,
            public_key=pub_text,
            key_id=key_id,
            require_passphrase=str(bool(pw)).lower(),
        ), encoding="utf-8")
        print(f"policy:       {policy_path}  (written, reviewer already filled in)")

    print(f"database:     {home / 'memory.db'}  (created on first write)")
    print(f"\nAdd this to your MCP host config — no env var needed, {policy_path.name}\n"
          f"is found at the conventional path:\n")
    print(_mcp_block(args.server_name))
    return 0


def claude_desktop_config_path() -> Path:
    """Where Claude Desktop keeps its MCP config, per platform."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "Claude" / "claude_desktop_config.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "Claude" / "claude_desktop_config.json"


def cmd_install_claude_desktop(args) -> int:
    """Merge this server into claude_desktop_config.json.

    Editing a file the user owns and did not hand us, so the rules are: back it
    up first, never write over JSON we could not parse, and touch nothing but
    our own key. A config that fails to parse is far more likely to be a config
    we do not understand than one that is corrupt, and overwriting it would
    destroy every other server the user has configured.
    """
    path = Path(args.path).expanduser() if args.path else claude_desktop_config_path()
    name = args.server_name
    entry = {"command": sys.executable, "args": ["-m", "memory_agent.server"]}

    if path.exists():
        raw = path.read_text(encoding="utf-8")
        try:
            config = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            print(f"{path} is not valid JSON ({exc}).\n"
                  f"Refusing to overwrite it - fix or move it first. Every other "
                  f"MCP server you have configured lives in that file.",
                  file=sys.stderr)
            return 1
        if not isinstance(config, dict):
            print(f"{path} is JSON but not an object; refusing to touch it.", file=sys.stderr)
            return 1
    else:
        raw, config = "", {}

    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        print(f'{path}: "mcpServers" is not an object; refusing to touch it.', file=sys.stderr)
        return 1

    previous = servers.get(name)
    if previous == entry:
        print(f"{name} is already configured in {path} - nothing to do.")
        return 0
    servers[name] = entry
    merged = json.dumps(config, indent=2) + "\n"

    if args.dry_run:
        print(f"--- would write {path} ---")
        print(merged, end="")
        return 0

    if raw:
        backup = path.with_suffix(path.suffix + f".bak-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
        backup.write_text(raw, encoding="utf-8")
        print(f"backup:  {backup}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(merged, encoding="utf-8")

    verb = "updated" if previous is not None else "added"
    others = sorted(k for k in servers if k != name)
    print(f"config:  {path}")
    print(f"{verb}:  {name} -> {sys.executable} -m memory_agent.server")
    print(f"kept:    {', '.join(others) if others else '(no other servers)'}")
    print("\nRestart Claude Desktop for it to pick this up.")
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
        priv = _load_key(args.key, args)
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

    i = sub.add_parser(
        "init", help="set up key, policy, and host config in one step")
    i.add_argument("--id", default="me", help="your reviewer id")
    i.add_argument("--home", help=f"runtime dir (default: {default_home()})")
    i.add_argument("--no-passphrase", action="store_true",
                   help="write the private key unencrypted (use for automation)")
    i.add_argument("--server-name", default="memory-champ",
                   help="name for the MCP server in the printed host config")
    i.add_argument("--force", action="store_true",
                   help="rewrite policy.yaml; never touches an existing key")
    i.set_defaults(func=cmd_init)

    k = sub.add_parser("keygen", help="create a reviewer signing key")
    k.add_argument("path", nargs="?", default="~/.memory-agent/approval")
    k.add_argument("--id", default="me", help="reviewer id to put in policy.yaml")
    k.add_argument("--passphrase", action="store_true", help="encrypt the private key")
    k.add_argument("--no-passphrase", action="store_true",
                   help="write the private key unencrypted (use for automation)")
    k.add_argument("--force", action="store_true")
    k.set_defaults(func=cmd_keygen)

    for _p in (i, k):
        _p.add_argument("--passphrase-file",
                        help="read the passphrase from this file's first line")


    d = sub.add_parser("install-claude-desktop",
                       help="merge this server into claude_desktop_config.json")
    d.add_argument("--server-name", default="memory-champ")
    d.add_argument("--path", help="config file to edit (default: the platform location)")
    d.add_argument("--dry-run", action="store_true",
                   help="print the merged config instead of writing it")
    d.set_defaults(func=cmd_install_claude_desktop)

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
        rd.add_argument("--passphrase-file",
                        help="read the key's passphrase from this file's first line")
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
