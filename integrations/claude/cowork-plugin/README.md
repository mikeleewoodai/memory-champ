# memory-champ for Cowork

The Cowork half of [memory-champ](https://github.com/mikeleewoodai/memory-champ)
— a CoALA memory service with an MCP interface, SQLite storage, and a
cryptographic human gate on anything the agent wants to learn.

This plugin ships one skill. It does **not** ship a server.

## Prerequisite: the MCP server must already be registered

The skill talks to the `memory-champ` MCP server through Claude Desktop's own
config. Install it once, from the machine where the package is installed:

```
pip install "memory-champ[recommended]"
memory-agent init
memory-agent install-claude-desktop
```

Then restart Claude Desktop. Without that, the skill loads and every tool call
fails.

### Why there is no `.mcp.json` in this plugin

A plugin *can* carry its own MCP server definition, and that is the obvious
default. It is deliberately not done here, for two reasons:

1. It would start a **second** `memory_agent.server` process against the same
   `memory.db` alongside the one already registered.
2. Claude Desktop gives each MCP server a 60-second connect budget on cold
   start, and every server spawns at once. Adding a redundant competitor to that
   window is a good way to make an already-working server intermittently
   unavailable.

A plugin cannot reach the interpreter that has the package installed via
`${CLAUDE_PLUGIN_ROOT}` anyway, so a bundled definition would need a hardcoded
absolute path — machine-specific, and wrong on every other machine.

## What the skill does

| Asked for | Does |
|---|---|
| bare / open | Store health, scope, and a recall of what is already known |
| recall | Hybrid search within one scope |
| remember | Write a durable fact or a record of what happened |
| propose | Queue a procedure for human approval |
| review | List the pending queue, read-only |
| stats | Counts, coverage, and warning signs |
| reflect | Consolidate, find contradictions, promote candidates |
| forget | Tombstone records, dry run first |

## What it cannot do here

- **No local config.** Cowork does not have the checkout or `~/.memory-agent`
  as a trusted folder, so there is no scope map to read. The skill asks, using
  the store's own scope list as the menu.
- **No approvals.** Approving a proposal signs with an Ed25519 private key, and
  the CLI is the only thing that ever reads one. Proposals are queued here and
  signed from Claude Code or a terminal. This is the design working, not a gap
  to route around.

## The limit that matters

Caller authentication is not built (backlog item B-1). Any caller reaching the
server can read and write any scope it can name, and writes are unattributed.
**Scope isolates logically, not securely.** This is accepted for one person on
one machine over stdio, and must be closed before any multi-user, multi-machine,
network-bound, or third-party-data use.

MIT licensed. Source: https://github.com/mikeleewoodai/memory-champ
