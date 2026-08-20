"""`memory-agent init` — the one-command setup path.

The install used to be six manual steps with two traps in it: `[all]` omitting
`dev`, and a hand-written interpreter path in the host config. init exists to
remove the hand-written parts, so these tests are mostly about what it must
never do rather than what it produces.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from memory_agent import approval as A
from memory_agent import cli
from memory_agent.config import Policy


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated runtime dir.

    Every test here points MEMORY_AGENT_HOME at a tmp_path. The real
    ~/.memory-agent holds a live database and the only copy of a reviewer's
    private key, and a test that regenerates that key would silently invalidate
    every approval ever signed with it.
    """
    h = tmp_path / "ma-home"
    monkeypatch.setenv("MEMORY_AGENT_HOME", str(h))
    return h


def _init(home, *extra):
    return cli.main(["init", "--home", str(home), "--no-passphrase", *extra])


def test_init_creates_key_policy_and_nothing_else(home):
    assert _init(home) == 0
    assert sorted(p.name for p in home.iterdir()) == ["approval", "approval.pub", "policy.yaml"]
    # The database is promised for "first write", not created here. init that
    # left an empty db behind would make `stats` report a healthy empty store
    # rather than an absent one.
    assert not (home / "memory.db").exists()


def test_generated_policy_loads_with_a_usable_reviewer(home):
    _init(home, "--id", "ada")
    p = Policy.load()

    assert [r.id for r in p.learning.approval.reviewers] == ["ada"]
    assert p.learning.approval.require_signature is True
    # The key_id in the file must be the fingerprint of the key that was
    # written, or approvals fail with unknown_key against the reviewer's own key.
    pub = A.load_public_key((home / "approval.pub").read_text(encoding="utf-8").strip())
    assert p.learning.approval.reviewers[0].key_id == A.fingerprint(pub)


def test_database_resolves_beside_the_policy_file(home):
    """The generated policy sets no storage.path, so this leans on relative
    paths resolving against the policy file rather than the process cwd. If that
    ever changes, a server started from elsewhere quietly opens a second empty
    database instead of the real one."""
    _init(home)
    assert Policy.load().db_path == str((home / "memory.db").resolve())


def test_rerunning_init_never_regenerates_the_key(home):
    """The one destructive thing init could do, and must not.

    Replacing a reviewer key invalidates every signature made with the old one:
    stored approvals stop verifying and `memory-agent verify` reports them as
    tampered, which is indistinguishable from an actual attack.
    """
    _init(home)
    before = (home / "approval").read_bytes()

    assert _init(home) == 0
    assert (home / "approval").read_bytes() == before


def test_force_rewrites_policy_but_still_keeps_the_key(home):
    """--force is scoped to the policy file. There is deliberately no flag on
    init that destroys a key; `keygen --force` is the explicit way to do that."""
    _init(home)
    key_before = (home / "approval").read_bytes()
    (home / "policy.yaml").write_text("schema_version: '1.0'\n", encoding="utf-8")

    assert _init(home, "--force") == 0
    assert (home / "approval").read_bytes() == key_before
    assert "reviewers" in (home / "policy.yaml").read_text(encoding="utf-8")


def test_existing_policy_is_left_alone_without_force(home):
    _init(home)
    (home / "policy.yaml").write_text("# hand-edited\n", encoding="utf-8")

    assert _init(home) == 0
    assert (home / "policy.yaml").read_text(encoding="utf-8") == "# hand-edited\n"


def test_mcp_block_is_valid_json_naming_this_interpreter(capsys, home):
    """The printed host config must carry an absolute interpreter path, because
    hand-writing that path is the most common way the server ends up
    unstartable — the venv's python is the only one with the package."""
    import sys

    _init(home, "--server-name", "memory-champ")
    out = capsys.readouterr().out
    block = json.loads(out[out.index("{"):])

    server = block["mcpServers"]["memory-champ"]
    assert server["command"] == sys.executable
    assert server["args"] == ["-m", "memory_agent.server"]
    # No env var: Policy.load() finds the conventional path on its own.
    assert "env" not in server


def test_runtime_contracts_are_declared_as_package_data():
    """Guards a bug that shipped: `pip install` produced an unusable package.

    store.py applies contracts/db/schema.sql and server.py builds its tool list
    from contracts/mcp-tools.json. Both used to resolve `parents[2]/contracts`,
    which is the repo root — true for an editable install, false once the
    package sits in site-packages, where it landed on `Lib/contracts` and the
    first database open raised FileNotFoundError.

    The whole test suite missed it because tests run from the checkout, where
    the broken path happens to be the right one. So this asserts the packaging
    declaration instead of the runtime behaviour: drop these from pyproject and
    the wheel silently stops carrying the two files the package cannot run
    without.
    """
    tomllib = pytest.importorskip("tomllib", reason="TOML parsing needs 3.11+")
    root = Path(__file__).resolve().parents[1]
    cfg = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    st = cfg["tool"]["setuptools"]
    assert "memory_agent.contracts" in st["packages"]
    assert "memory_agent.contracts.db" in st["packages"]
    assert st["package-dir"]["memory_agent.contracts"] == "contracts"

    data = st["package-data"]
    assert any(p.endswith(".sql") for p in data["memory_agent.contracts.db"])
    assert any(p.endswith(".json") for p in data["memory_agent.contracts"])


def test_contract_path_finds_both_runtime_files():
    """The two files that must resolve for the package to start at all."""
    from memory_agent.contracts_path import contract_path

    assert contract_path("db", "schema.sql").is_file()
    assert contract_path("mcp-tools.json").is_file()

    with pytest.raises(FileNotFoundError, match="cannot start"):
        contract_path("nope", "missing.sql")


def test_noninteractive_refuses_instead_of_prompting(home, monkeypatch):
    """Prompting is impossible with no human present, and the tempting fallback —
    write the key unencrypted and warn — hands back a key the user believes is
    protected. Refusing is the only safe answer."""
    monkeypatch.setenv("MEMORY_AGENT_NONINTERACTIVE", "1")

    with pytest.raises(SystemExit) as exc:
        cli.main(["init", "--home", str(home)])
    assert "--no-passphrase" in str(exc.value), "must say how to proceed without a terminal"
    assert not (home / "approval").exists()


def test_isatty_alone_is_not_trusted(home, monkeypatch):
    """The bug this whole mechanism exists for.

    An agent harness hands the process a pty, so `sys.stdin.isatty()` returns
    True with nobody behind it. Windows `getpass` then writes its prompt with
    `msvcrt.putwch` — to the console device, not stdout — so the caller sees
    nothing at all, and `msvcrt.getwch()` blocks forever. isatty() saying True
    must therefore not be enough to start a prompt that could hang: the
    explicit env var overrides it.
    """
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setenv("MEMORY_AGENT_NONINTERACTIVE", "1")
    assert cli._noninteractive() is True


def test_passphrase_from_environment(home, monkeypatch):
    monkeypatch.setenv("MEMORY_AGENT_PASSPHRASE", "correct horse battery")
    assert cli.main(["init", "--home", str(home)]) == 0

    assert cli._is_encrypted(home / "approval"), "env passphrase must actually encrypt the key"
    # And it must round-trip: a key nobody can reopen is worse than no key.
    loaded = cli._load_key(str(home / "approval"))
    assert A.fingerprint(loaded.public_key()) == A.fingerprint(
        A.load_public_key((home / "approval.pub").read_text(encoding="utf-8").strip()))


def test_passphrase_from_file(home, tmp_path):
    pf = tmp_path / "pw.txt"
    pf.write_text("from-a-file\nignored second line\n", encoding="utf-8")

    assert cli.main(["init", "--home", str(home), "--passphrase-file", str(pf)]) == 0
    assert cli._is_encrypted(home / "approval")


def test_empty_passphrase_file_is_an_error_not_an_empty_passphrase(home, tmp_path):
    """An empty file must not silently become an unencrypted key."""
    pf = tmp_path / "pw.txt"
    pf.write_text("\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="empty"):
        cli.main(["init", "--home", str(home), "--passphrase-file", str(pf)])
    assert not (home / "approval").exists()


def test_require_passphrase_matches_how_the_key_was_actually_written(home, monkeypatch):
    """policy.yaml must not claim a passphrase the key does not have.

    OpenSSH keys carry the same header encrypted or not, so this is detected by
    trying to load one — a substring check on the file would always say "not
    encrypted" and write the wrong flag.
    """
    monkeypatch.setenv("MEMORY_AGENT_PASSPHRASE", "pw")
    cli.main(["init", "--home", str(home)])
    assert "require_passphrase: true" in (home / "policy.yaml").read_text(encoding="utf-8")

    # Re-running against the existing encrypted key must keep saying true.
    monkeypatch.delenv("MEMORY_AGENT_PASSPHRASE")
    cli.main(["init", "--home", str(home), "--force"])
    assert "require_passphrase: true" in (home / "policy.yaml").read_text(encoding="utf-8")


def test_install_claude_desktop_merges_without_disturbing_anything_else(tmp_path, capsys):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"other": {"command": "node", "args": ["x.js"]}},
        "globalShortcut": "Ctrl+Space",
    }), encoding="utf-8")

    assert cli.main(["install-claude-desktop", "--path", str(cfg)]) == 0
    written = json.loads(cfg.read_text(encoding="utf-8"))

    assert written["mcpServers"]["other"] == {"command": "node", "args": ["x.js"]}
    assert written["globalShortcut"] == "Ctrl+Space", "unrelated keys must survive"
    assert written["mcpServers"]["memory-champ"]["command"] == sys.executable
    assert list(tmp_path.glob("*.bak-*")), "the original must be backed up before writing"

    # Idempotent: a second run changes nothing.
    before = cfg.read_text(encoding="utf-8")
    assert cli.main(["install-claude-desktop", "--path", str(cfg)]) == 0
    assert cfg.read_text(encoding="utf-8") == before


def test_install_claude_desktop_refuses_to_clobber_unparseable_json(tmp_path):
    """That file holds every other MCP server the user has. Failing to parse it
    is far likelier to mean we do not understand it than that it is corrupt, and
    rewriting it would destroy the lot."""
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text('{ "mcpServers": {"a": {}}, <<< not json\n', encoding="utf-8")
    before = cfg.read_bytes()

    assert cli.main(["install-claude-desktop", "--path", str(cfg)]) == 1
    assert cfg.read_bytes() == before
    assert not list(tmp_path.glob("*.bak-*")), "no backup either - nothing was written"
