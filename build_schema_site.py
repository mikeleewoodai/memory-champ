#!/usr/bin/env python3
"""Emit the published schema tree from `contracts/`.

Every schema in `contracts/` carries an absolute `$id` under
https://mikeleewoodai.github.io/memory-champ/v1/. Those are not decoration: a
`$id` that does not resolve is a contract that cannot be fetched, validated
against, or `$ref`-ed by anyone downstream. This walks the contracts, pulls out
each schema, and writes it to the exact path its own `$id` claims.

Output goes to a directory you nominate, which the publish step copies onto the
gh-pages branch alongside the brief. Nothing here touches the repo.

    python build_schema_site.py out/

The tool schemas live inline inside `contracts/mcp-tools.json` rather than as
files, because `server.py` builds its advertised tool list from that one
document and splitting it would create a second source of truth. So they are
extracted here at publish time instead — derived artefacts, never checked in.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = "https://mikeleewoodai.github.io/memory-champ/"
ROOT = Path(__file__).resolve().parent
CONTRACTS = ROOT / "contracts"


def _walk(node, found: dict[str, dict]) -> None:
    """Collect every object carrying an absolute `$id` under BASE.

    Recursive rather than top-level-only: the tool input/output schemas are
    nested two deep inside mcp-tools.json, and hard-coding that depth would
    silently skip any schema that later moves.
    """
    if isinstance(node, dict):
        ident = node.get("$id")
        if isinstance(ident, str) and ident.startswith(BASE):
            found[ident] = node
        for value in node.values():
            _walk(value, found)
    elif isinstance(node, list):
        for value in node:
            _walk(value, found)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    out_root = Path(argv[1]).resolve()

    found: dict[str, dict] = {}
    sources = sorted(CONTRACTS.glob("*.json")) + sorted(CONTRACTS.glob("schemas/*.json"))
    for src in sources:
        _walk(json.loads(src.read_text(encoding="utf-8")), found)

    if not found:
        print("no $id under the published base — nothing to write", file=sys.stderr)
        return 1

    for ident, schema in sorted(found.items()):
        rel = ident[len(BASE):]
        if ".." in rel or rel.startswith("/"):
            print(f"refusing to write outside the output root: {ident}", file=sys.stderr)
            return 1
        dest = out_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Newline-terminated and stably sorted so republishing an unchanged
        # contract produces an identical byte stream, and the gh-pages diff
        # stays empty when nothing actually changed.
        dest.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"wrote {len(found)} schemas to {out_root}")
    for ident in sorted(found):
        print(f"  {ident[len(BASE):]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
