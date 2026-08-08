# Backlog

Open work items, highest priority first. Each names the trigger that makes it urgent, so nothing here is a vague "someday".

---

## B-1 — Authentication and scope authorization · BLOCKS the work version

**Status:** open · **Raised:** 2026-08-08 · **Blocks:** any deployment beyond one person on one machine
**Related:** spec assumption A-3, §4 non-goals, §11 memory poisoning, §14 re-architecture triggers

### The gap

v1 has no authentication of any kind. Any orchestration that can reach the MCP server can read and write **any scope whose name it knows**. Scope is a logical namespace, not a security boundary — there is nothing enforcing that the `acme.crm` agent only touches `acme.crm`.

This was accepted deliberately for solo use, where the threat model is genuinely empty: anything that could reach the server over stdio could already open `memory.db` with a SQLite client. That reasoning **stops holding the moment a second person, a second machine, or a network transport is involved.**

### What makes it urgent

Any one of these:

- A work version, or any deployment with more than one user
- Anyone else's data in the store — clients, colleagues, anything covered by an agreement
- Binding to anything other than loopback, or using `streamable-http` instead of stdio
- Sharing or syncing a `memory.db` between people

### Why it is worse here than in a typical service

Three properties of this specific design raise the stakes:

1. **Memory is trusted by construction.** A host asks for context and injects it into a prompt. A false fact written by an unauthorised writer is not a data-integrity problem — it is a behaviour-modification primitive, and it persists across every future run.
2. **Procedural memory is instructions.** The approval gate stops an *agent* from writing procedures. It does not stop an unauthorised *caller* from posing as a human reviewer and approving one, because there is nothing to check who the reviewer is. `reviewed_by` is currently a self-asserted string.
3. **Scope names are guessable.** They are short, human-chosen, and appear in logs and configs. There is no penalty for probing.

### Scope of the work

| Piece | What it means |
|---|---|
| Caller identity | A principal per connecting host, not an unauthenticated socket. Bearer token over `streamable-http`; for stdio, a token in env |
| Scope authorization | Per-principal grants — read, write, review — enforced in the storage layer, not in each tool handler. A missing check must fail closed |
| Reviewer identity | `reviewed_by` becomes the authenticated principal rather than a caller-supplied string, and `APPROVAL_REQUIRES_HUMAN` becomes enforceable instead of advisory |
| Daemon identity | The daemon gets its own principal with propose-but-never-approve rights, making `daemon_may_approve: false` a permission rather than a config setting anyone can flip |
| Audit | Every write and every approval records the authenticated principal, not just `provenance.agent` |
| Rate limiting per principal | Currently per session, which an unauthenticated caller can trivially rotate |
| Encryption at rest | Separate decision. `memory.db` is plaintext today. Relevant once it holds anyone else's data |

### Design notes for when this is picked up

- **The tool contract should not change.** Identity belongs in the transport and the storage layer. If adding auth forces changes to `mcp-tools.json`, that is a signal the design went wrong — this is the same isolation that lets storage move to Postgres without touching the contract (A-2).
- **Fail closed.** An unrecognised principal gets nothing, not a default scope.
- **Do not reuse `scope` as the permission unit without checking the hierarchy.** Scopes are dotted (`leewood.crm.builder`), so a grant on `leewood` plausibly implies its children — decide that explicitly rather than letting prefix matching decide it by accident.
- Revisit whether the work version wants Postgres anyway (A-2, §14). Multi-user and multi-writer tend to arrive together, and Postgres row-level security would do a large part of this work.

### Definition of done

- A caller with no credential can perform no operation, including `memory_stats`
- A caller credentialed for `scope.a` receives zero records from `scope.b` on every tool and every strategy — the F3 acceptance test, re-run against a *different* principal rather than a different argument
- `reviewed_by` on an approved procedure equals the authenticated principal and cannot be set by the caller
- The daemon's principal is refused on approve, verified end-to-end rather than by reading the config
- New acceptance criteria added to spec §10 alongside F1–F17 / NF1–NF10, and A-3 closed
