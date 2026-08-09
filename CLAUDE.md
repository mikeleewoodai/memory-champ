# memory-champ

A CoALA-based memory service for agent orchestrations. MCP interface, SQLite + sqlite-vec storage, Python. The project lives at the repo root — `src/`, `contracts/`, `docs/`, `tests/`, `verify.py`.

**Status: implemented.** Spec + contracts + nine MCP tools + daemon + signing CLI. `python verify.py` and `pytest -q` are both green, and expected to stay that way.

## Where things are

| Path | What |
|---|---|
| `docs/memory-agent-coala-spec.md` | The spec. Architecture, CoALA mapping, action contract, numbered acceptance criteria, decision log |
| `contracts/` | Source of truth. Tool schemas, record schemas, DDL, the signed example fixture |
| `src/memory_agent/` | Implementation. The package keeps the `memory_agent` name; the repo is `memory-champ` |
| `verify.py` | 132 contract checks. Run before and after any contract change |
| `HANDOVER.md` | Read-cold orientation, plus the six things most likely to trip you up |
| `BACKLOG.md` | Open work, highest priority first |

## Standing items

**Caller authentication is unbuilt and blocks a work version.** Reviewer identity is done: approving a procedure requires an Ed25519 signature from Mike's key, so an agent cannot approve its own proposals (spec §8, F18–F23). What remains is authn/authz for *callers* — any orchestration reaching the server can read and write any scope it can name, and `memory_remember` writes are unattributed. Scope isolates logically, not securely. Accepted for solo use on one machine over stdio. Must be closed before any multi-user, multi-machine, network-bound, or client-data deployment. Full work item in `BACKLOG.md` (B-1); assumption A-3 in the spec. Raise this when a work version, a second user, or any non-stdio transport comes up.

## Conventions

- Machine-readable contracts in `contracts/` are the source of truth an implementation is built against — JSON Schema draft 2020-12, DDL, example fixtures. `server.py` builds its advertised tool schemas from `contracts/mcp-tools.json`, not from Python signatures. Keep it that way, or the contract stops being the source of truth.
- Requirements are numbered and testable, each with an `accept:` line stating a concrete input and expected result. Tests assert the `accept:` line as written, one per requirement.
- Assumptions are falsifiable claims with an explicit consequence if wrong, and get resolved or carried into the backlog rather than left implicit.
- Decisions get logged with the tension behind them, not just the outcome.
- Verify claims before making them. Schemas get validated, DDL gets executed, links get checked.
- **Read and write text with an explicit `encoding="utf-8"`.** The contract files carry non-ASCII, and the platform default is cp1252 on Windows. Dropping the encoding silently mangles `contracts/examples/records.json` and breaks the golden signing test in a way that looks like canonical-form drift. See HANDOVER item 2.
- **Write transactions open with `BEGIN IMMEDIATE`, never a bare `BEGIN`.** A deferred transaction cannot honour `busy_timeout` on a read→write upgrade, so concurrent writers fail instantly with `database is locked`. See HANDOVER item 6.

## History

Drafted in `mikeleewoodai/claude-mobile` under `memory-agent/` — a scratch repo for work started from a phone. Moved here with full history, so early commits show that layout.
