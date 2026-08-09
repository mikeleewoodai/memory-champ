# Handover — memory-agent

Everything needed to pick this up in a local session. Written to be read cold.

---

## Where things are

| | |
|---|---|
| Repo | `mikeleewoodai/memory-champ` |
| Branch | `main` |
| Project | repo root |
| PR | none opened |

```bash
git clone https://github.com/mikeleewoodai/memory-champ.git
cd memory-champ
```

Originally drafted in `mikeleewoodai/claude-mobile` under `memory-agent/`, which is a scratch repo for work started from a phone. The full history came across, so early commits show that layout. Nothing lives there any more.

## What it is

A memory service for agent orchestrations, structured on CoALA (Sumers, Yao, Narasimhan & Griffiths, Princeton; TMLR 2024). It stores three kinds of memory rather than one — **what happened** (episodic), **what is true** (semantic), **how to do something** (procedural) — exposes them over MCP as nine tools, and runs a daemon that consolidates its own store between sessions.

It has **no grounding actions**: no network, no filesystem outside its database, no user dialogue. The worst it can do is return a bad memory. That is what makes it safe to attach to an arbitrary loop.

Spec + machine-readable contracts + working implementation. `verify.py` and the full test suite are green.

## Prove it works before you change anything

```bash
pip install -e ".[all,dev]"  # or ".[vector,server,tokenizer,dev]" to skip torch
python verify.py             # contract checks: schemas, DDL invariants, the published signature
pytest -q                    # one test per acceptance criterion, plus conformance and NF suites
pytest -q -m slow            # + recall latency benchmark
```

If `verify.py` passes, the contracts are intact. If `pytest` passes, every acceptance criterion in spec §10 holds. Both should be green on a fresh clone.

Only `cryptography` and `PyYAML` are hard requirements. `sqlite-vec`, `sentence-transformers`, `tiktoken`, and `mcp` are optional and each degrades visibly rather than failing. `dev` is not optional for the commands above, though — it is what supplies `pytest` and `jsonschema`, and `[all]` does not include it.

## Run it

```bash
memory-agent keygen ~/.memory-agent/approval --id mike   # do this first
# paste the printed block into policy.yaml under learning.approval.reviewers
```

```jsonc
{
  "mcpServers": {
    "memory": {
      "command": "python",
      "args": ["-m", "memory_agent.server"],
      "env": { "MEMORY_AGENT_POLICY": "/abs/path/to/policy.yaml" }
    }
  }
}
```

```bash
memory-agent-daemon --once                # independent mode
memory-agent review list --scope acme.crm # what the agent wants to learn
memory-agent review approve <id> --reviewer mike --key ~/.memory-agent/approval
memory-agent verify                       # re-check every stored approval
memory-agent stats --scope acme.crm
```

## Read in this order

1. `README.md` — orientation, install, module map
2. `docs/memory-agent-coala-spec.md` §1–3 — what it is, quick start, the CoALA mapping
3. §8 — the action contract and **signed approvals**
4. §13 — the decision log. Every non-obvious choice with the tension behind it
5. `BACKLOG.md` — B-1, the one thing blocking a work version

## The six things most likely to trip you up

1. **`content` is the only indexed field.** Nothing else is embedded or keyword-searchable. Writer-side invariant: whatever a future reader needs must appear in `content`, even if it also lives in a structured field. A procedure whose trigger is only in `procedural_attrs.trigger_text` will not be found.

2. **The signed payload is fixed-field text, not JSON, and is stored verbatim.** Canonical JSON is a footgun — key order, unicode escaping, number formatting. Verification uses the stored bytes, never a payload rebuilt from current field values; rebuilding would prove only that a row is self-consistent with itself. `contracts/examples/records.json` carries a real signature and its public key — it is the golden test.

   If it fails, suspect **how you read the file** before you suspect the canonical form. `records.json` is UTF-8 and carries non-ASCII, so reading it with the platform default — cp1252 on Windows — mangles a character in `steps` and changes the hash. That looks exactly like drift, and acting on it means editing `candidate_hash()` until the hashes agree, which would break every genuine approval. Every text read in the codebase passes `encoding="utf-8"` for this reason; don't drop it. Only with the bytes known good does a mismatch mean your canonical form has drifted — and then it will reject genuine approvals.

3. **`candidate_sha256` covers a specific field subset** — `content`, `trigger`, `preconditions`, `steps`, `success_signal`, `failure_signal` — serialised with sorted keys, no whitespace, `ensure_ascii=False`. Envelope fields the server assigns are excluded, because the reviewer signs before the record exists. `approval.candidate_hash()` is the one implementation, and `store.procedure_candidate()` must reconstruct every covered field or verification silently stops checking the ones it misses — which is exactly how `content` went uncovered.

   `content` is load-bearing here. It is the only indexed field and the text recall returns, so it is what an agent actually follows. Do not narrow the subset. Widening it is a breaking change: it invalidates every existing signature, so it needs a `PAYLOAD_VERSION` bump and a re-run of `regenerate_fixture.py`. Still uncovered, and tracked in `BACKLOG.md` as B-2: `supersedes` and `evidence_record_ids`.

4. **Constraints in the DDL are load-bearing; never work around one.** Signature columns are `NOT NULL` so an unsigned approved procedure is unrepresentable. Supersession has a CHECK so the half-applied case cannot exist. Unapproved candidates live in `proposals`, a separate table, so a buggy query cannot leak one into recall. A constraint firing means the caller is wrong.

5. **The MCP SDK is moving.** `server.py` supports both 1.x (decorators) and 2.x (`add_request_handler`, snake_case `input_schema`/`open_world_hint`). Tool schemas come from `contracts/mcp-tools.json`, not from Python signatures — keep it that way, or the contract stops being the source of truth.

6. **Writes open with `BEGIN IMMEDIATE`, and that is load-bearing.** A deferred `BEGIN` takes no write lock, so the first write has to upgrade read→write — and SQLite answers a contended upgrade with `SQLITE_BUSY` *without* invoking the busy handler, because blocking there can deadlock. `busy_timeout` is then never consulted and a second writer fails instantly with `database is locked`, rather than waiting its turn. The daemon plus an attached host is two writers on one `memory.db`, so this is the ordinary case, not an edge one. Don't change it back to a bare `BEGIN`.

## Decisions already made — don't re-litigate without reason

Full reasoning in spec §13. The headlines:

- **MCP only.** No HTTP API, no importable-library surface. One interface done properly.
- **SQLite over Postgres.** Portability won; the whole memory is one copyable file. Revisit above ~1M records (spec §14).
- **Procedural writes always gated**, not configurable. A flexible gate becomes an off gate the first time someone is in a hurry.
- **Approve stays callable over MCP.** The signature, not the caller, is what is trusted — so relaying is harmless and forging is impossible, and you can approve from a chat session instead of a terminal.
- **Contradictions reported, never resolved.** The agent has no basis for picking a winner.
- **`max_records` mandatory on forget.** Friction on the one irreversible operation.

## Open work

**B-1 — caller authentication. Blocks a work version.** Reviewer identity is done; *who is calling* is not. Any orchestration reaching the server can read and write any scope it can name, and `memory_remember` writes are unattributed. Fine for one person on one machine over stdio, where anything that could reach the server could already open `memory.db` directly. Not fine the moment there is a second person, a second machine, a network transport, or client data. Full work item with definition of done in `BACKLOG.md`.

One note carried there: **don't fold reviewer identity back into caller identity** once principals exist. A session credential proves who is connected *now*; a signature proves who decided *then*, and survives the session, the server, and the database.

Also open, all with stated consequences in spec §12: A-2 (single-writer), A-4 (local embeddings good enough), A-5 (hosts cooperate with idempotency keys), A-6 (reflection quality unproven — which is why `auto_commit` defaults to false).

## Honest gaps

- **Reflection's clustering is crude** — by cycle, and by repeated successful action name. It queues proposals rather than committing them, which is what makes that tolerable. First thing to improve if consolidation quality matters.
- **`HashingEmbedder` is lexical only.** It exists so the service runs and is testable offline. Install `sentence-transformers` for real semantic recall; the daemon re-embeds automatically when the model name changes.
- **No real tokenizer in this environment**, so `TokenCounter` fell back to a conservative over-estimate. That keeps the F1 budget guarantee (an upper bound) but returns slightly less memory than it could. Install `tiktoken` locally for exact counts.
- **NF1's latency benchmark ran at 2k records, not the 100k the spec names.** It passes with headroom; re-run at full scale locally before trusting the number.
- **NF5 is checked structurally** (no network imports, `openWorldHint` false) rather than by tracing syscalls. The strong claim in the spec deserves a real trace at some point.

## Environment notes from the session that built this

Container-specific; they may not apply locally, but they cost time here.

- `arxiv.org`, `ar5iv`, `openreview.net`, and tiktoken's encoding CDN are all blocked by the egress proxy.
- The system `cryptography` package was broken (`ModuleNotFoundError: _cffi_backend`); `pip install --force-reinstall cffi` fixed it.
- `pip install` aborted on the Debian-managed `PyJWT`; `--ignore-installed PyJWT` worked around it.
- No `sqlite3` CLI binary — use `python -c "import sqlite3; ..."`.

## If you want to keep going

Reasonable next moves, roughly in order of value:

1. Swap in `sentence-transformers` and see whether recall quality justifies A-4.
2. Improve reflection clustering (embedding-based rather than by-cycle), still queueing rather than committing.
3. Run the agent against a real orchestration for a week, then read `memory-agent stats` and the review queue. The queue depth tells you whether the gate is workable in practice.
4. B-1, when a work version comes up.
