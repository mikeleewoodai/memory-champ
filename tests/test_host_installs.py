"""`install-codex` and `install-antigravity`.

Requirements I11 and I12 in `integrations/README.md`. Both hosts get the same
skill as Claude Code, so the copy rules (I7, I8) are not re-proved here beyond
one round trip each; what is new is the server registration.

Every test pins HOME, CODEX_HOME and MEMORY_AGENT_HOME to tmp_path and passes
explicit destinations. The defaults point at the real ~/.agents, ~/.gemini and
~/.codex - a test that forgot a flag would otherwise write into them.
"""

from __future__ import annotations

import json
import sys

import pytest

from memory_agent import cli
from memory_agent.skill_path import skill_files, skill_path


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("MEMORY_AGENT_HOME", str(tmp_path / "ma"))
    monkeypatch.delenv("MEMORY_AGENT_POLICY", raising=False)
    policy = tmp_path / "ma" / "policy.yaml"
    policy.parent.mkdir()
    policy.write_text("storage:\n  path: memory.db\n", encoding="utf-8")
    return tmp_path, policy.resolve()


def _codex(skills, *extra):
    return cli.main(["install-codex", "--path", str(skills), *extra])


def _antigravity(cfg, skills, *extra):
    return cli.main(["install-antigravity", "--path", str(cfg),
                     "--skills-path", str(skills), *extra])


def _same_skill(dest):
    source = skill_path()
    return all((dest / rel).read_bytes() == (source / rel).read_bytes()
               for rel in skill_files(source))


# --- I11: install-codex -------------------------------------------------------

def test_codex_copies_the_skill_and_prints_a_registration_for_this_install(env, capsys):
    tmp, policy = env
    skills = tmp / "agents-skills"

    assert _codex(skills) == 0
    assert _same_skill(skills / "memory-agent")

    out = capsys.readouterr().out
    # The printed command is the whole registration; every part of it must
    # name this install, or Codex starts a different interpreter or store.
    assert f"codex mcp add memory-champ --env MEMORY_AGENT_POLICY={policy}" in out \
        or f'"MEMORY_AGENT_POLICY={policy}"' in out
    assert sys.executable in out
    assert "-m memory_agent.server" in out
    assert "[mcp_servers.memory-champ]" in out
    assert "[mcp_servers.memory-champ.env]" in out


def test_codex_toml_block_parses_to_the_same_entry(env, capsys):
    tomllib = pytest.importorskip("tomllib", reason="TOML parsing needs 3.11+")
    tmp, policy = env
    _codex(tmp / "s")

    out = capsys.readouterr().out
    block = out[out.index("[mcp_servers.memory-champ]"):]
    entry = tomllib.loads(block)["mcp_servers"]["memory-champ"]
    assert entry == {"command": sys.executable, "args": ["-m", "memory_agent.server"],
                     "env": {"MEMORY_AGENT_POLICY": str(policy)}}


def test_codex_never_writes_config_toml(env):
    tmp, _ = env
    codex_home = tmp / "home" / ".codex"
    codex_home.mkdir()
    cfg = codex_home / "config.toml"
    cfg.write_text('model = "x"\n', encoding="utf-8")
    before = cfg.read_bytes()

    assert _codex(tmp / "s") == 0
    assert cfg.read_bytes() == before
    assert sorted(p.name for p in codex_home.iterdir()) == ["config.toml"], "no backup either"


def test_codex_reports_an_existing_registration(env, capsys):
    pytest.importorskip("tomllib", reason="TOML parsing needs 3.11+")
    tmp, _ = env
    codex_home = tmp / "home" / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        "[mcp_servers.memory-champ]\ncommand = 'srv.exe'\n\n"
        "[mcp_servers.memory-champ.env]\nMEMORY_AGENT_POLICY = 'p.yaml'\n", encoding="utf-8")

    _codex(tmp / "s")
    out = capsys.readouterr().out
    assert "already registered" in out and "srv.exe" in out and "p.yaml" in out


def test_codex_is_idempotent_and_check_reports_drift(env, capsys):
    tmp, _ = env
    skills = tmp / "s"

    assert _codex(skills, "--check") == 2, "absent before install"
    _codex(skills)
    capsys.readouterr()
    assert _codex(skills) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert _codex(skills, "--check") == 0

    (skills / "memory-agent" / "SKILL.md").write_text("edited\n", encoding="utf-8")
    assert _codex(skills, "--check") == 1


def test_codex_dry_run_writes_nothing(env):
    tmp, _ = env
    assert _codex(tmp / "s", "--dry-run") == 0
    assert not (tmp / "s").exists()


@pytest.mark.parametrize("command", ["install-codex", "install-antigravity"])
def test_refuses_without_a_policy_to_pin(env, capsys, command):
    """A host config with no policy path sends the server to ./memory.db in
    whatever directory the host starts it from - an empty store that looks
    healthy (B-4). Refuse, and say how to get one."""
    tmp, policy = env
    policy.unlink()

    argv = [command, "--path", str(tmp / "cfg"),
            *(["--skills-path", str(tmp / "s")] if command == "install-antigravity" else [])]
    assert cli.main(argv) == 1
    assert "memory-agent init" in capsys.readouterr().err
    assert not (tmp / "cfg").exists() and not (tmp / "s").exists()


def test_root_policy_flag_wins(env, capsys):
    tmp, _ = env
    other = tmp / "elsewhere" / "policy.yaml"
    other.parent.mkdir()
    other.write_text("{}\n", encoding="utf-8")

    assert cli.main(["--policy", str(other), "install-codex", "--path", str(tmp / "s")]) == 0
    assert str(other.resolve()) in capsys.readouterr().out


# --- I12: install-antigravity -------------------------------------------------

def test_antigravity_fills_an_empty_config_and_installs_the_skill(env):
    """Antigravity ships mcp_config.json as 0 bytes. That is an empty config,
    not a corrupt one."""
    tmp, policy = env
    cfg = tmp / "mcp_config.json"
    cfg.write_bytes(b"")

    assert _antigravity(cfg, tmp / "s") == 0
    entry = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["memory-champ"]
    assert entry == {"command": sys.executable, "args": ["-m", "memory_agent.server"],
                     "env": {"MEMORY_AGENT_POLICY": str(policy)}}
    assert _same_skill(tmp / "s" / "memory-agent")


def test_antigravity_keeps_other_servers_and_backs_up(env):
    tmp, _ = env
    cfg = tmp / "mcp_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"serverUrl": "https://x"}},
                               "theme": "dark"}), encoding="utf-8")

    assert _antigravity(cfg, tmp / "s") == 0
    written = json.loads(cfg.read_text(encoding="utf-8"))
    assert written["mcpServers"]["other"] == {"serverUrl": "https://x"}
    assert written["theme"] == "dark"
    assert list(tmp.glob("mcp_config.json.bak-*")), "the original must be backed up first"


def test_antigravity_is_idempotent_and_check_agrees(env, capsys):
    tmp, _ = env
    cfg, skills = tmp / "mcp_config.json", tmp / "s"

    assert _antigravity(cfg, skills, "--check") == 2
    _antigravity(cfg, skills)
    before = cfg.read_bytes()
    capsys.readouterr()

    assert _antigravity(cfg, skills) == 0
    assert cfg.read_bytes() == before
    assert capsys.readouterr().out.count("nothing to do") == 2, "server and skill both"
    assert not list(tmp.glob("*.bak-*")), "an idempotent run must not back up"
    assert _antigravity(cfg, skills, "--check") == 0


def test_antigravity_check_sees_a_server_pointed_elsewhere(env):
    tmp, _ = env
    cfg = tmp / "mcp_config.json"
    _antigravity(cfg, tmp / "s")
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["mcpServers"]["memory-champ"]["env"]["MEMORY_AGENT_POLICY"] = "other.yaml"
    cfg.write_text(json.dumps(data), encoding="utf-8")

    assert _antigravity(cfg, tmp / "s", "--check") == 1


def test_antigravity_refuses_unparseable_json_and_installs_nothing(env):
    tmp, _ = env
    cfg = tmp / "mcp_config.json"
    cfg.write_text('{"mcpServers": {"a": {}}, <<< not json\n', encoding="utf-8")
    before = cfg.read_bytes()

    assert _antigravity(cfg, tmp / "s") == 1
    assert cfg.read_bytes() == before
    assert not list(tmp.glob("*.bak-*"))
    assert not (tmp / "s").exists(), "a failed registration must not leave half an install"


def test_antigravity_prefers_the_2x_config_dir(env, monkeypatch):
    tmp, _ = env
    gemini = tmp / "home" / ".gemini"
    (gemini / "antigravity").mkdir(parents=True)
    assert cli.antigravity_home() == gemini / "antigravity", "1.x when only 1.x exists"

    (gemini / "config").mkdir()
    assert cli.antigravity_home() == gemini / "config", "2.x wins once it exists"

    assert cli.antigravity_skills_paths() == [gemini / "config" / "skills"]
    (gemini / "antigravity-cli").mkdir()
    assert cli.antigravity_skills_paths() == [gemini / "config" / "skills",
                                              gemini / "antigravity-cli" / "skills"]
