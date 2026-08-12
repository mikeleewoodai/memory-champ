"""`memory-agent init` — the one-command setup path.

The install used to be six manual steps with two traps in it: `[all]` omitting
`dev`, and a hand-written interpreter path in the host config. init exists to
remove the hand-written parts, so these tests are mostly about what it must
never do rather than what it produces.
"""

from __future__ import annotations

import json
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


def test_passphrase_requested_without_a_terminal_fails_loudly(home, monkeypatch):
    """Prompting is impossible when stdin is not a tty, and the tempting
    fallback — write the key unencrypted and warn — hands back a key the user
    believes is protected. Refusing is the only safe answer."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert cli.main(["init", "--home", str(home)]) == 1
    assert not (home / "approval").exists()
