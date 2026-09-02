# Integrations

How the service attaches to a host. Nothing here is imported by
`src/memory_agent/` at runtime — these are files a *host* reads.

```
integrations/claude/
  claude.example.yaml                    template for ~/.memory-agent/claude.yaml
  code-skill/memory-agent/               Claude Code skill  (memory-agent install-claude-code)
    SKILL.md
    references/{tools,hygiene,scope-map,review-gate}.md
  cowork-plugin/                         Claude Cowork plugin  (python build_plugin.py)
    .claude-plugin/plugin.json
    skills/memory-agent/
      SKILL.md
      references/{tools,hygiene}.md      byte-identical copies, gated by I4
    README.md
```

Two skills rather than one, because they differ in *capability*, not tone.
Cowork cannot read `~/.memory-agent/claude.yaml` (not a trusted folder) and
cannot run the CLI, so it cannot sign approvals. A single file would have to be
a standing conditional across three separate capabilities, and the wrong branch
fails silently — Cowork tries to shell out, gets nothing, and invents a scope,
which is exactly the failure the scope rules exist to prevent. Unconditional
instructions are the ones that get followed.

What must never diverge is tool semantics and memory hygiene. Those live in
`references/tools.md` and `references/hygiene.md`, physically duplicated so each
tree is self-contained, with `verify.py` asserting the copies are identical
(I4). A checker rather than a generator, for the same reason `verify_docs()`
asserts the spec documents every tool instead of generating the spec: generation
guarantees sync at the cost of shipping a file nobody proofreads.

`claude.example.yaml` lives here and not in `contracts/`, for two reasons. It is
not a contract the implementation is built against — nothing in
`src/memory_agent/` reads it. And `[tool.setuptools.package-data]` maps
`"memory_agent.contracts" = ["*.json", "*.yaml"]`, so dropping a YAML file there
ships a Claude Desktop config template inside every wheel on every platform.

---

## Requirements

Numbered `I*` so they do not collide with the spec's `F`/`NF` series. I1–I6 are
checked by `verify.py`; I7–I10 by `tests/test_claude_integration.py` and CI.

**I1 — every skill declares itself.** *accept:* each `integrations/**/SKILL.md`
opens with a `---` fenced YAML block that parses and carries a non-empty `name`
and `description`; `name` matches `^[a-z0-9]+(-[a-z0-9]+)*$` and equals the name
of its parent directory.

**I2 — the plugin manifest is valid and points at this project.** *accept:*
`cowork-plugin/.claude-plugin/plugin.json` parses; `name` is kebab-case;
`version` matches `^\d+\.\d+\.\d+$`; `homepage` and `repository` equal the
`Homepage` and `Source` values in `pyproject.toml [project.urls]` rather than
restating them.

**I3 — the skills name only tools that exist, and name the same ones.**
*accept:* the set of `memory_[a-z_]+` tokens in each SKILL.md is a subset of the
tool names in `contracts/mcp-tools.json`, and the two skills' sets are equal.

**I4 — shared references do not drift.** *accept:* every file present under both
`code-skill/memory-agent/references/` and
`cowork-plugin/skills/memory-agent/references/` is byte-identical.

**I5 — the example config is a template, not somebody's config.** *accept:*
`claude.example.yaml` loads via `yaml.safe_load`, has `version: 1`, an empty
`scopes` mapping, a null `default_scope`, and every leaf under `cli:` is the
empty string.

**I6 — nothing machine-specific or private ships.** *accept:* no file under
`integrations/` contains an absolute local path — a drive-letter root or a POSIX
home directory — and no file contains a scope value in a scope-shaped position
except the documented placeholders. Verified as an *inverted* check: rather than
denying known-private tokens, which would put them in a public file, it asserts
that no real scope value appears at all.

**I7 — `install-claude-code` copies, and is idempotent.** *accept:* run with
`--path <tmp>`; `<tmp>/memory-agent/SKILL.md` matches the packaged source
byte-for-byte and the `references/` files land with it. A second run prints
"nothing to do", writes nothing, and creates no backup.

**I8 — it refuses to clobber, and backs up when forced.** *accept:* with a
differing `SKILL.md` planted at the destination, the command exits 1, leaves the
file byte-identical, and creates no backup. With `--force`: exits 0, the file
matches the source, and a `memory-agent.bak-*` directory holds the previous
content.

**I9 — the skill travels in a non-editable install.** *accept:* after
`pip install ".[recommended]"` from outside the checkout,
`memory-agent install-claude-code --path <tmp>` exits 0 and writes a `SKILL.md`.
This is the only place the defect is visible; no in-repo test can see it, by
construction, because tests run from the checkout where the wrong path happens
to be the right one. Same class as HANDOVER item 8.

**I10 — the built plugin is installable.** *accept:* `python build_plugin.py
<tmp>` writes `<tmp>/memory-champ-<version>.plugin`, which opens as a zip whose
root contains `.claude-plugin/plugin.json` and `skills/memory-agent/SKILL.md`.
