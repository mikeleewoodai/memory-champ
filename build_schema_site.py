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

    _write_index(out_root / "v1" / "index.html", found)

    print(f"wrote {len(found)} schemas + an index to {out_root}")
    for ident in sorted(found):
        print(f"  {ident[len(BASE):]}")
    return 0


def _write_index(dest: Path, found: dict[str, dict]) -> None:
    """A directory listing for /v1/.

    GitHub Pages serves 404 for a directory with no index, so without this the
    `Schemas` link in the published brief lands on nothing. Generated from the
    same walk as the schemas themselves, so it cannot list a file that was not
    written or miss one that was.
    """
    # The record schemas carry their own `title`. The tool input/output schemas
    # do not - the prose lives on the tool that owns them - so those get a label
    # derived from the filename instead of an empty cell.
    rows = []
    for ident in sorted(found):
        name = ident[len(BASE) + len("v1/"):]
        rows.append(f'    <tr><td><a href="{name}">{name}</a></td>'
                    f'<td>{_escape(_label(name, found[ident]))}</td></tr>')

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>memory-champ — v1 JSON Schemas</title>
<style>
  :root{{color-scheme:light dark;--ink:#231e19;--soft:#5b5349;--line:#ddd8cc;
        --bg:#f6f4ee;--card:#fff;--euc:#2f5d4f}}
  @media(prefers-color-scheme:dark){{:root{{--ink:#e8e6e0;--soft:#a9b0ac;--line:#333b3c;
        --bg:#16191a;--card:#1d2223;--euc:#8fd0b4}}}}
  body{{margin:0;background:var(--bg);color:var(--ink);padding:44px 22px;
       font:16px/1.6 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}}
  main{{max-width:820px;margin:0 auto}}
  h1{{font-size:1.7rem;margin:0 0 6px;letter-spacing:-.02em}}
  p{{color:var(--soft);margin:0 0 22px}}
  a{{color:var(--euc)}}
  .wrapt{{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px}}
  table{{width:100%;border-collapse:collapse;font-size:.92rem}}
  td{{padding:9px 14px;border-bottom:1px solid var(--line);vertical-align:top}}
  tr:last-child td{{border-bottom:none}}
  td:first-child{{font-family:ui-monospace,Consolas,monospace;font-size:.83rem;white-space:nowrap}}
  td:last-child{{color:var(--soft)}}
</style></head><body><main>
<h1>v1 JSON Schemas</h1>
<p>The tool contract for <a href="../">memory-champ</a>, at the URLs its
<code>$id</code> values declare. Generated from <code>contracts/</code> —
<a href="https://github.com/mikeleewoodai/memory-champ">source on GitHub</a>.</p>
<div class="wrapt"><table>
{chr(10).join(rows)}
</table></div>
</main></body></html>
""", encoding="utf-8")


def _label(filename: str, schema: dict) -> str:
    """A one-line description for the index."""
    explicit = schema.get("title") or schema.get("description") or ""
    if explicit:
        return explicit.split(". ")[0].strip().rstrip(".")

    stem = filename.removesuffix(".schema.json")
    for suffix, word in ((".input", "arguments"), (".output", "result")):
        if stem.endswith(suffix):
            return f"{stem.removesuffix(suffix)} — {word}"
    return stem


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
