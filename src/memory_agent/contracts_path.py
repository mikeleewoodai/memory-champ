"""Locate `contracts/` at runtime, whether installed or run from a checkout.

Two files in `contracts/` are read at runtime rather than being restated in
Python: `db/schema.sql`, which carries the storage invariants, and
`mcp-tools.json`, which `server.py` builds its advertised tool schemas from.
That is deliberate — it is what keeps the contract the source of truth instead
of a document that drifts from the code.

The cost is that both have to be found at runtime, and the two situations
differ. Installed, they ship inside the wheel as the `memory_agent.contracts`
subpackage. Run from a checkout, they sit at the repo root, two levels above
this file. Resolving the packaged copy first and falling back to the repo
layout covers both without either one having to know which it is.

This existed as `parents[2] / "contracts"` inline in two modules, which only
ever worked from an editable install: a real `pip install` put the package in
site-packages, `parents[2]` landed on `Lib/contracts`, and the first database
open died on FileNotFoundError. Tests never saw it because they run from the
checkout.
"""

from __future__ import annotations

from pathlib import Path

# Installed: package-data lands the directory beside this module. A checkout:
# it is at the repo root, two levels up. Plain path arithmetic rather than
# importlib.resources, because `contracts/` carries no __init__.py - it is a
# data directory humans edit, not a Python package - so resources.files()
# cannot import it, and adding an __init__.py just to satisfy that would put a
# Python file in a directory that deliberately contains none.
_CANDIDATES = (
    Path(__file__).resolve().parent / "contracts",
    Path(__file__).resolve().parents[2] / "contracts",
)


def contract_path(*parts: str) -> Path:
    """Return a real filesystem path to `contracts/<parts...>`.

    A `Path` rather than a `Traversable` because callers hand it to sqlite3 and
    to `read_text`, and because every distribution of this package is a plain
    directory on disk rather than a zipimport. If that ever stops being true,
    this is the one place that has to change.
    """
    for root in _CANDIDATES:
        candidate = root.joinpath(*parts)
        if candidate.is_file():
            return candidate

    looked = "\n  ".join(str(r.joinpath(*parts)) for r in _CANDIDATES)
    raise FileNotFoundError(
        f"contracts/{'/'.join(parts)} not found. Looked at:\n  {looked}\n"
        f"An install that omits contracts/ cannot start: the DDL and the tool "
        f"schemas live there, not in Python.")
