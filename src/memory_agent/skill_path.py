"""Locate the Claude Code skill, whether installed or run from a checkout.

`install-claude-code` copies a directory of Markdown into the user's skills
folder. That directory lives at `integrations/claude/code-skill/memory-agent/`
in the repo, and it has to travel in the wheel or the command works from a
checkout and nowhere else — which is HANDOVER item 8's failure, one layer out.

The two situations differ the same way `contracts/` does. Installed, the files
ship as the `memory_agent.claude_skill` subpackage and land beside this module.
Run from a checkout, they sit under the repo root, two levels up. Resolve the
packaged copy first and fall back to the repo layout, so neither has to know
which it is.

This one is worse than `contracts/` to get wrong, because it fails quietly: a
missing schema kills the first database open, but a missing skill directory just
means the install command has nothing to copy. Only the CI install job, running
a real non-editable install from outside the checkout, can see it (requirement
I9 in `integrations/README.md`).
"""

from __future__ import annotations

from pathlib import Path

# Plain path arithmetic rather than importlib.resources, for the same reason as
# contracts_path: this is a data directory of Markdown that humans edit, it
# carries no __init__.py, and adding one purely to satisfy resources.files()
# would put a Python file in a directory that deliberately contains none.
_CANDIDATES = (
    Path(__file__).resolve().parent / "claude_skill",
    Path(__file__).resolve().parents[2] / "integrations" / "claude" / "code-skill" / "memory-agent",
)


def skill_path() -> Path:
    """Return the directory holding SKILL.md and its references/.

    Identified by SKILL.md rather than by the directory existing, so a stale
    empty `claude_skill/` left behind by an older install cannot shadow the
    checkout copy.
    """
    for root in _CANDIDATES:
        if (root / "SKILL.md").is_file():
            return root

    looked = "\n  ".join(str(r) for r in _CANDIDATES)
    raise FileNotFoundError(
        f"the Claude Code skill was not found. Looked at:\n  {looked}\n"
        f"An install that omits it cannot run install-claude-code: the skill is "
        f"Markdown that ships as package data, not something generated at "
        f"runtime.")


def skill_files(root: Path | None = None) -> list[Path]:
    """Every file the install copies, relative to the skill directory.

    Sorted, so a copy, a comparison and a --dry-run listing all agree on order.
    Only `.md` travels: nothing else in there is part of the skill, and a glob
    that swept everything would happily install a stray `.pyc` or an editor
    backup.
    """
    base = root or skill_path()
    return sorted(p.relative_to(base) for p in base.rglob("*.md") if p.is_file())
