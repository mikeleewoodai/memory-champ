"""`install-claude-code` and the Cowork plugin build.

Requirements I7, I8 and I10 in `integrations/README.md`. I9 — that the skill
actually travels in a wheel — is deliberately not here: it cannot be tested from
the checkout, where the packaged path and the repo path resolve to the same
files. Only the CI install job sees it, which is exactly the shape of the
packaging defect that already shipped once (HANDOVER item 8).

The rules being asserted are the same ones install-claude-desktop follows: this
writes into a directory the user owns and did not hand us, so it backs up before
overwriting and refuses anything it did not write itself.
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

from memory_agent import cli
from memory_agent.skill_path import skill_files, skill_path

ROOT = Path(__file__).resolve().parents[1]
# build_plugin.py sits at the repo root, beside build_schema_site.py. conftest
# puts src/ on the path but not the root, and `pytest` run bare - as CI runs it -
# does not add the cwd the way `python -m pytest` does.
sys.path.insert(0, str(ROOT))


def _install(dest, *extra):
    return cli.main(["install-claude-code", "--path", str(dest), *extra])


def test_install_copies_the_whole_skill(tmp_path):
    assert _install(tmp_path) == 0

    source = skill_path()
    installed = tmp_path / "memory-agent"
    for rel in skill_files(source):
        assert (installed / rel).read_bytes() == (source / rel).read_bytes()

    # references/ has to land too. A SKILL.md whose progressive disclosure
    # points at files that were not copied is worse than no skill: it reads as
    # working and silently drops the rules it defers to.
    assert (installed / "references").is_dir()
    assert list((installed / "references").glob("*.md"))


def test_a_second_run_changes_nothing(tmp_path, capsys):
    _install(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    assert _install(tmp_path) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert not list(tmp_path.glob("*.bak-*")), "an idempotent run must not back up"


def test_refuses_to_clobber_a_hand_edited_skill(tmp_path):
    """The destination is a directory the user owns. A file that differs is far
    likelier to be their edit than our stale copy, and replacing it silently is
    how someone loses work they cannot get back."""
    _install(tmp_path)
    edited = tmp_path / "memory-agent" / "SKILL.md"
    edited.write_text("# mine\n", encoding="utf-8")

    assert _install(tmp_path) == 1
    assert edited.read_text(encoding="utf-8") == "# mine\n"
    assert not list(tmp_path.glob("*.bak-*")), "no backup either - nothing was written"


def test_force_replaces_but_backs_up_first(tmp_path):
    _install(tmp_path)
    edited = tmp_path / "memory-agent" / "SKILL.md"
    edited.write_text("# mine\n", encoding="utf-8")

    assert _install(tmp_path, "--force") == 0
    assert edited.read_bytes() == (skill_path() / "SKILL.md").read_bytes()

    backups = list(tmp_path.glob("memory-agent.bak-*"))
    assert len(backups) == 1
    assert (backups[0] / "SKILL.md").read_text(encoding="utf-8") == "# mine\n"


def test_check_reports_drift_without_writing(tmp_path):
    """Drift is the failure mode the copy-install pattern has and cannot see.
    Exit codes carry it so a script can act on it: 0 matches, 1 drifted, 2 absent."""
    assert _install(tmp_path, "--check") == 2

    _install(tmp_path)
    assert _install(tmp_path, "--check") == 0

    (tmp_path / "memory-agent" / "SKILL.md").write_text("# drifted\n", encoding="utf-8")
    assert _install(tmp_path, "--check") == 1
    assert (tmp_path / "memory-agent" / "SKILL.md").read_text(encoding="utf-8") == "# drifted\n"


def test_dry_run_writes_nothing(tmp_path):
    assert _install(tmp_path, "--dry-run") == 0
    assert not (tmp_path / "memory-agent").exists()


def test_an_extra_file_at_the_destination_counts_as_drift(tmp_path):
    """A leftover .md from an older layout still gets read by the host, so a
    destination carrying one has not been brought up to date."""
    _install(tmp_path)
    (tmp_path / "memory-agent" / "references" / "stale.md").write_text("old\n", encoding="utf-8")

    assert _install(tmp_path, "--check") == 1
    assert _install(tmp_path) == 1


def test_refuses_the_managed_skills_folder(tmp_path):
    """Claude Desktop syncs that directory and deletes files it did not install,
    between turns and without saying so. A skill written there looks installed
    and is gone by the next message."""
    managed = tmp_path / "local-agent-mode-sessions" / "skills-plugin" / "guid" / "skills"
    managed.mkdir(parents=True)

    assert cli.main(["install-claude-code", "--path", str(managed)]) == 1
    assert cli.main(["install-claude-code", "--path", str(managed), "--force"]) == 1, \
        "--force must not get past this one"
    assert not (managed / "memory-agent").exists()


def test_refuses_to_write_through_a_link(tmp_path):
    """A junction or symlink points somewhere the user chose - plausibly the
    checkout. Writing through it edits the target instead of installing a copy."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "skills" / "memory-agent"
    link.parent.mkdir()
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privilege or developer mode on this platform")

    assert cli.main(["install-claude-code", "--path", str(link.parent)]) == 1
    assert not list(target.iterdir()), "the link target must be untouched"


def test_skill_files_are_declared_as_package_data():
    """Guards the same defect as the contracts/ test, one layer out.

    Every skill installer copies Markdown that lives outside src/. Drop these
    declarations and the wheel stops carrying it, the commands find nothing to
    copy, and no test in this file can tell - they all run from the checkout,
    where the repo path resolves anyway.
    """
    tomllib = pytest.importorskip("tomllib", reason="TOML parsing needs 3.11+")
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    st = cfg["tool"]["setuptools"]
    assert "memory_agent.agent_skill" in st["packages"]
    assert "memory_agent.agent_skill.references" in st["packages"], \
        "references/ needs its own package entry - package-data globs do not recurse"
    assert st["package-dir"]["memory_agent.agent_skill"] == \
        "integrations/agent-skill/memory-agent"

    data = st["package-data"]
    assert data["memory_agent.agent_skill"] == ["*.md"]
    assert data["memory_agent.agent_skill.references"] == ["*.md"]


def test_skill_path_names_both_places_it_looked(tmp_path, monkeypatch):
    from memory_agent import skill_path as sp

    monkeypatch.setattr(sp, "_CANDIDATES", (tmp_path / "a", tmp_path / "b"))
    with pytest.raises(FileNotFoundError, match="package data"):
        sp.skill_path()


def test_build_plugin_produces_an_installable_bundle(tmp_path):
    """I10. Cowork installs a zip whose ROOT holds .claude-plugin/plugin.json;
    one nested directory level and the install button rejects the bundle."""
    import build_plugin

    assert build_plugin.main(["build_plugin.py", str(tmp_path)]) == 0
    bundles = list(tmp_path.glob("*.plugin"))
    assert len(bundles) == 1

    with zipfile.ZipFile(bundles[0]) as z:
        names = z.namelist()
        assert ".claude-plugin/plugin.json" in names
        assert "skills/memory-agent/SKILL.md" in names
        assert json.loads(z.read(".claude-plugin/plugin.json"))["name"] == "memory-champ"

    # Reproducible: same input, same bytes. A differing archive should mean
    # differing content rather than a differing clock.
    second = tmp_path / "again"
    assert build_plugin.main(["build_plugin.py", str(second)]) == 0
    assert bundles[0].read_bytes() == next(second.glob("*.plugin")).read_bytes()


def test_build_plugin_refuses_to_ship_an_absolute_path(tmp_path, monkeypatch):
    """The build is the last point at which a file can be stopped from leaving
    the machine, and the plugin is the artefact most likely to be handed on."""
    import build_plugin

    staged = tmp_path / "plugin"
    (staged / ".claude-plugin").mkdir(parents=True)
    (staged / "skills" / "memory-agent").mkdir(parents=True)
    (staged / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "x", "version": "0.1.0"}), encoding="utf-8")
    (staged / "skills" / "memory-agent" / "SKILL.md").write_text(
        "the store is at Q:\\\\somebody\\\\memory.db\n", encoding="utf-8")

    monkeypatch.setattr(build_plugin, "PLUGIN", staged)
    out = tmp_path / "out"
    assert build_plugin.main(["build_plugin.py", str(out)]) == 1
    assert not out.exists() or not list(out.glob("*.plugin"))
