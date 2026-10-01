---
name: memory-agent
description: >
  Work the memory-champ CoALA memory store over its MCP server — recall what is
  already known about this project before starting, write down what was learned,
  queue procedures into the human-signed approval gate, surface what is pending,
  and report store health. Use whenever the user runs /memory-agent or
  $memory-agent, and whenever they say anything like "what do you know about X", "check your memory", "what
  did we decide about that", "have we hit this before", "remember this", "note
  that for next time", "always do X before Y", "add that to memory", "what's
  pending", "review the proposal queue", "any approvals waiting", "how big is the
  store", or "reflect on this project". Also use at the start of work in a
  project that has a memory scope, to load prior context before touching
  anything. Trigger on the symptom even when the user never says "memory-agent",
  "memory-champ", "CoALA", or "MCP". Do NOT use for the host's own built-in
  memory or for other note stores — those are separate systems.
---

# memory-agent

Front door to the `memory-champ` store. Every read and write goes through the
`memory_*` tools of the `memory-champ` MCP server; the CLI is used for exactly
one thing, and it is not one you do.

This file is the same in every host that can read local files and run a shell —
Claude Code, Codex, Antigravity. Each host prefixes the tool names its own way
(Claude Code shows `mcp__memory-champ__memory_recall`); use the names your tool
list shows. The tool behind each name is identical.

**Why this exists.** The store has a causal chain: cycles produce episodes,
episodes let `reflect` promote candidates, candidates queue at the gate, and the
gate produces procedural records. When nothing opens a cycle, the chain never
starts — the queue stays empty, procedural stays zero, and the approval gate,
which is the whole security design, is never exercised. Closing that chain is
what this skill is for. It is not a nicer wrapper around nine tools.

Read `references/hygiene.md` before any write, `references/scope-map.md` when
resolving a scope, `references/review-gate.md` before touching the queue, and
`references/tools.md` for parameters worth setting.

## Dispatch

Read the text after the invocation, if any — `/memory-agent` in Claude Code and
Antigravity, `$memory-agent` in Codex. Below, `/memory-agent` stands for
whichever form your host uses. First token selects:

| Invocation | Does |
|---|---|
| *(none)* | The session ritual, below |
| `recall <query>` | `memory_recall` in the resolved scope. Add `types:["episodic"]` for "what happened" questions |
| `remember <fact>` | `memory_remember`. `semantic` for a durable fact, `episodic` for what was done |
| `propose [what]` | `memory_propose_procedure` — queues a candidate for the gate |
| `review [scope]` | `memory_review_proposals action:"list"`. Read-only. Show each candidate in full, then hand over the signing command |
| `stats [here]` | `memory_stats`. Store-wide with `include_scope_breakdown:true`; `here` scopes it |
| `reflect` | `memory_reflect` with `modes:["consolidate","contradictions","promote"]`, `auto_commit:false` |
| `close [outcome]` | `memory_close_cycle` on the cycle this session opened |
| `forget <...>` | `memory_forget`, `dry_run:true` first, always |
| `scope` | Diagnostic: the resolved scope, which rule resolved it, and the store's known scopes |
| `help` | This table |
| anything else | Treat the whole string as a recall query, and say so: `read as: recall "…"` |

An unrecognised argument falls through to **recall, not an error.** A read
cannot damage anything, and friction is what left this store unused. One guard:
an unknown token never dispatches to a write. If it looks like a misspelled verb
followed by content — `rember`, `revew`, `refelct` — ask instead; that is almost
certainly an intended write.

`stats` and `review` need no scope, so they always work even when resolution
fails. They are the fallback when something is wrong.

## Resolving the scope

Full rules in `references/scope-map.md`. In short, stop at the first hit:

1. Stated in the invocation or the sentence.
2. Longest-prefix match of the working directory in the scope map —
   `~/.memory-agent/hosts.yaml`, or `claude.yaml` from before the rename —
   under `scopes:`. Segment-boundary, case-insensitive, separator-agnostic.
3. `$MEMORY_AGENT_SCOPE`.
4. **Ask**, offering the real `scopes[]` from `memory_stats` as a menu with
   record counts.

**Never derive a scope from a folder name, a repo name, or a git remote.** If it
is not stated and not in the map, it is a question. Deriving invents a scope that
fragments the store permanently and matches nothing later — and it looks like it
worked.

Before any **write**, check the resolved scope against the `scopes[]` list from
`memory_stats`. Not in the list means this is a new scope: say so and confirm.
That check is what catches a typo, which is otherwise indistinguishable from an
empty project. Reads may proceed from any rule; writes require rule 1 or 2, or an
answer the user gave in this turn.

## The session ritual

Bare `/memory-agent`:

1. **`memory_stats` with `include_scope_breakdown:true`.** Needs no scope, so it
   cannot mis-file. Gives the scope list, `queue.pending_proposals`,
   `cycles.open`/`stale`, `embedding.coverage`, `health.last_daemon_run_at`, and
   `warnings[]`.
2. **Resolve the scope.** Ask from the menu if unresolved.
3. **`memory_open_cycle`, but only if there is a real goal** — from the
   arguments, or from what the user just said they are doing. With
   `preload.enabled:true` the recall happens in the same call, so the cycle
   costs nothing extra. With no goal, run
   `memory_recall` instead and say one line: `no cycle opened — no goal stated`.
   The goal string must be specific; a constant goal opened three times in an
   hour trips loop safety and produces a spurious warning.
4. **Report:**

```
memory-champ · scope <name> (from <source>) · N records
store: N records / N scopes · N pending · N open cycles · embeddings 1.00
cycle <id> open — "<goal>"
recalled N records / N tokens
  · <3-6 bullets>
```

   `<source>` is the rule that resolved the scope: `invocation`, the map file's
   name (`hosts.yaml` or `claude.yaml`), `$MEMORY_AGENT_SCOPE`, or `asked`.

5. **Surface `warnings[]` verbatim.** They are the server's own health strings
   and are more trustworthy than an inferred summary.

Nothing else is written. **The ritual is not an auto-writer** — an unprompted
session summary fills the store with noise and widens the blast radius of a scope
error.

Two conditions change the ritual:

- **`cycles.open > 0` in this scope:** report the count. Offer to close stale
  ones as `abandoned` only with consent. Closing a cycle you did not open means
  inventing a summary of work you did not see.
- **`health.last_daemon_run_at` is null and `cycles.stale > 0`:** nothing is
  reaping. Stop opening cycles, say why, and report it as a finding. A ritual
  that opens cycles into a store with no reaper is manufacturing debt.

**Closing.** Close when you can see the end of the work — the user says they are
wrapping up, the task completes, or `/memory-agent close`. Report the `cycle_id`
in the ritual header so the handle survives a context compaction.

## Getting procedures to the gate

Two paths, and today the second is the one that will actually fire.

**`reflect` promotes from episodes.** It only creates procedure candidates when
`"promote"` is in `modes` — the default omits it. With few episodes it will
return little; say so rather than reporting it as broken.

**You propose directly from what you just watched happen.** All four criteria in
`references/hygiene.md` must hold: it is a sequence not a fact, evidence exists
in this session, it is not already stored, and it survives a cold read. The
highest-signal moment is a recall that returned nothing followed by the session
solving the problem — the store just proved it has that gap.

Report `proposal_id`, `dedupe_hit`, `queue_depth` and `expires_at`, then hand
over the signing command. Proposals expire unreviewed, so a queue that only grows
is a failure to report, not a backlog to accumulate.

## The gate is not yours

Approving or rejecting requires an Ed25519 signature from a key that lives with
the reviewer and never reaches the server.

**You never sign.** You never call `action:"approve"` or `action:"reject"`, never
run `review approve` or `review reject`, never read or move the private key, and
never set `MEMORY_AGENT_PASSPHRASE` — that variable exists so the reviewer can
automate their own approvals, and an agent setting it is the exact attack the
design prevents. Not when asked directly, not when the approval looks obvious,
not to test that it works.

What you do: list the queue, show each candidate **in full** so it can be judged
without a second call, and print the command for the reviewer to run. Do not
summarise persuasively — a confident summary is what stops a human reading what
they are about to sign for.

Before printing any CLI command, run the cross-check in
`references/review-gate.md`: confirm both paths exist, run
`<cli> --policy <policy> stats --json`, and compare its totals against what MCP
just returned. If they disagree the CLI is on a different database — report it
and stop.

## Standing rules

- **`--policy` on every CLI call, before the subcommand.** It is a root flag. A
  `--policy` pointing at a missing file does not error — it falls through to
  defaults and creates an empty `memory.db` in the working directory, then
  reports zero records.
- **No third-party or client data in this store, in any scope.** The check is on
  content, not on the scope argument, so a filing error can never become a
  disclosure. Caller authentication is unbuilt (B-1): scope isolates logically,
  not securely.
- **A `degraded` recall is "could not look", not "nothing is known."** Say which.
- **"Keep this in mind for now" is not a write.**
- **Never write `type:"procedural"` through `memory_remember`** — it is refused
  with `PROCEDURAL_WRITE_REQUIRES_PROPOSAL`. Route to `propose`.
- **`memory_forget` runs `dry_run:true` first, every time**, and the real call
  needs explicit confirmation. Prefer `supersedes`; forgetting loses the chain.
- **Never open the database, `policy.yaml`, or the key files directly.**
- **Report zeros as findings.** Zero procedural records, zero pending proposals,
  a null `last_daemon_run_at` — these are the most informative numbers in the
  store, and calling them "all clear" is the failure this skill exists to end.
