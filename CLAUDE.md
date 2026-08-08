# claude-mobile

Projects folder for work started from my phone. Each subdirectory is a separate project — nothing at the root is shared infrastructure.

## Projects

| Path | What it is | Status |
|---|---|---|
| `memory-agent/` | CoALA-based memory service for agent orchestrations. MCP interface, SQLite + sqlite-vec storage, Python target. | Specification + contracts complete. **No implementation yet.** |

## Standing items

**`memory-agent` — authentication is unbuilt and blocks a work version.** v1 deliberately has no authn/authz; scope isolates logically, not securely. Accepted for solo use on one machine over stdio. Must be closed before any multi-user, multi-machine, network-bound, or client-data deployment. Full work item in `memory-agent/BACKLOG.md` (B-1); assumption A-3 in the spec. Raise this when a work version, a second user, or any non-stdio transport comes up.

## Conventions

Established by `memory-agent/`, worth following unless a project has a reason not to:

- Projects are kebab-case folders. Specs live in `<project>/docs/<subject>-<role>.md`.
- Machine-readable contracts live in `<project>/contracts/` — JSON Schema draft 2020-12, DDL, example fixtures — and are the source of truth an implementation is built against.
- Requirements are numbered and testable, each with an `accept:` line stating a concrete input and expected result.
- Assumptions are falsifiable claims with an explicit consequence if wrong, and get resolved or carried into a backlog rather than left implicit.
- Decisions get logged with the tension behind them, not just the outcome.
- Verify claims before making them. Schemas get validated, DDL gets executed, links get checked.
