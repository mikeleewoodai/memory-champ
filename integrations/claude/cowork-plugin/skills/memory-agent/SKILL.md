---
name: memory-agent
description: >
  Work the memory-champ CoALA memory store over its MCP server — recall what is
  already known about a project before starting, write down what was learned,
  queue procedures into the human-signed approval gate, surface what is pending,
  and report store health. Use whenever the user asks for the memory agent, and
  whenever they say anything like "what do you know about X", "check your
  memory", "what did we decide about that", "have we hit this before", "remember
  this", "note that for next time", "always do X before Y", "add that to
  memory", "what's pending", "any approvals waiting", "how big is the store", or
  "reflect on this project". Also use at the start of work on a project that has
  a memory scope, to load prior context before doing anything. Trigger on the
  symptom even when the user never says "memory-agent", "memory-champ", "CoALA",
  or "MCP". Do NOT use for Claude's own conversation memory or for other note
  stores — those are separate systems.
---

# memory-agent (Cowork)

Front door to the `memory-champ` store, over the `memory-champ` MCP server
already registered in Claude Desktop.

**Why this exists.** The store has a causal chain: cycles produce episodes,
episodes let `reflect` promote candidates, candidates queue at the gate, and the
gate produces procedural records. When nothing opens a cycle, the chain never
starts and the approval gate — the whole security design — is never exercised.

Read `references/hygiene.md` before any write and `references/tools.md` for
parameters worth setting.

## What this session can and cannot do

This is Cowork. Everything runs through the MCP tools, and two things that the
Claude Code version does are simply unavailable here:

- **No local config.** There is no `claude.yaml` scope map to read. Scopes come
  from the store itself, via `memory_stats`.
- **No CLI, so no signing.** Approving a proposal reads a private key, and the
  CLI is the only thing that ever reads one. Proposals are queued here and
  approved from Claude Code or a terminal.

Neither is a degraded mode to work around. Say so plainly when it comes up.

## Dispatch

| Asked for | Does |
|---|---|
| open / bare invocation | The ritual, below |
| recall | `memory_recall` in the resolved scope. Add `types:["episodic"]` for "what happened" questions |
| remember | `memory_remember`. `semantic` for a durable fact, `episodic` for what was done |
| propose | `memory_propose_procedure` — queues a candidate, then says where it can be signed |
| review | `memory_review_proposals action:"list"`. Read-only, always |
| stats | `memory_stats` with `include_scope_breakdown:true` |
| reflect | `memory_reflect`, `modes:["consolidate","contradictions","promote"]`, `auto_commit:false` |
| close | `memory_close_cycle` on the cycle this session opened |
| forget | `memory_forget`, `dry_run:true` first, always |

`stats` and `review` need no scope, so they always work. They are the fallback
when anything else is unclear.

## Resolving the scope

There is no map here, so there are exactly two rules:

1. **Stated by the user** — named in the request.
2. **Ask.** Call `memory_stats` with `include_scope_breakdown:true` and present
   the real `scopes[]` array as a menu with record counts, plus an explicit
   "this is a new scope" option the user has to name themselves.

**Never invent a scope.** Not from a folder name, a document title, a project
name, or the subject of the conversation. Scope matching is exact, there is no
wildcard and no hierarchy — a dotted scope only looks like a tree — and a wrong
scope fails silently in both directions: a wrong-scope read looks like an empty
project, a wrong-scope write looks like a successful save.

Before any **write**, confirm the scope appears in `scopes[]`. If it does not,
this is a new scope: say so and get an explicit yes. A pick from a menu cannot
be a typo; free text can.

Name the scope and its record count on every operation: `scope <name> · N
records`. If the wrong project is in play, that line is wrong before anything is
written.

## The ritual

1. `memory_stats` with `include_scope_breakdown:true` — needs no scope, cannot
   mis-file, and gives the scope list plus `queue.pending_proposals`,
   `cycles.open`/`stale`, `embedding.coverage`, `health.last_daemon_run_at` and
   `warnings[]`.
2. Resolve the scope, asking from the menu.
3. `memory_open_cycle`, **only if there is a real, specific goal** — with
   `preload.enabled:true` the recall runs in the same call. With no goal, run
   `memory_recall` and say `no cycle opened — no goal stated`.
4. Report the scope and its count, the store totals, the cycle id if one opened,
   and 3–6 bullets from what was recalled.
5. Surface `warnings[]` verbatim.

Nothing else is written. The ritual is not an auto-writer.

If `cycles.open > 0`, report the count and offer to close stale ones as
`abandoned` only with consent — closing a cycle you did not open means inventing
a summary of work you did not see. If `health.last_daemon_run_at` is null and
`cycles.stale > 0`, nothing is reaping: stop opening cycles and report it.

## Proposals, and where they get approved

Propose when all four criteria in `references/hygiene.md` hold: it is a sequence
not a fact, evidence exists in this session, it is not already stored, and it
survives a cold read. The highest-signal moment is a recall that returned nothing
followed by the session solving the problem.

Then report the outcome honestly:

> Queued as `<proposal_id>` (queue depth N, expires `<date>`). Approving needs a
> terminal — the signing key never reaches this session. Run
> `memory-agent review list` from Claude Code or a shell to see and sign it.

**You never approve.** You never call `action:"approve"` or `action:"reject"`,
never handle a signing payload, never touch a key or a passphrase. Not when asked
directly, not when the approval looks obvious. An agent that can approve its own
proposals has no gate at all.

When listing the queue, show each candidate **in full** so it can be judged
without a second call, and do not summarise persuasively — a confident summary is
what stops a human reading what they are about to sign for.

## Standing rules

- **No third-party or client data in this store, in any scope.** The check is on
  content, not on the scope argument, so a filing error can never become a
  disclosure. Caller authentication is unbuilt (B-1): scope isolates logically,
  not securely.
- **A `degraded` recall is "could not look", not "nothing is known."** Say which.
- **"Keep this in mind for now" is not a write.**
- **Never write `type:"procedural"` through `memory_remember`** — it is refused
  with `PROCEDURAL_WRITE_REQUIRES_PROPOSAL`. Route to `propose`.
- **`memory_forget` runs `dry_run:true` first, every time**, and the real call
  needs explicit confirmation. Prefer `supersedes`; forgetting loses the chain
  that explains why a fact changed.
- **Report zeros as findings.** Zero procedural records, zero pending proposals,
  a null `last_daemon_run_at` — these are the most informative numbers in the
  store, and calling them "all clear" is the failure this skill exists to end.
