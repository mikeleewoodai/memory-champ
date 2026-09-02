#!/usr/bin/env python3
"""Package the Cowork plugin from `integrations/claude/cowork-plugin/`.

Claude Cowork installs a plugin from a zip with a `.plugin` extension, whose
root holds `.claude-plugin/plugin.json`. This builds that archive.

Output goes to a directory you nominate. Nothing here touches the repo — the
archive is a derived artefact, never checked in.

    python build_plugin.py dist/

The publish-safety checks run before the zip is written, and a failure aborts
the build rather than warning. This is the last point at which a file can be
stopped from leaving the machine, and the plugin is the artefact most likely to
be handed to someone else.

Builds are byte-reproducible: entries are sorted and timestamps fixed, so a
differing archive means differing content rather than a differing clock. Same
reasoning as the signed fixture — a diff should mean a change.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLUGIN = ROOT / "integrations" / "claude" / "cowork-plugin"

EXCLUDE_DIRS = {"__pycache__", ".git"}
EXCLUDE_SUFFIXES = (".pyc", ".DS_Store")
EXCLUDE_PATTERNS = (".bak", "~")

# A drive-rooted path anywhere in a shipped file is somebody's machine. The
# lookbehind lets documentation say `<drive>:/path` without tripping it, while
# still catching a real `C:\...` wherever one would actually appear.
ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]|/Users/|/home/")


def _files() -> list[Path]:
    out = []
    for p in sorted(PLUGIN.rglob("*")):
        if not p.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        if p.name.endswith(EXCLUDE_SUFFIXES) or any(x in p.name for x in EXCLUDE_PATTERNS):
            continue
        out.append(p)
    return out


def _check(files: list[Path]) -> list[str]:
    """Refuse to build a bundle that is broken or carries someone's paths."""
    problems = []

    manifest = PLUGIN / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        return [f"missing {manifest.relative_to(ROOT).as_posix()}"]

    meta = json.loads(manifest.read_text(encoding="utf-8"))
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", meta.get("name", "")):
        problems.append(f"plugin name is not kebab-case: {meta.get('name')!r}")
    if not re.fullmatch(r"\d+\.\d+\.\d+", meta.get("version", "")):
        problems.append(f"plugin version is not semver: {meta.get('version')!r}")

    skills = sorted((PLUGIN / "skills").glob("*/SKILL.md"))
    if not skills:
        problems.append("no skills/*/SKILL.md in the plugin")

    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for hit in ABSOLUTE_PATH.findall(text):
            problems.append(f"{f.relative_to(PLUGIN).as_posix()}: absolute local path ({hit})")

    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: python build_plugin.py OUTDIR", file=sys.stderr)
        return 2

    out_dir = Path(argv[1]).expanduser().resolve()
    files = _files()

    problems = _check(files)
    if problems:
        print("refusing to build:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1

    meta = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{meta['name']}-{meta['version']}.plugin"

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            info = zipfile.ZipInfo(f.relative_to(PLUGIN).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, f.read_bytes())

    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    print(f"{target}")
    print(f"{len(files)} file(s), {target.stat().st_size} bytes, sha256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
