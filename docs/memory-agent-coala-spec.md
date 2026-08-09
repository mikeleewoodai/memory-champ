# Memory Agent — CoALA Specification

*Version 1.0 · Runtime: Python · Storage: SQLite + sqlite-vec · Interface: MCP · Status: implemented — see `../src/memory_agent/` and `../tests/`*

*This document is the handoff to whoever maintains it — it must stand on its own. Assume the builder has never read the CoALA paper and has no context from the conversation that produced this. Everything needed to implement, test, and operate the agent is either here or in `../contracts/`.*

---

## 1. Executive summary

A memory service for agent orchestrations, structured on CoALA — the cognitive architecture proposed in *Cognitive Architectures for Language Agents* (Sumers, Yao, Narasimhan & Griffiths, Princeton; TMLR 2024; arXiv:2309.02427).

It stores three kinds of long-term memory, not one: **what happened** (episodic), **what is true** (semantic), and **how to do something** (procedural). It exposes them over MCP as nine tools, so any orchestration that speaks MCP can attach to it. It also runs on its own schedule, consolidating and pruning its own store with no host present.

What it refuses to do is as important as what it does. It has **no grounding actions**: it never reaches the network, never touches the filesystem outside its own database file, never talks to a user. It will not overwrite a fact that contradicts a new one — it reports the conflict and keeps both. It will not learn a new procedure without a human approving it. It cannot exceed the context budget a caller gives it.

Those four refusals are why it is safe to drop into an arbitrary agentic loop. The worst thing it can do is return a bad memory.

---

## 2. Quick start

Register the server:

```json
{
  "mcpServers": {
    "memory": {
      "command": "python",
      "args": ["-m", "memory_agent.server"],
      "env": { "MEMORY_AGENT_POLICY": "./policy.yaml" }
    }
  }
}
```

Three calls get an orchestration remembering. **Recall before you act:**

```json
{ "tool": "memory_recall",
  "arguments": { "scope": "acme.crm", "query": "how does this client want invoices formatted", "max_tokens": 1200 } }
```

Paste `context_block` straight into your prompt. It is measured, not estimated, and will not exceed `max_tokens`.

**Remember after you learn something:**

```json
{ "tool": "memory_remember",
  "arguments": { "scope": "acme.crm", "type": "semantic",
    "content": "Acme wants invoices as PDF with the PO number in the subject line.",
    "semantic": { "subject": "acme", "predicate": "invoice_format", "object": "PDF, PO number in subject" },
    "idempotency_key": "run-4417-invoice-pref" } }
```

**Reflect when the run ends:**

```json
{ "tool": "memory_reflect", "arguments": { "scope": "acme.crm", "window": { "session_id": "run-4417" } } }
```

That is the whole loop. Everything else — cycles, procedures, forgetting — is optional structure on top.

Two rules that matter more than they look:

- **Always pass `idempotency_key` from inside a loop.** Without it a retried iteration writes a near-duplicate, and near-duplicates are what turn a useful memory into noise.
- **Never read a thin result as "nothing is known"** without checking `degraded`. It distinguishes *looked and found little* from *could not look properly*.

---

## 3. CoALA in one page

CoALA frames a language agent as a cognitive architecture rather than a prompt with tools attached. Three parts matter here.

**Memory is not one thing.** CoALA separates short-term *working memory* — the active state of the current decision cycle — from *long-term memory*, which divides into:

| Module | Holds | Example |
|---|---|---|
| Episodic | The agent's own past experience, as sequences | "On run 4417 I sent the invoice as a Word doc and the client asked for PDF" |
| Semantic | Facts about the world and the agent itself | "Acme wants invoices as PDF" |
| Procedural | How to do things | "When invoicing Acme: export PDF, put the PO number in the subject, cc billing" |

The distinction is load-bearing. An episode is evidence and is never wrong — it happened. A fact is a claim and can be contradicted. A procedure is an instruction and can be harmful if wrong. They therefore get different write rules, different retention, and different levels of trust.

**Actions are typed.** CoALA splits the action space into *internal* actions that operate on the agent's own knowledge — **retrieval** (read long-term into working memory), **reasoning** (reads from *and* writes to working memory, via the LLM), **learning** (write to long-term memory) — and *external* **grounding** actions that touch the world: physical, dialogue, digital.

**Decision-making is a cycle with two stages.** Each cycle "yield[s] an external grounding action or an internal learning action" (§4.6). Getting there has two stages:

| Stage | What happens | Actions used |
|---|---|---|
| **Planning** | *Proposal* generates candidate actions; *evaluation* assigns each a value; *selection* picks one or loops back to proposal. The sub-stages may interleave and iterate. | Reasoning and retrieval, which support planning rather than being the cycle's output |
| **Execution** | The selected action is applied, an observation comes back, the cycle loops. | Grounding **or** learning |

The asymmetry matters for this design. Retrieval and reasoning are *how the agent decides*; grounding and learning are *what a cycle produces*. Since this agent has no grounding actions at all, **every cycle it executes can only ever yield a learning action** — which is precisely what the independent daemon does.

### How this agent maps onto it

| CoALA element | Here |
|---|---|
| Working memory | `working_set` + `observations` tables, TTL 24h, keyed by `cycle_id` |
| Episodic memory | `records` + `episodic_attrs` |
| Semantic memory | `records` + `semantic_attrs` |
| Procedural memory | `records` + `procedural_attrs`, approval-gated |
| Internal action: retrieval | `memory_recall` |
| Internal action: reasoning | `memory_reflect` — strictly, reasoning *composed with* learning, since it distils and then writes. §4.4: "Reasoning can be used to support learning (by writing the results into long-term memory)" |
| Internal action: learning | `memory_remember`, `memory_forget`, `memory_propose_procedure` |
| External grounding actions (physical / dialogue / digital) | **None. Deliberate.** See §4 |
| Decision cycle — planning stage | `memory_recall` and `memory_reflect` are the retrieval and reasoning a host uses to propose, evaluate, and select |
| Decision cycle — execution stage | `memory_remember`, `memory_forget`, `memory_close_cycle`, `memory_propose_procedure` — all learning actions, since there are no grounding ones |
| Learning modality: update episodic with experience | `memory_remember` (type episodic), `memory_close_cycle` |
| Learning modality: update semantic with knowledge | `memory_remember` (type semantic), `memory_reflect` |
| Learning modality: update agent code (procedural) | Narrowed to stored routines: `memory_propose_procedure` → human gate → `memory_review_proposals`. The agent's own code is never rewritten |
| Learning modality: update LLM parameters (procedural) | **Out of scope.** See §4 |
| "Modifying and deleting (a case of 'unlearning')" — named in §4.5 and §6 as understudied | `memory_forget`, with three escalating modes. See §8 |

### Two deliberate narrowings

**Procedural memory is data, not code.** CoALA's procedural memory is "implicit knowledge stored in the LLM weights, and explicit knowledge written in the agent's code" (§4.1) — and the authors are blunt about the risk: writing to it is "significantly riskier than writing to episodic or semantic memory, as it can easily introduce bugs or allow an agent to subvert its designers' intentions." §6 puts it in the action space directly: "'Learning' actions (especially procedural deletion and modification) could cause internal harm."

Here a procedure is a natural-language routine a host may *choose* to follow. The memory agent never executes it, and never applies one to itself. Every procedural write is additionally gated on human approval.

One consequence worth naming: CoALA notes procedural memory "must be initialized by the designer with proper code to bootstrap the agent." That bootstrap layer here is the memory agent's own source, which is fixed. What the store holds is a *learned* layer on top, which legitimately starts empty.

The paper's own worked example endorses exactly this asymmetry. Designing a retail assistant, §6 gives the agent "read and write access to episodic memory ... but read-only access to semantic and procedural memory (since it should not update the inventory or its own code)." Differentiated write access per memory module is the intended design move, not a deviation from it.

### Cast into the paper's own terms

CoALA's Table 2 characterises agents on four axes. This one, added in the same format:

| Agent | Long-term memory | External grounding | Internal actions | Decision making |
|---|---|---|---|---|
| SayCan | — | physical | — | evaluate |
| ReAct | — | digital | reason | propose |
| Voyager | procedural | digital | reason / retrieve / learn | propose |
| Generative Agents | episodic / semantic | digital / agent | reason / retrieve / learn | propose |
| Tree of Thoughts | — | digital | reason | propose, evaluate, select |
| **This memory agent** | **episodic / semantic / procedural** | **none** | **reason / retrieve / learn** | **attached: delegated to host · independent: propose, evaluate, select** |

The empty grounding column is the whole point. It is the only row in the table with all three long-term memory modules and no external action space at all — a specialist in the internal half of CoALA's action space, designed to be attached to agents that own the external half.

**Working memory belongs to the agent, not the host.** This service does not try to own or mirror the host's context window. It returns bounded blocks; the host decides what to put in its prompt. The working set exists so a crashed loop can be reconstructed and a stuck loop can be spotted — not to be the host's scratchpad.

---

## 4. Goals and non-goals

### Goals

1. Give an orchestration memory that survives the run, structured by kind rather than dumped into one bucket.
2. Attach to any MCP-speaking orchestration with no code change on either side.
3. Run standalone, improving its own store between sessions.
4. Behave correctly inside a retrying, crashing, or looping caller.
5. Stay inspectable: every stored item traceable to where it came from and why it was kept.
6. Run offline on one machine, with the whole memory in one copyable file.

### Non-goals

| Not this | Why |
|---|---|
| A RAG document store | Documents are inputs to memory, not memory. Chunking a PDF into this would drown the signal — episodes and facts are small, numerous, and mutually related in ways document chunks are not. Keep the doc store separate and write *conclusions* here. |
| A general-purpose vector database | Retrieval here is deliberately opinionated: recency and importance are first-class, not post-filters. Anything wanting raw ANN over millions of vectors wants a different tool. |
| Grounding actions of any kind | The single strongest safety property of this design. No network, no filesystem outside the DB, no user dialogue. A compromised or confused host cannot use memory as a lateral-movement tool. The paper argues both halves of this independently: §6 "Safety of the action space" observes that grounding actions "could cause external harm", and §6 "Size of the action space" recommends "taking the minimal action space necessary to solve a given task". |
| LLM parameter updates | CoALA lists fine-tuning as a learning modality. It is excluded: it needs training infrastructure, it is not reversible in the way every other write here is, and the value at this scale is not there. |
| Self-modifying procedural memory | See §3. Procedures are data a host may follow, never code the agent runs on itself. |
| Multi-tenant authentication | v1 has none. Scope isolates logically, not securely. See assumption A-3 — this must be fixed before any shared deployment. |
| Distributed / multi-writer operation | One node, one writer. See A-2 and §14. |

---

## 5. Architecture

```
   ATTACHED MODE                                    INDEPENDENT MODE
   host orchestration runs the cycle                daemon runs the cycle over the store

   ┌──────────────────────────┐                     ┌──────────────────────────┐
   │  Host agent / orchestr.  │                     │  memory_agent.daemon     │
   │                          │                     │  (cron / scheduled task) │
   │  propose ─ evaluate ─    │                     │                          │
   │  select ─ EXECUTE        │                     │  propose:  candidates    │
   │     │           │        │                     │  evaluate: thresholds    │
   │  retrieval   grounding   │                     │  select:   respect gates │
   │     │        (host's own)│                     │  execute:  writes        │
   └─────┼────────────────────┘                     └────────────┼─────────────┘
         │ MCP (stdio / http)                                    │ in-process
         ▼                                                       ▼
   ┌───────────────────────────────────────────────────────────────────────────┐
   │                          MEMORY AGENT                                     │
   │                                                                           │
   │   recall      remember    reflect    forget    propose   review   stats    │
   │  (retrieval)  (learning) (reasoning)(learning)  (gated)  (gate)            │
   ├───────────────────────────────────────────────────────────────────────────┤
   │   Retrieval engine        Gate            Working memory                   │
   │   RRF(vector, BM25)       proposals       working_set + observations       │
   │   + recency + importance  table           TTL 24h · loop detection         │
   ├───────────────────────────────────────────────────────────────────────────┤
   │                        memory.db  (one SQLite file)                        │
   │   records ─ episodic_attrs · semantic_attrs · procedural_attrs             │
   │   records_fts (FTS5)   records_vec (sqlite-vec)   proposals   meta         │
   └───────────────────────────────────────────────────────────────────────────┘
                                     │
                          embedder (local by default)
```

### Attached mode

The host owns the decision cycle. The memory agent serves it and holds no state between calls beyond the database.

```
host: memory_open_cycle(goal)      →  cycle_id, preloaded context, loop_warning
host: ... its own reasoning ...
host: ... its own grounding actions ...
host: memory_remember(observation) →  written before acting on the result
host: memory_close_cycle(outcome)  →  observations promoted to episodic
host: memory_reflect(session)      →  facts distilled, procedures proposed
```

Cycles are optional. A host that only calls `recall` and `remember` gets a working memory service with no ceremony; cycles add trajectory reconstruction, preloading, and loop detection.

### Independent mode

A scheduled process runs the CoALA cycle over the agent's own memory, with no host present. This is what makes memory improve *between* sessions rather than only during them.

Because the agent has no grounding actions, its cycles can only yield learning actions — so the daemon is a pure learning loop, and every stage below maps onto §4.6.

| CoALA stage | What the daemon does |
|---|---|
| **Planning** — proposal | Retrieval and reasoning over the store: cluster unconsolidated episodes, find repeated successful action patterns, find contradicting facts, find expired TTLs, stale cycles, and drifted embeddings |
| **Planning** — evaluation | Score candidates on cluster size, corroboration count, reward, and access history. Optional LLM adjudication for contradictions the triple form cannot settle — off by default, because an unattended model call that rewrites stored facts is exactly the failure this design avoids |
| **Planning** — selection | Rank by value, cap per run, and **respect the gates**: semantic may commit, procedural may only queue |
| **Execution** | The selected learning action is applied: write semantic facts with provenance, queue procedural proposals, reap abandoned cycles, expire TTLs, re-embed drifted records, vacuum. Then the cycle loops |

Both modes call the same code paths. `memory_reflect` is the daemon's consolidation stage exposed as a tool, so an attached host can trigger learning at the end of a run instead of waiting six hours.

### Retrieval

```
score = w_relevance · relevance + w_recency · recency + w_importance · importance

relevance = RRF(vector_rank, bm25_rank)      reciprocal-rank fusion, k = 60
recency   = exp(-ln2 · hours_since_last_access / half_life_hours)
importance ∈ [0,1], set at write time, revised by reflection
```

Defaults `0.5 / 0.2 / 0.3`, half-life 72h, all in `policy.yaml`. The composition of relevance, recency and importance follows the Generative Agents retrieval function (Park et al., 2023), which slots cleanly into CoALA's retrieval action.

Reciprocal-rank fusion rather than score normalisation, because cosine similarity and BM25 are not on comparable scales and any attempt to normalise them is a fudge factor that drifts with corpus size.

Every returned record carries its full score breakdown, so retrieval is tunable rather than a black box.

### Embeddings

Local by default — `sentence-transformers` / all-MiniLM-L6-v2 / 384 dimensions — so the agent runs fully offline and no memory content leaves the machine. The embedder is an interface; hosted providers can be swapped in. Note that Anthropic ships no embeddings model, so a hosted option means a third party such as Voyage, which is a data-egress decision, not just a quality one.

Changing model invalidates every stored vector. `records.embedding_model` makes the mismatch detectable; the daemon re-embeds in the background while recall stays available on the keyword path.

---

## 6. State

*What persists, where, and whether it survives a restart.*

| State | Where | Survives restart | Survives DB file loss | Notes |
|---|---|---|---|---|
| Episodic, semantic, procedural records | `memory.db` → `records` + attrs | Yes | No | The whole memory is one file. Back it up or it is the single point of failure |
| Keyword index | `memory.db` → `records_fts` | Yes | No | Derived; rebuildable from `records` |
| Vector index | `memory.db` → `records_vec` | Yes | No | Derived; rebuildable by re-embedding, which costs time but loses nothing |
| Proposal queue | `memory.db` → `proposals` | Yes | No | Pending approvals survive a restart by design — an unattended gate that forgets is not a gate |
| Working sets (open cycles) | `memory.db` → `working_set` | Yes | No | Persisted, not in-process. A server restart mid-cycle does not lose the trajectory |
| Observations | `memory.db` → `observations` | Yes | No | Written before the host acts, so a crash leaves evidence rather than a hole |
| Approval signatures | `memory.db` → `procedural_attrs`, `proposals` | Yes | No | The signed payload is stored verbatim, so approvals re-verify offline from the record alone |
| Approval challenges (nonces) | `memory.db` → `approval_challenges` | Yes | No | Single-use and short-lived. Consumed rather than deleted, so a replay attempt is visible |
| Reviewer **public** keys | `policy.yaml` on disk | Yes | Yes | Public halves only |
| Reviewer **private** keys | **Never on this machine's server config** — the reviewer's own keystore | n/a | n/a | This is the whole basis of the approval gate. If a private key ends up in the server's config or environment, the gate is gone |
| Policy | `policy.yaml` on disk | Yes | Yes | Not in the DB. A corrupted DB does not take the configuration with it |
| Embedder weights | Model cache on disk | Yes | Yes | ~90MB for the default model; first run downloads unless pre-seeded |
| MCP session state | None | n/a | n/a | The server is stateless between tool calls. Everything is in the DB |

Two consequences worth stating plainly:

- **The server holds nothing in memory that matters.** Kill it mid-loop and restart: open cycles are still open, observations are still there, nothing is lost. This is why working memory is a table and not a dict.
- **`memory.db` is the whole product.** Copy it to move an agent's memory to another machine. Lose it and everything except policy is gone. Backup is an operational requirement, not a nice-to-have.

---

## 7. The data layer

Full DDL: `../contracts/db/schema.sql`. JSON Schema for every record type: `../contracts/schemas/`. This section explains the parts where the *reason* is not obvious from the SQL.

### Base + extension tables

`records` holds the envelope common to all three memory types; `episodic_attrs`, `semantic_attrs`, `procedural_attrs` hold what is specific to each, keyed one-to-one and cascading on delete. This mirrors the schema inheritance in the contracts and lets each type enforce its own NOT NULLs — a wide nullable table cannot say "an episode must have a step number" without giving up on the constraint entirely.

### `content` is the only searchable field

Exactly one column is embedded and keyword-indexed: `records.content`. Everything else — `payload`, the triple form, the steps of a procedure — is structure for machines and is not searchable.

This is a **writer-side invariant**: whatever a future reader needs must appear in `content`, even if it also appears in a structured field. A procedure whose trigger is only in `procedural_attrs.trigger_text` will not be found by a recall that describes that trigger. The alternative — indexing several columns — was rejected because it makes relevance scoring depend on which column matched, which is exactly the kind of hidden behaviour that makes retrieval untunable.

### Idempotency

`UNIQUE (scope, idempotency_key) WHERE idempotency_key IS NOT NULL`.

A partial index, so records without a key never collide. This one line is the loop-safety primitive: a host that passes its loop-iteration id gets exactly-once write semantics for free, and a retry returns the original record rather than a second copy.

### Supersession is atomic or not at all

```sql
CHECK ((status = 'superseded') = (superseded_by IS NOT NULL))
```

A record cannot be marked superseded without naming its successor, and cannot name a successor without being marked. The half-applied case — status flipped, pointer not set — is precisely how a memory store starts returning stale facts while appearing healthy. The constraint makes it unrepresentable.

### The proposal queue is a separate table

Unapproved candidates live in `proposals`, not in `records` with a flag. A flag would put the entire safety property at the mercy of every query remembering its `WHERE approval_state = 'approved'`. A separate table means an unapproved procedure is **unreachable by recall even if a query is wrong**, because it is not in the table recall reads.

Correspondingly, `procedural_attrs.approval_state` is constrained to `('approved','rejected')` — a row's mere existence in that table means the gate was passed.

### Nothing is destroyed by default

`status` has four values and only `active` is recallable:

| Status | Meaning | Reversible |
|---|---|---|
| `active` | Normal | — |
| `superseded` | Replaced by a newer record, chain preserved | Yes |
| `tombstoned` | Hidden, retained for the retention window | Yes |
| `redacted` | Content destroyed, record shape and provenance kept | No, but auditable |

Only an explicit `hard_delete` with `confirm: true` removes bytes. Everything else is reversible or at least leaves a trace, because a memory that can quietly lose things is a memory nobody can reason about.

### Views encode the questions worth asking

`v_recallable` is the only thing retrieval reads — it excludes non-active and expired-but-not-yet-swept records, so correctness does not depend on the daemon having run recently. `v_stale_cycles`, `v_failing_procedures`, `v_expired_facts`, and `v_pending_proposals` are the health signals surfaced by `memory_stats`.

`v_failing_procedures` deserves note: it lists procedures with at least five invocations and under 50% success. That is the feedback loop keeping procedural memory honest — a learned procedure that does not work should stop being recommended before a human gets round to retiring it.

---

## 8. The action contract

Full contract with input and output schemas: `../contracts/mcp-tools.json`. Nine tools.

| Tool | CoALA | Read-only | Destructive | Idempotent |
|---|---|---|---|---|
| `memory_open_cycle` | working memory | no | no | yes |
| `memory_close_cycle` | working memory → learning | no | no | yes |
| `memory_recall` | retrieval | yes | no | yes |
| `memory_remember` | learning | no | no | yes |
| `memory_forget` | learning (negative) | no | **yes** | yes |
| `memory_reflect` | reasoning | no | no | no |
| `memory_propose_procedure` | learning (gated) | no | no | yes |
| `memory_review_proposals` | learning gate | no | no | yes |
| `memory_stats` | introspection | yes | no | yes |

`openWorldHint` is false on all nine. There is no tenth tool that touches the world.

### The contract details that carry weight

**`memory_recall` returns two shapes for two callers.** `records[]` with full score breakdowns for a host that reasons over structure; `context_block` for a host that wants text. The block is token-*measured*, not estimated, and never exceeds `max_tokens`. That guarantee is what makes recall safe on every iteration of a loop.

When nothing is found, `context_block` is the empty string — never a sentence like "No relevant memories found." A model handed that sentence will treat it as a retrieved fact and reason from it.

**`memory_remember` cannot write procedural memory.** `type` accepts only `episodic` and `semantic`; `procedural` returns `PROCEDURAL_WRITE_REQUIRES_PROPOSAL`. The gate is in the type system, not in a policy check that could be reconfigured.

**Contradictions are reported, never resolved.** Writing a fact that conflicts with an existing one leaves both active and returns `contradictions_detected`. The memory agent has no basis for picking a winner — it does not know which source is more reliable, and guessing is how a store starts lying with confidence.

**`memory_forget` requires a declared blast radius.** `max_records` is mandatory: state how many records you expect to touch, and the call aborts unchanged with `BLAST_RADIUS_EXCEEDED` if the selector matches more. A selector matching more than the caller expected is a bug, not a big job. `reason` is also mandatory and stored — a forget with no recorded reason is indistinguishable from data loss.

**`memory_close_cycle` on a closed cycle is a no-op, not an error.** It returns `already_closed: true`. A retried loop iteration must not fail here.

**`memory_propose_procedure` can only ever return `pending`.** The output schema pins `state` to a const. There is no parameter that approves on write and none will be added.

### Signed approvals

`reviewed_by` used to be a self-asserted string. Anything holding the tool could write a person's name against a decision. The gate stopped an agent from *writing* a procedure while leaving it free to *approve* one — which is the same thing with extra steps.

Approving or rejecting now requires an **Ed25519 signature** from a reviewer key. The private half lives with the reviewer and never reaches the server, which holds only public keys. An agent has the tool and not the key, so it cannot manufacture a decision.

Two calls:

```
1. memory_review_proposals(action="list")
     -> for each pending proposal: the candidate, its sha256,
        and `signing_payload` — the exact bytes to sign, carrying
        a single-use server nonce and that hash

2. sign those bytes locally, then
   memory_review_proposals(action="approve", proposal_ids=[...],
                           reviewed_by="mike", signatures=[...])
```

The payload is fixed-field and newline-delimited, never JSON — canonical JSON is a footgun (key order, unicode escaping, number formatting), and a verifier that re-serialises can disagree with the signer:

```
memory-agent-approval-v2
scope: acme.crm
proposal: p_01J9X2QK7M
candidate_sha256: bb46d3d9...307d0940
decision: approve
reviewer: mike
nonce: 3f9a1c77b204e8d6
expires: 2026-08-08T14:12:00Z
```

To reject, flip the `decision:` line and sign that. One challenge authorises one decision, either way. A nonce that is unknown, expired, or already spent returns `APPROVAL_CHALLENGE_INVALID` — retryable by re-listing for a fresh challenge, but a repeat failure on a fresh one means the signature itself is wrong, not the timing.

What each field buys:

| Field | Attack it closes |
|---|---|
| `candidate_sha256` | Approving a benign procedure, then editing it before it lands. Covers `content`, `trigger`, `preconditions`, `steps`, `success_signal`, `failure_signal` — including `content`, which is the text recall returns and an agent follows, so the signature covers the exact instructions shown |
| `nonce` (single-use, 10 min) | Stockpiling pre-signed approvals, or replaying an old one |
| `proposal` + `scope` | Reusing a signature from one proposal against another |
| `decision` | Turning a signed rejection into an approval |
| `reviewer` | Attributing a decision to someone who did not make it |

**The signed payload is stored verbatim on the record**, not reconstructed at verification time. That distinction is the point: reconstructing from current field values would prove only that the row is self-consistent. Storing the signed bytes means that months later, with nothing but the record and a public key, the decision re-verifies offline — and a procedure edited after approval fails that check instead of quietly continuing to be recalled as approved. The daemon re-verifies on a sweep; mismatches surface in `v_approval_audit` and must be treated as unapproved.

Rejections are signed too. A forged rejection is a denial of service on the agent's own learning, and it puts a name against a decision that was not made.

Key handling: an existing SSH key can be reused — public keys load in OpenSSH `ssh-ed25519 AAAA...` form, so there is no new key material to manage. Retiring a key keeps its old signatures verifiable while refusing new decisions; revoking one invalidates everything it approved and surfaces those procedures for re-review.

**What this does and does not defend against.** It makes agent self-approval structurally impossible, and it makes every approval durably provable and tamper-evident. It does not defend against an attacker who already has code execution as you and can read an unencrypted key file — that is what `require_passphrase`, or a hardware-backed key via `ssh-agent`, is for. And it protects only the approval path: `memory_remember` writes remain unauthenticated, as does the transport. Those are B-1. See §12 A-3.

### Errors

Eleven codes, in `mcp-tools.json`. Three worth knowing before writing a client:

- `IDEMPOTENCY_CONFLICT` — same key, different payload. The original record comes back in the error detail so a caller can see what it collided with.
- `WRITE_RATE_EXCEEDED` — retryable, but almost always a runaway loop that ignored a `loop_warning`. Fix the loop, not the retry.
- `VECTOR_UNAVAILABLE` — an error when `require_vector_extension: true`, a `degraded` flag on the response when false. Choose deliberately: failing loudly beats silently returning worse results.

---

## 9. Integration patterns

### Pattern A — attached to a host loop

The common case. The host owns control flow; memory serves it.

```
open_cycle(goal) ──▶ loop_warning?  ─yes─▶ change approach or escalate
      │
      ▼ preloaded context
   host reasons and acts
      │
      ▼ remember(observation)   ← before acting on the result, not after
   ... iterate ...
      │
      ▼ close_cycle(outcome)
      ▼ reflect(session_id)      ← at the end of the run, not every step
```

Pass `idempotency_key` on every write. Check `loop_warning` on every `open_cycle`. Reflect once per run — reflecting every step burns tokens producing facts that the next step invalidates.

### Pattern B — independent daemon

No host. A cron entry or scheduled task runs `python -m memory_agent.daemon` on a schedule. It consolidates, prunes, reaps, re-embeds, and queues proposals. It cannot approve them. A human drains the queue with `memory_review_proposals` when convenient.

Use this when memory should improve between sessions, which is nearly always. Attached-only memory grows monotonically and gets slower and noisier.

### Pattern C — shared memory across a multi-agent fan-out

Several subagents, one store, isolated by `scope`.

```
orchestrator            scope: acme.crm
  ├─ researcher         scope: acme.crm          shared, reads+writes
  ├─ writer             scope: acme.crm          shared
  └─ scratch worker     scope: acme.crm.scratch  isolated, TTL'd
```

Give every subagent `parent_cycle_id` so the tree is reconstructable. Give throwaway workers their own child scope with a short TTL so exploratory noise never reaches the shared store. Writes serialise on SQLite's single writer; with WAL and a 5s busy timeout this is a non-issue below roughly ten concurrent writers.

### Pattern D — cold-start seeding

Bulk-load known facts with `provenance.source: "import"` before the first run, so the agent starts with what is already known rather than rediscovering it. Set `importance` deliberately at seed time; seeded facts with default importance get outranked by trivia the agent happens to observe.

### Anti-patterns

| Don't | Why |
|---|---|
| Use it as a document store | Chunked documents drown episodes and facts in the ranking. Keep the doc store separate; write conclusions here |
| Let every subagent write to the shared scope | Exploratory noise becomes permanent. Give scratch workers a child scope |
| Reflect on every loop iteration | Expensive, and produces facts the next iteration invalidates. Reflect per run |
| Recall without `max_tokens` | The default is 1500 and it will apply. If your budget is smaller, say so |
| Treat `degraded` results as authoritative absence | "Could not look properly" is not "nothing is known" |
| Skip `idempotency_key` in a loop | The single most common cause of a memory store filling with near-duplicates |
| Auto-approve procedures to "move faster" | The gate is the design. An agent that teaches itself a wrong procedure at 3am repeats it every run thereafter |

---

## 10. Requirements and acceptance criteria

Numbered, testable, and written against the defaults in `policy.example.yaml`.

### Functional

**F1 — Bounded recall.** `memory_recall` never returns a `context_block` exceeding the caller's `max_tokens`.
*accept:* Given 200 records each ~500 tokens and `max_tokens: 1000`, the response has `token_count <= 1000`, `truncated: true`, and `omitted_count > 0`.

**F2 — Honest degradation.** Recall run without the vector path reports it.
*accept:* With sqlite-vec unloaded and `require_vector_extension: false`, a recall returns results with `degraded.reason: "vector_unavailable"` and a non-empty `records` array from the keyword path.

**F3 — Scope isolation.** Recall never returns records outside the requested scope.
*accept:* Given identical content written to `proj.a` and `proj.b`, a recall on `proj.a` returns only the `proj.a` record, for every value of `strategy`.

**F4 — Idempotent writes.** A repeated write with the same key does not create a second record.
*accept:* Three `memory_remember` calls with identical payload and `idempotency_key: "k1"` in one scope produce exactly one row; calls 2 and 3 return the same `record_id` with `created: false, deduped: true`.

**F5 — Procedural writes are refused on the direct path.**
*accept:* `memory_remember` with `type: "procedural"` returns `PROCEDURAL_WRITE_REQUIRES_PROPOSAL` and writes nothing.

**F6 — Contradictions surfaced, not resolved.**
*accept:* Writing `(acme, invoice_format, "PDF")` then `(acme, invoice_format, "DOCX")` leaves both `active`; the second response lists the first in `contradictions_detected` with `basis: "triple"`; a subsequent recall returns both and names the pair in `contradictions`.

**F7 — The procedural gate holds.**
*accept:* After `memory_propose_procedure`, a recall with `types: ["procedural"]` matching the proposal's text returns zero records. After `memory_review_proposals` with `action: "approve"`, the same recall returns exactly one.

**F8 — The daemon cannot approve its own proposals.**
*accept:* An approve from the daemon fails for two independent reasons: `daemon_may_approve: false` refuses it, and with that flag flipped to true it still fails with `APPROVAL_SIGNATURE_REQUIRED`, because the daemon holds no reviewer key. The proposal stays `pending` in both cases.

**F9 — Trajectories reconstruct.**
*accept:* After a 5-step cycle, ordering `episodic_attrs` by `(session_id, cycle_id, step_no)` returns the 5 steps in the order they occurred, with no gaps and no duplicates.

**F10 — Crashes leave evidence.**
*accept:* Open a cycle, write 3 observations, kill the process. After restart and one daemon pass, the cycle is `abandoned`, and its 3 observations are present as episodic records with `outcome: "abandoned"`.

**F11 — Runaway loops get flagged.**
*accept:* Opening 3 cycles in one scope within 60 minutes with the same goal and no successful outcome makes the 3rd response carry a non-null `loop_warning` with `repeats: 3` and a populated `advice`.

**F12 — Blast-radius guard.**
*accept:* `memory_forget` with a selector matching 50 records and `max_records: 10` returns `aborted: true`, `aborted_reason: "exceeds_max_records"`, `affected_count: 0`, and leaves all 50 unchanged.

**F13 — Forget modes behave as labelled.**
*accept:* `tombstone` makes a record unrecallable while its row and content remain and `recoverable_until` is set. `redact` empties `content` while `id`, `provenance`, and `created_at` survive. `hard_delete` without `confirm: true` returns `CONFIRM_REQUIRED` and changes nothing.

**F14 — Reflection is bounded and admits it.**
*accept:* With 10,000 episodes in the window and `max_episodes: 5000`, the response has `episodes_examined: 5000` and `capped: true`.

**F15 — Health problems are named.**
*accept:* With 41 proposals pending for over 30 days, `memory_stats` returns a `warnings` entry naming both the count and the age.

**F16 — Sensitive records are withheld visibly.**
*accept:* With 3 records marked `sensitivity: "pii"` matching a query and `include_sensitive: false`, none of the 3 appear in `context_block`, `excluded_sensitive_count: 3`, and all 3 are still present in `records`.

**F17 — Supersession is atomic.**
*accept:* `memory_remember` with `supersedes: <id>` leaves the old record `status: "superseded"` with `superseded_by` set to the new id, in one transaction. No sequence of calls produces a record that is superseded with a null successor.

**F18 — An unsigned approval is refused.**
*accept:* `memory_review_proposals` with `action: "approve"` and no `signatures` entry for a proposal id returns `APPROVAL_SIGNATURE_REQUIRED`; the proposal stays `pending`, no record is created, and the queue depth is unchanged.

**F19 — A signature commits to exact content.**
*accept:* Sign a proposal's `signing_payload`, edit the candidate so its hash changes, then submit the signature. The result is `APPROVAL_CANDIDATE_CHANGED`, `skipped[].reason: "candidate_changed"`, and the proposal stays `pending`.

**F20 — A signature cannot be replayed or repurposed.**
*accept:* A valid signature submitted twice fails the second time with `challenge_already_used`. The same signature submitted against a different `proposal_id`, a different `reviewed_by`, or with `decision` flipped from reject to approve fails with `APPROVAL_SIGNATURE_INVALID`. A challenge older than `challenge_ttl_seconds` fails with `challenge_expired`.

**F21 — Approvals re-verify offline, years later.**
*accept:* Given only a stored procedural record and the reviewer's public key — no server, no policy file, no database — the signature over `approval.signature.signed_payload` verifies, and the payload's `candidate_sha256` matches a fresh hash of the stored candidate.

**F22 — Post-approval tampering is detected, not trusted.**
*accept:* Edit an approved procedure's steps directly in the database. The next daemon re-verification pass surfaces it in `v_approval_audit` with a hash mismatch, and it stops being returned by recall as approved.

**F23 — Key lifecycle behaves as labelled.**
*accept:* A signature from an unknown `key_id` returns `APPROVAL_KEY_UNKNOWN`. A key marked `retired` still verifies its existing approvals but is refused for new decisions. A key marked `revoked` causes every procedure it approved to appear in `v_approval_audit` for re-review. A server started with `require_signature: true` and no reviewer keys refuses to start with `NO_REVIEWER_KEYS_CONFIGURED`.

### Non-functional

**NF1 — Recall latency.** p95 under 150ms for a hybrid recall with `k: 12` over 100,000 active records on commodity hardware, excluding embedding time for the query.
*accept:* Benchmark over a seeded 100k-record store, 100 queries, reports p95 < 150ms.

**NF2 — Portability.** The entire memory is one file with no external dependency.
*accept:* Copy `memory.db` to a different machine, start the server, and a recall returns identical results to the source machine.

**NF3 — Concurrency.** Many readers, one writer, no corruption.
*accept:* 8 concurrent readers and 2 concurrent writers for 60 seconds produce no `database is locked` failures beyond the 5s busy timeout, and `PRAGMA integrity_check` returns `ok`.

**NF4 — Offline.** No network access is required in the default configuration.
*accept:* With outbound networking disabled and the embedding model pre-cached, all nine tools function.

**NF5 — No grounding.** The process makes no outbound connections and touches no path outside its DB and policy file.
*accept:* Traced under normal operation, the process opens no sockets and no file handles outside `memory.db*` and the model cache.

**NF6 — Graceful vector loss.** Losing sqlite-vec degrades recall rather than breaking it.
*accept:* With `require_vector_extension: false` and the extension absent, all nine tools function; recall returns keyword results with `degraded` set.

**NF7 — Bounded growth.** Retention keeps the store bounded without human intervention.
*accept:* Simulating 12 months at 500 episodes/day with the default retention, the daemon holds the active record count under `limits.max_records_per_scope`.

**NF8 — Auditable.** No API call destroys data without an explicit, recorded, confirmed request.
*accept:* Every write and forget stores `provenance` or `reason`; only `hard_delete` with `confirm: true` removes rows; `tombstone_retention_days` is honoured before permanent removal.

**NF9 — Observable without leaking.** Operations can be debugged without logging memory content.
*accept:* At `log_level: info` with `log_content: false`, logs contain record ids, scores, and timings, and no `content` values.

**NF10 — Versioned.** A record written by v1.0 is readable by any 1.x.
*accept:* Every record and payload carries `schema_version`; the server refuses to open a DB whose `meta.schema_version` has a higher major version than the code.

---

## 11. Failure modes

| Failure | How it shows up | Mitigation |
|---|---|---|
| **Embedding drift** — model changed, old vectors meaningless | Recall quality falls with no error | `records.embedding_model` makes it detectable; daemon re-embeds; keyword path stays correct throughout |
| **Contradictory facts accumulate** | Recall returns both sides of a conflict; host behaviour goes inconsistent | Contradictions reported at write and at recall, and counted in `stats.health`. Deliberately not auto-resolved — see §8 |
| **Memory poisoning** — a compromised or confused host writes false facts | Agent confidently acts on fiction | Every record carries provenance; `forget` can select by `provenance.agent` and reason; procedural memory is gated *and signed*, so poisoning cannot reach the most dangerous layer without the reviewer's key. **Episodic and semantic writes remain unauthenticated in v1** — any host that can reach the server can write a fact. See A-3 and B-1 |
| **Reviewer key lost or compromised** | Approvals that look legitimate but were not made by the reviewer, or no way to approve anything | Mark the key `revoked` in policy: every procedure it approved surfaces in `v_approval_audit` for re-review rather than being silently trusted. Add a new key alongside. Keys are per-reviewer and rotatable, so this is a re-review exercise, not a rebuild |
| **Procedure edited after approval** | A signed, apparently-approved procedure whose steps are no longer what was signed | `candidate_sha256` is covered by the signature; the daemon re-verifies on a sweep and mismatches surface in `v_approval_audit`, where they count as unapproved rather than approved |
| **Unbounded growth** | DB grows, recall slows, ranking noise rises | TTLs, decay, daemon consolidation, and `limits.max_records_per_scope`. `stats` warns before the ceiling |
| **DB corruption / loss** | Total memory loss — one file is the whole product | WAL plus `synchronous: NORMAL`; `PRAGMA integrity_check` in the daemon; **backup is an operational requirement**, called out in §6 rather than assumed |
| **Runaway loop floods memory** | Thousands of near-identical episodes in minutes | `loop_warning` at cycle open; `max_writes_per_session_per_minute`; `idempotency_key` collapses genuine retries |
| **Review queue ignored** | Procedural memory silently stops growing; the agent never gets better at anything | `queue_depth` on every propose response; `stats` warns on count and age of oldest pending |
| **Learned procedure is wrong** | Agent repeats a bad routine confidently, every run | `usage.invocations` vs `successes`; `v_failing_procedures`; ranking demotion below `procedure_min_success_rate` before a human retires it |
| **Vector extension missing** | Semantic recall silently becomes keyword-only | `require_vector_extension` decides fail-loud vs degrade-visibly; either way `degraded` is on the response |
| **Clock skew across hosts** | Recency ranking and TTLs misbehave | All timestamps UTC, server-assigned. Client-supplied times are only accepted for `valid_from`/`valid_to`, which are claims about the world rather than about the store |

---

## 12. Assumptions

Falsifiable, each with the consequence if it turns out wrong.

**A-1 — The CoALA mapping in §3 is faithful to the paper. RESOLVED — verified against the paper.**
Checked against the published TMLR version (02/2024, OpenReview `1i6ZCvflQJ`). Memory modules, the internal/external action split, the three internal action classes, the learning modalities, and the Generative Agents retrieval attribution all match §4.1–§4.6 as written. Three things were corrected in the process: the decision cycle now uses the paper's planning-stage / execution-stage structure rather than a flat four-step sequence; `memory_reflect` is labelled as reasoning composed with learning rather than reasoning alone; and §4 and §13 now cite the paper's own §6 arguments for the minimal-action-space and no-grounding decisions instead of asserting them.
*No longer an open risk.*

**A-2 — One node with one writer is enough.**
*If wrong:* the storage layer moves to Postgres + pgvector. The tool contract, record schemas, and every acceptance criterion above are unaffected, which is why storage sits behind the contract rather than in it. Expect a week of work, not a redesign.

**A-3 — Hosts are trusted. PARTIALLY CLOSED in v1: reviewer identity is now proven; transport and scope authorization are not.**

Originally: no authentication anywhere, and `reviewed_by` a self-asserted string.

*Closed in v1 (§8, F18–F23):* **reviewer identity.** Approving or rejecting requires an Ed25519 signature from a key the server never holds the private half of. An agent cannot approve its own proposals, approvals are bound to exact content, and every decision is durably provable and tamper-evident. This was closed early because it is the only part of A-3 that bites in solo use — the realistic threat on one machine is not a remote attacker, it is an agent deciding that approving its own procedure would be convenient.

*Still open, carried to [`BACKLOG.md`](../BACKLOG.md) B-1:* everything else. There is no caller authentication, so any orchestration reaching the server can read and write any scope it can name, and `memory_remember` writes are unattributed. Scope remains a logical boundary, not a security one.

*Accepted for solo use* on one machine over stdio, where anything that could reach the server could already open `memory.db` directly. Until B-1 is closed: no network-bound transport, no second person's data, no shared `memory.db`.

*Known limit of the signing scheme:* it does not stop an attacker who already has code execution as you and can read an unencrypted key file. `require_passphrase: true`, or a hardware-backed key through `ssh-agent`, closes that at the cost of a prompt per approval.

**A-4 — Local 384-dimension embeddings give good enough recall.**
*If wrong:* swap the embedder for a stronger hosted model. Costs a re-embed pass, which the daemon already supports, plus a data-egress decision since memory content would leave the machine.

**A-5 — Hosts will actually pass `idempotency_key` and check `loop_warning`.**
The loop-safety properties are cooperative: the server cannot force a host to use them.
*If wrong:* near-duplicates accumulate and runaway loops are caught only by the write-rate limiter, which is a blunt backstop. Mitigation is documentation and the rate limit; a stricter design would reject unkeyed writes inside an open cycle, which is worth revisiting in v2.

**A-6 — Reflection produces facts worth keeping.**
Consolidation quality is unproven until there is real traffic.
*If wrong:* `auto_commit` defaults to false, so bad consolidations queue as proposals rather than entering memory. The default is conservative precisely because this assumption is the least tested one here.

---

## 13. Decision log

| Decision | The tension | Why this way |
|---|---|---|
| MCP as the only interface | An HTTP API would reach non-MCP orchestrators (LangGraph, n8n, CrewAI); MCP reaches the Claude ecosystem natively with zero glue | One interface done properly beats two done partially. MCP gives tool discovery, typed schemas, and stdio isolation for free. An HTTP adapter can wrap the same core later without touching it |
| SQLite over Postgres/pgvector | Postgres gives multi-writer, multi-device, and row-level security; SQLite gives one portable file and no infrastructure | Portability won. The whole memory being one copyable file is a genuine feature, and the storage layer is isolated so the swap stays cheap. Revisit above ~1M records |
| Base table + per-type extension tables | One wide nullable table is simpler to query; extension tables need a join | Constraints. A wide table cannot enforce "an episode has a step number" without abandoning NOT NULL. Joins at this scale cost nothing |
| Proposals in a separate table, not a status flag | A flag is less code | Safety must not depend on every query remembering a `WHERE`. A separate table makes an unapproved procedure unreachable even by a buggy query |
| Procedural writes always gated, not configurable | A configurable gate is more flexible; an unattended agent that must wait for approval learns slower | CoALA §4.1 calls procedural writes "significantly riskier ... [they] can easily introduce bugs or allow an agent to subvert its designers' intentions", and §6 names "procedural deletion and modification" as the learning actions that "could cause internal harm". A flexible gate becomes an off gate the first time someone is in a hurry |
| `memory_forget` is a first-class tool, not an admin script | Most memory systems only add | CoALA §4.5 notes "modifying and deleting (a case of 'unlearning') are understudied", and §6 lists "deleting unneeded memory items" as a needed form of learning. A store that can only grow is a store that gets worse |
| Signed approvals in v1, ahead of any other authentication | Cryptography before basic authn looks backwards, and it is the only part of A-3 built early | The threat in solo use is not a remote attacker — it is an agent approving its own proposals, which needs no network at all. A name in a field stopped nothing. Signing closes the realistic gap now, and does it in a way transport auth never could: the proof outlives the session and is verifiable offline |
| Approve stays on the MCP surface rather than moving to a CLI | Removing it entirely would make self-approval structurally impossible, matching the minimal-action-space principle | The signature, not the caller, is what is trusted — so an agent relaying an approval is harmless and an agent forging one is impossible. Keeping it callable means approving from a chat session instead of a terminal, which is the difference between a queue that gets drained and one that does not |
| Signed payload is fixed-field text, not JSON | JSON is the obvious choice and everything else here is JSON | Canonical JSON is a footgun — key order, unicode escaping, number formatting. A verifier that re-serialises can disagree with the signer over bytes that look identical. Fixed-field text has one representation |
| The signed payload is stored verbatim | Storing just the signature and rebuilding the payload is less data | Rebuilding proves only that the row is self-consistent with itself. Storing the signed bytes is what makes an approval independently verifiable years later, and what makes post-approval edits detectable |
| Contradictions reported, never auto-resolved | Auto-resolution gives the host one clean answer; reporting pushes work onto the host | The agent has no basis for choosing. It does not know which source is more reliable. A store that silently picks wrong is worse than one that admits conflict |
| Only `content` is indexed | Indexing several columns would improve recall on structured fields | Relevance would then depend on which column matched — hidden behaviour that makes ranking untunable. One indexed field with a stated writer-side invariant is honest |
| RRF instead of normalised score fusion | Normalising cosine and BM25 into one scale is the common approach | The scales are not comparable and normalisation constants drift with corpus size. RRF needs one constant and is stable |
| `context_block` returned alongside records | Returning only records is cleaner API design | Most hosts want text. Making them re-implement token-bounded formatting guarantees each does it differently and some do it wrong. The budget guarantee is the point |
| Empty string, not a sentence, when nothing is found | A message is friendlier | A model handed "No relevant memories found" treats it as a retrieved fact and reasons from it |
| Working memory in a table, not in process | In-process is faster and simpler | Restart resilience. A crash mid-loop must not lose the trajectory — that trace is the most valuable data a failure produces |
| Local embeddings by default | Hosted models embed better | Offline operation and no memory content leaving the machine. Memory contains whatever hosts put in it, which may be personal |
| `max_records` mandatory on forget | An extra required parameter is friction | It is friction on the one irreversible operation. A selector matching more than expected is a bug, and this turns it into an abort rather than a data-loss event |
| No grounding actions, ever | A memory agent that could fetch a URL to verify a fact would be more capable | It is the property that makes this safe to attach to any orchestration. Capability here buys little and costs the one guarantee worth having |

---

## 14. Out of scope, and what would force a re-architecture

### Out of scope for v1

- Authentication, authorization, and multi-tenancy (A-3 — required before any shared deployment)
- HTTP/REST interface, importable-library surface, file-folder memory contract
- LLM parameter updates / fine-tuning (CoALA learning modality, deliberately excluded — §4)
- Self-modifying procedural memory or agent-code editing (§3)
- Cross-device sync or replication
- A UI for the review queue — v1 reviews through `memory_review_proposals`
- Graph-structured semantic memory (entity resolution, relationship traversal)
- Multi-modal memory (images, audio)
- Automatic PII detection — `sensitivity` is set by the writer, not inferred

### What would force a re-architecture, not just a feature

| Trigger | Why the current design breaks | Where it goes |
|---|---|---|
| More than one concurrent writer process | SQLite serialises writes; contention becomes the bottleneck well before the record ceiling | Postgres + pgvector. Tool contract survives (A-2) |
| Beyond ~1M total records | Ranking over the candidate pool stops being cheap; the file stops being casually copyable | Postgres + pgvector, or a dedicated ANN index alongside |
| Memory shared between people | No authentication, and scope is a logical boundary only | Real authz, per-scope tokens, row-level security. This is a security redesign |
| Sub-50ms recall required | The Python + SQLite + local-embedding path has a floor | In-process embedding cache, or a different storage engine |
| Facts needing relationship traversal ("who else works with X") | Triples in a relational table do not traverse | Graph store for semantic memory; episodic and procedural can stay |

### Build order — complete

The contracts are the spec. This was built against them in the sequence below, each stage independently tested. All stages are done; the list stands as the map of what lives where.

1. Storage layer — apply `schema.sql`, implement CRUD against the record schemas. Verify every DDL invariant.
2. `memory_remember` + `memory_recall`, keyword-only. This alone is a useful service and validates F1, F3, F4, F5.
3. Embeddings and hybrid retrieval. F2, NF6.
4. Cycles: `open_cycle` / `close_cycle`, loop detection. F9, F10, F11.
5. `memory_forget` with all three modes. F12, F13, NF8.
6. The gate: `propose_procedure`, `review_proposals`. F7, F8.
7. `memory_reflect`. F6, F14.
8. The daemon — reuses stage 7. Pattern B.
9. `memory_stats`. F15.

Stages 1-3 are the minimum that earns its place in an orchestration. Everything after makes it improve rather than merely persist.

---

## References

- Sumers, T. R., Yao, S., Narasimhan, K., & Griffiths, T. L. (2024). *Cognitive Architectures for Language Agents.* Transactions on Machine Learning Research, 02/2024. arXiv:2309.02427. OpenReview: `1i6ZCvflQJ`
  Sections referenced above: §4.1 memory modules · §4.2 grounding · §4.3 retrieval · §4.4 reasoning · §4.5 learning modalities and the unlearning gap · §4.6 decision cycle · §6 action-space size and safety, and the per-module write-access example.
- Park, J. S., O'Brien, J., Cai, C. J., Morris, M. R., Liang, P., & Bernstein, M. S. (2023). *Generative Agents: Interactive Simulacra of Human Behavior.* UIST '23 — source of the relevance × recency × importance retrieval function, and cited as such in CoALA §4.3.
