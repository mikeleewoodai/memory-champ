# The nine tools

Reference for the `memory-champ` MCP server. Every tool is advertised from
`contracts/mcp-tools.json`; this file records the parameters worth setting and
what the returned numbers mean. When the two disagree, the contract wins.

Scope is required on six of the nine. `memory_stats`, `memory_review_proposals`
and `memory_close_cycle` need none — they are the three that cannot mis-file
anything, which makes them the safe entry points when scope resolution fails.

---

## memory_recall

`scope`, `query` required.

| Parameter | Worth setting when |
|---|---|
| `types` | Defaults to `["semantic","procedural"]` — **episodes are opt-in.** Add `"episodic"` for "what happened / what did we try" questions. A recall that feels empty is often this. |
| `k` | Default 12. Raise for a survey, lower for a pointed lookup. |
| `max_tokens` | Default 1500. `context_block` is measured against this and never exceeds it; `records` is **not** capped, so a large `k` still costs context. |
| `strategy` | `hybrid` (default), `semantic`, `keyword`, `recent`. Use `keyword` when the query is an identifier or an exact string. |
| `time_window` | `{from, to}` ISO-8601. Pair with `types:["episodic"]` for "what did we do last week". |
| `min_confidence` | Filters low-confidence records out of a decision you are about to act on. |
| `include_superseded` | Reading history — why a fact changed, not just what it is now. |
| `tags` | Narrow within a scope. |
| `cycle_id` / `session_id` | Attributes the read to an open cycle, so reflect can see what was consulted. |

Returns `records` (structured, for reasoning over) and `context_block`
(token-measured text, for pasting into a prompt). Pick one; reporting both is
noise.

**`degraded` is the field that matters.** When set — `vector_unavailable`,
`embedder_unavailable`, `index_rebuilding`, `timeout` — the recall ran without
full capability. A thin result then means *could not look properly*, not
*nothing is known*. Say which one it was. Never report a degraded miss as an
absence.

## memory_remember

`scope`, `type`, `content` required. `type` is `episodic` or `semantic` only.

- **`semantic`** — a durable fact. "The store lives outside every checkout."
- **`episodic`** — something that happened. Fill the `episodic` sub-object and
  attach `session_id` / `cycle_id`, or reflect has nothing to promote later.
- **`procedural` is not accepted here.** It returns
  `PROCEDURAL_WRITE_REQUIRES_PROPOSAL`, by design — procedures go through the
  gate. Route to `memory_propose_procedure`.

| Parameter | Worth setting when |
|---|---|
| `supersedes` | Correcting an existing record. **Prefer this to forget-then-write** — it keeps the chain, so `include_superseded` can still explain the change. |
| `tags` | Always, if a natural one exists. Tags are how a future recall narrows. |
| `importance` / `confidence` | Inferred facts deserve a lower confidence than observed ones. Be honest here; recall weights it. |
| `provenance` | Where the fact came from. |
| `idempotency_key` | Any write that might be retried. A conflict returns `IDEMPOTENCY_CONFLICT` rather than duplicating. |

## memory_propose_procedure

`scope`, `content`, `trigger`, `steps`, `rationale`, `proposed_by` required.

Queues a candidate. It becomes a record only when a human signs for it.

- `trigger` — the situation that should recall this. Write it as the *cue*, not
  the title: "invoking the CLI", not "CLI usage".
- `steps` — ordered `{n, instruction}`. Executable by someone with no memory of
  the session that produced them.
- `dedupe_key` / `supersedes` — recall with `types:["procedural"]` against the
  trigger first. If one exists, supersede it rather than stacking a near-duplicate.
- `evidence_record_ids` — the episodes that justify it, when they exist.

Report `proposal_id`, `dedupe_hit`, `queue_depth` and `expires_at` back. A
proposal expires unreviewed (30 days by default), so a queue that only grows is
a failure, not a backlog.

## memory_review_proposals

`action` required: `list`, `approve`, `reject`. No scope needed.

`list` is read-only and always safe. `approve` and `reject` require an Ed25519
signature and are **not yours to make** — see `review-gate.md`.

`state` filters `pending` (default) / `approved` / `rejected` / `expired`.
Reading `approved` is how you check what the gate has actually passed.

## memory_reflect

`scope` required.

`modes` defaults to `["consolidate","contradictions"]` — **procedures are only
promoted when `"promote"` is included.** Run
`modes:["consolidate","contradictions","promote"]` with `auto_commit:false`.

`window` takes `session_id` (end-of-session pass) or `lookback_days` (periodic).

Report `episodes_examined`, `proposals_created`, `contradictions`, and
`capped`. **A capped reflect must not be reported as complete** — it stopped at
`max_proposals`, and the rest was not examined.

Reflect promotes from episodes. With few episodes it will return little, and
that is the expected reading, not a fault.

## memory_open_cycle / memory_close_cycle

`open`: `scope`, `session_id`, `goal`. `preload` runs a recall in the same call,
so a cycle costs no extra round trip. `ttl_hours` default 24.

`close`: `cycle_id`, `outcome` — `success` / `failure` / `partial` /
`abandoned` / `unknown`. `summary` is what becomes the searchable episode;
`promote_observations` feeds reflect.

A `failure` outcome with an honest summary is the highest-value episode there
is. Do not round failures up to `partial`.

## memory_forget

`scope`, `selector`, `reason`, `max_records` required.

`dry_run:true` **first, every time.** `mode` defaults to `tombstone`; `redact`
blanks content but keeps the record; `hard_delete` needs `confirm:true` and is
the user's call, never yours. `BLAST_RADIUS_EXCEEDED` means the selector matched
more than `max_records` — re-read the selector, do not raise the cap.

## memory_stats

Nothing required. `include_scope_breakdown:true` returns `scopes[]` as
`{scope, total}` — the authoritative list of which scopes exist.

Numbers that carry meaning:

| Field | Reading |
|---|---|
| `counts.by_type.procedural` | Zero means the approval gate has never produced a record. |
| `queue.pending_proposals` + `oldest_pending_at` | A queue that grows and never drains. |
| `cycles.open` / `cycles.stale` | Cycles opened and never closed. |
| `embedding.coverage` | Below 1.0, recall is searching an incomplete index. |
| `health.last_daemon_run_at` | Null means the daemon has never run — nothing is reaping stale cycles or promoting. |
| `warnings[]` | The server's own plain-language health strings. Surface them verbatim; they are more trustworthy than an inferred summary. |

Report zeros as findings. A zero here is the most informative number in the
store, and calling it "all clear" is the failure this integration exists to end.

---

## Error codes

| Code | Means |
|---|---|
| `SCOPE_REQUIRED` | A scoped tool was called without one. |
| `PROCEDURAL_WRITE_REQUIRES_PROPOSAL` | Route it to `memory_propose_procedure`. |
| `APPROVAL_SIGNATURE_REQUIRED` / `_INVALID` / `APPROVAL_KEY_UNKNOWN` | The gate. Not something to work around. |
| `APPROVAL_CHALLENGE_INVALID` | The nonce expired or was already used. Re-list. |
| `APPROVAL_CANDIDATE_CHANGED` | The candidate changed after it was shown. The signature is correctly refused. |
| `NO_REVIEWER_KEYS_CONFIGURED` | The store has no reviewer. Nothing can ever be approved until one is added. |
| `IDEMPOTENCY_CONFLICT` | Same key, different content. |
| `BLAST_RADIUS_EXCEEDED` / `CONFIRM_REQUIRED` | Forget guards. |
| `WRITE_RATE_EXCEEDED` | Writing too fast — usually a loop. |
| `VECTOR_UNAVAILABLE` | Vector search is down; recall degrades to keyword. |
| `STORE_BUSY` | Another writer holds the lock. Retry once. |
