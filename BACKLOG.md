# Backlog

Open work items, highest priority first. Each names the trigger that makes it urgent, so nothing here is a vague "someday".

---

## B-1 — Caller authentication and scope authorization · BLOCKS the work version

**Status:** open, narrowed · **Raised:** 2026-08-08 · **Blocks:** any deployment beyond one person on one machine
**Related:** spec assumption A-3, §4 non-goals, §11 memory poisoning, §14 re-architecture triggers

### Already done — reviewer identity (2026-08-08)

The approval half of A-3 was closed early and is **not** part of this item. Approving or rejecting a procedure now requires an Ed25519 signature from a key the server holds only the public half of. An agent cannot approve its own proposals; approvals bind to exact candidate content; every decision is verifiable offline from the record alone, and post-approval edits are detectable. Spec §8 "Signed approvals", requirements F18–F23.

That was pulled forward because it is the only part of A-3 that bites in solo use — the realistic single-machine threat is an agent approving its own procedure, which needs no network.

### What remains

Everything about **who is calling**, as opposed to who approved.

There is still no caller authentication. Any orchestration that can reach the MCP server can read and write **any scope whose name it knows**, and `memory_remember` writes are unattributed — a host can write any fact, into any scope, as anyone. Scope is a logical namespace, not a security boundary.

This is accepted for solo use, where the threat model is genuinely thin: anything that could reach the server over stdio could already open `memory.db` with a SQLite client. That reasoning **stops holding the moment a second person, a second machine, or a network transport is involved.**

### What makes it urgent

Any one of these:

- A work version, or any deployment with more than one user
- Anyone else's data in the store — clients, colleagues, anything covered by an agreement
- Binding to anything other than loopback, or using `streamable-http` instead of stdio
- Sharing or syncing a `memory.db` between people

### Why it is worse here than in a typical service

Two properties of this specific design raise the stakes:

1. **Memory is trusted by construction.** A host asks for context and injects it into a prompt. A false fact written by an unauthorised writer is not a data-integrity problem — it is a behaviour-modification primitive, and it persists across every future run. Signing protects procedures; it does nothing for semantic facts, which are the easier and quieter thing to poison.
2. **Scope names are guessable.** They are short, human-chosen, and appear in logs and configs. There is no penalty for probing, and no record of who probed.

### Scope of the work

| Piece | What it means |
|---|---|
| Caller identity | A principal per connecting host, not an unauthenticated socket. Bearer token over `streamable-http`; for stdio, a token in env |
| Scope authorization | Per-principal grants — read, write, review — enforced in the storage layer, not in each tool handler. A missing check must fail closed |
| Write attribution | `memory_remember` records the authenticated principal, not just the self-declared `provenance.agent`. This is what makes a poisoning incident cleanable: without it, `forget` cannot reliably select what one bad writer wrote |
| Daemon identity | The daemon gets its own principal with propose-but-never-approve rights, making `daemon_may_approve: false` a permission rather than a config setting anyone can flip. Signing already blocks the daemon in practice; this makes it structural |
| Rate limiting per principal | Currently per session, which an unauthenticated caller can trivially rotate |
| Encryption at rest | Separate decision. `memory.db` is plaintext today, and it now holds approval signatures whose value depends on the record not being editable underneath them. Relevant once it holds anyone else's data |
| Multi-reviewer policy | Signing supports several reviewer keys already. A work version needs the surrounding rules: who may review which scope, and whether some procedures need two approvals |

### Design notes for when this is picked up

- **The tool contract should barely change.** Identity belongs in the transport and the storage layer. Signing needed contract changes because a signature is *data the caller supplies*; caller identity is not, and should ride the transport. If adding authn forces changes to `mcp-tools.json`, that is a signal the design went wrong — the same isolation that lets storage move to Postgres without touching the contract (A-2).
- **Fail closed.** An unrecognised principal gets nothing, not a default scope.
- **Do not reuse `scope` as the permission unit without checking the hierarchy.** Scopes are dotted (`leewood.crm.builder`), so a grant on `leewood` plausibly implies its children — decide that explicitly rather than letting prefix matching decide it by accident.
- **Do not fold reviewer identity back into caller identity.** The obvious move once principals exist is to drop signing and let `reviewed_by` be the authenticated principal. Resist it: a session credential proves who is connected *now*, while a signature proves who decided *then* and survives the session, the server, and the database. They answer different questions, and the durable one is the one worth keeping.
- Revisit whether the work version wants Postgres anyway (A-2, §14). Multi-user and multi-writer tend to arrive together, and Postgres row-level security would do a large part of this work.

### Definition of done

- A caller with no credential can perform no operation, including `memory_stats`
- A caller credentialed for `scope.a` receives zero records from `scope.b` on every tool and every strategy — the F3 acceptance test, re-run against a *different* principal rather than a different argument
- Every record carries the authenticated principal that wrote it, and `memory_forget` can select on it
- The daemon's principal is refused on approve, verified end-to-end rather than by reading the config, and independently of the signature check
- Signed approvals still verify unchanged — adding caller auth must not alter or weaken F18–F23
- New acceptance criteria added to spec §10 alongside F1–F23 / NF1–NF10, and A-3 fully closed

---

## B-2 — `supersedes` and `evidence_record_ids` are outside the signature

**Status:** open · **Raised:** 2026-08-09 · **Blocks:** nothing today; fold into the next breaking signature change
**Related:** `approval.CANDIDATE_FIELDS`, `store.procedure_candidate`, spec §8

`candidate_sha256` now covers `content`, `trigger`, `preconditions`, `steps`, `success_signal` and `failure_signal`. Two fields the reviewer is shown are still outside it.

**`supersedes`** is the more interesting of the two. It says which procedure this one replaces, so altering it after approval retargets a signed supersession at a different procedure — retiring something the reviewer never agreed to retire. The value is already stored in `procedural_attrs`, so `procedure_candidate` can reconstruct it today; this is a small change.

**`evidence_record_ids`** is the records the proposal was justified by. There is no column for it, so it cannot be reconstructed at verification time at all. Covering it needs a DDL change first, and the DDL is where this class of bug hides: a field with nowhere to be reconstructed from is a field the signature quietly stops covering.

### Why it was not done at the same time as `content`

`content` was a live hole — the text an agent follows could be rewritten under a valid signature. These two are narrower, and each addition invalidates every existing signature. Doing them separately would mean two `PAYLOAD_VERSION` bumps and two fixture regenerations for no gain, so they should land together, in whatever breaking change comes next.

### Definition of done

- `CANDIDATE_FIELDS` covers `supersedes`, and `evidence_record_ids` if the DDL gains a column for it
- `store.procedure_candidate()` reconstructs every covered field — verified by a test that fails if a covered field is missing from the reconstruction, rather than by reading the code
- `PAYLOAD_VERSION` bumped, `regenerate_fixture.py` re-run, schema and spec updated
- A test proves mutating each covered field breaks verification, so the next omission is caught by the suite rather than by hand
