# Integrations

How the service attaches to a host. Nothing here is imported by
`src/memory_agent/` at runtime — these are files a *host* reads.

```
integrations/
  hosts.example.yaml                     template for ~/.memory-agent/hosts.yaml
  agent-skill/memory-agent/              the skill for every full-capability host
    SKILL.md                               memory-agent install-claude-code  -> ~/.claude/skills/
    references/{tools,hygiene,               memory-agent install-codex        -> ~/.agents/skills/
               scope-map,review-gate}.md     memory-agent install-antigravity  -> Antigravity's skills/
  claude/cowork-plugin/                  Claude Cowork plugin  (python build_plugin.py)
    .claude-plugin/plugin.json
    skills/memory-agent/
      SKILL.md
      references/{tools,hygiene}.md      byte-identical copies, gated by I4
    README.md
```

Two skills, split by *capability* rather than by host. Claude Code, Codex and
Antigravity can all read `~/.memory-agent/hosts.yaml` and run the CLI, and all
three load a `SKILL.md` with `name` and `description` frontmatter, so they get
one file. The only per-host differences are cosmetic and visible to the agent
in its own tool list — how MCP tool names are prefixed, and whether the skill
is invoked as `/memory-agent` or `$memory-agent` — so the skill names both
rather than branching. Cowork is the exception: it cannot read the scope map
(not a trusted folder) and cannot run the CLI, so it cannot sign approvals. A
single file would have to be a standing conditional across those capabilities,
and the wrong branch fails silently — Cowork tries to shell out, gets nothing,
and invents a scope, which is exactly the failure the scope rules exist to
prevent. Unconditional instructions are the ones that get followed.

Adding a host means asking which side of that line it falls on. If it can read
files and run a shell, it takes the agent skill and an `install-<host>` command
that copies it; it does not get a third body.

What must never diverge is tool semantics and memory hygiene. Those live in
`references/tools.md` and `references/hygiene.md`, physically duplicated so each
tree is self-contained, with `verify.py` asserting the copies are identical
(I4). A checker rather than a generator, for the same reason `verify_docs()`
asserts the spec documents every tool instead of generating the spec: generation
guarantees sync at the cost of shipping a file nobody proofreads.

`hosts.example.yaml` lives here and not in `contracts/`, for two reasons. It is
not a contract the implementation is built against — nothing in
`src/memory_agent/` reads it. And `[tool.setuptools.package-data]` maps
`"memory_agent.contracts" = ["*.json", "*.yaml"]`, so dropping a YAML file there
ships a personal config template inside every wheel on every platform. It was
`claude/claude.example.yaml` before other hosts shared it; the skill still reads
a `~/.memory-agent/claude.yaml` when no `hosts.yaml` exists.

---

## Requirements

Numbered `I*` so they do not collide with the spec's `F`/`NF` series. I1–I6 are
checked by `verify.py`; I7–I10 by `tests/test_claude_integration.py` and CI;
I11–I12 by `tests/test_host_installs.py` and CI.

**I1 — every skill declares itself.** *accept:* each `integrations/**/SKILL.md`
opens with a `---` fenced YAML block that parses and carries a non-empty `name`
and `description`; `name` matches `^[a-z0-9]+(-[a-z0-9]+)*$` and equals the name
of its parent directory; `name` is at most 64 characters and `description`, with
whitespace folded, at most 1024 — the Agent Skills limits that Codex and
Antigravity enforce on the same file Claude Code reads more leniently.

**I2 — the plugin manifest is valid and points at this project.** *accept:*
`cowork-plugin/.claude-plugin/plugin.json` parses; `name` is kebab-case;
`version` matches `^\d+\.\d+\.\d+$`; `homepage` and `repository` equal the
`Homepage` and `Source` values in `pyproject.toml [project.urls]` rather than
restating them.

**I3 — the skills name only tools that exist, and name the same ones.**
*accept:* the set of `memory_[a-z_]+` tokens in each SKILL.md is a subset of the
tool names in `contracts/mcp-tools.json`, and the two skills' sets are equal.

**I4 — shared references do not drift.** *accept:* every file present under both
`agent-skill/memory-agent/references/` and
`cowork-plugin/skills/memory-agent/references/` is byte-identical.

**I5 — the example config is a template, not somebody's config.** *accept:*
`hosts.example.yaml` loads via `yaml.safe_load`, has `version: 1`, an empty
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
matches the source, a `memory-agent.bak-*` directory under `<tmp>-backup/` holds
the previous content, and `<tmp>` holds nothing but `memory-agent/` — a backup
inside the skills directory is loaded by the host as a second skill.

**I9 — the skill travels in a non-editable install.** *accept:* after
`pip install ".[recommended]"` from outside the checkout,
`memory-agent install-claude-code --path <tmp>`, `install-codex --path <tmp>`
and `install-antigravity --path <tmp> --skills-path <tmp>` each exit 0 and write
a `SKILL.md`.
This is the only place the defect is visible; no in-repo test can see it, by
construction, because tests run from the checkout where the wrong path happens
to be the right one. Same class as HANDOVER item 8.

**I10 — the built plugin is installable.** *accept:* `python build_plugin.py
<tmp>` writes `<tmp>/memory-champ-<version>.plugin`, which opens as a zip whose
root contains `.claude-plugin/plugin.json` and `skills/memory-agent/SKILL.md`.

**I11 — `install-codex` installs the skill and prints, never writes, the
registration.** *accept:* with a policy file resolvable, `--path <tmp>` copies
the skill as I7 does and prints a `codex mcp add` command and a TOML block that
both name `sys.executable`, `-m memory_agent.server` and the absolute policy
path; the TOML block parses to exactly that entry. A pre-existing
`config.toml` is byte-identical afterwards, with no backup beside it. With no
policy resolvable it exits 1, says to run `init`, and writes nothing.

**I12 — `install-antigravity` merges the server and installs the skill.**
*accept:* into a 0-byte `mcp_config.json` it writes an entry of
`sys.executable`, `-m memory_agent.server` and `env.MEMORY_AGENT_POLICY` set to
the absolute policy path, and the skill lands at `--skills-path`. Other servers
and other keys survive, the original is backed up first, a second run changes
nothing and backs nothing up, unparseable JSON is left byte-identical with no
skill installed, and `--check` exits 0 matching, 1 differing, 2 absent.
