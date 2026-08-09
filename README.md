# memory-agent

A memory service for agent orchestrations, structured on **CoALA** — the cognitive architecture from *Cognitive Architectures for Language Agents* (Sumers, Yao, Narasimhan & Griffiths, Princeton; TMLR 2024; arXiv:2309.02427).

**Status: implemented.** Nine MCP tools, SQLite + sqlite-vec storage, an independent daemon, and a signing CLI. `verify.py` and the test suite are both green, with one test per acceptance criterion in the spec.

It stores three kinds of memory rather than one — **what happened** (episodic), **what is true** (semantic), **how to do something** (procedural) — exposes them over MCP so any orchestration can attach, and also runs on its own schedule to consolidate and prune its own store with no host present.

It has no grounding actions. It never reaches the network, never touches the filesystem outside its database, never talks to a user. That is what makes it safe to attach to an arbitrary agentic loop.

## Install

```bash
pip install -e ".[all,dev]"      # or: pip install -e ".[vector,server,tokenizer,dev]" to skip torch
```

`dev` is what brings in `pytest` and `jsonschema`. `[all]` on its own does not, so include it or the `pytest -q` below has nothing to run.

Only `cryptography` and `PyYAML` are required. Everything else degrades visibly rather than failing: no `sqlite-vec` means keyword-only recall that says so, no `sentence-transformers` means the built-in hashing embedder, no `tiktoken` means a conservative token bound.

## Set up a reviewer key

Approving a learned procedure requires **your** signature. Do this first — the server refuses to start without a reviewer key.

```bash
memory-agent keygen ~/.memory-agent/approval --id mike
```

It prints the exact block to paste into `policy.yaml`. An existing SSH key works too; only the public half goes in the config. The private half never reaches the server.

## Run it

```jsonc
// claude_desktop_config.json / .mcp.json
{
  "mcpServers": {
    "memory": {
      "command": "python",
      "args": ["-m", "memory_agent.server"],
      "env": { "MEMORY_AGENT_POLICY": "/path/to/policy.yaml" }
    }
  }
}
```

Independent mode, on a schedule:

```bash
memory-agent-daemon --once          # or: python -m memory_agent.daemon --once
```

## The shape of it

```
recall  →  act  →  remember  →  reflect
```

```json
{ "tool": "memory_recall",
  "arguments": { "scope": "acme.crm", "query": "how does this client want invoices", "max_tokens": 1200 } }
```

`context_block` comes back token-measured and ready to paste into a prompt. See §2 of the spec for the full quick start, and §9 for integration patterns.

## Reviewing what the agent wants to learn

```bash
memory-agent review list --scope acme.crm
memory-agent review approve p_80dcd7f5 --reviewer mike --key ~/.memory-agent/approval
memory-agent verify                       # re-check every stored approval
memory-agent stats --scope acme.crm
```

Approving is also callable over MCP — the signature, not the caller, is what the server trusts, so an agent can relay an approval you produced and can never manufacture one.

## Contents

| Path | What it is |
|---|---|
| [`docs/memory-agent-coala-spec.md`](docs/memory-agent-coala-spec.md) | **The spec.** Architecture, CoALA mapping, data layer, action contract, integration patterns, 23 functional + 10 non-functional requirements with acceptance criteria, failure modes, decision log |
| [`contracts/`](contracts/) | JSON Schema per record type, the nine-tool MCP contract, SQLite DDL, policy template, validated fixtures |
| `src/memory_agent/` | The implementation — see the module map below |
| [`tests/`](tests/) | One test per acceptance criterion, plus contract-conformance and non-functional suites |
| [`verify.py`](verify.py) | Contract verification: schemas, DDL invariants, the published signature |
| [`BACKLOG.md`](BACKLOG.md) | Open work. B-1 blocks a work version |

| Module | Responsibility |
|---|---|
| `approval.py` | Ed25519 signing, canonical payload, verification. The gate rests on this |
| `store.py` | SQLite over `contracts/db/schema.sql`. Never works around a constraint it trips |
| `retrieval.py` | RRF over FTS5 + sqlite-vec, recency/importance, token-bounded context blocks |
| `service.py` | The nine tools, as plain Python over dicts |
| `server.py` | MCP wiring. Tool schemas come from the contract, not from code |
| `daemon.py` | Independent mode — the CoALA cycle run over the store itself |
| `cli.py` | Key management and the review queue. The only place a private key is read |
| `embedding.py` | Pluggable embedder + token counter, both with offline fallbacks |

## Verify

```bash
python verify.py          # contract checks: schemas, DDL invariants, the published signature
pytest -q                 # one test per acceptance criterion, plus conformance and NF suites
pytest -q -m slow         # + the recall latency benchmark
python eval_recall.py     # recall *quality* - does it find the right memory? (A-4)
```

`verify.py` and `pytest` answer "is it correct". `eval_recall.py` answers the separate question of whether recall is any good, which nothing else here measures: it scores keyword-only, the hashing fallback, and sentence-transformers over one corpus, and splits queries into a lexical control set and a paraphrase set. Only the paraphrase set discriminates — an embedding that helps when the words already match has proved nothing. Read its docstring before quoting a number from it; the corpus and gold labels are hand-written, and a single query moves R@1 by ten points.

`verify.py` checks the published approval signature in `contracts/examples/records.json` — it is real and reproducible, and it is the golden test for signing code.

If it fails, suspect **how the file was read** before you touch the canonical form. `records.json` is UTF-8 and contains non-ASCII, so reading it with the platform default encoding — cp1252 on Windows — mangles a character inside `steps` and changes the hash. That is indistinguishable from canonical-form drift and invites the one repair you must not make: editing `candidate_hash()` until the hashes agree would break every genuine approval. Every text read in this codebase passes `encoding="utf-8"` for exactly this reason; don't drop it.

Only once the bytes are known good does a mismatch mean your canonical payload or candidate hashing disagrees with the contract — and then it will reject genuine approvals.

## Design commitments

- **No grounding actions.** The worst it can do is return a bad memory.
- **Procedural writes are human-gated, and the gate is real.** Unapproved candidates live in a separate table, unreachable by recall even if a query is wrong — and approving requires an Ed25519 signature. An agent has the tool but not the key.
- **Approvals are verifiable offline, years later.** The signed payload is stored verbatim, so a procedure edited after approval fails the check instead of quietly staying "approved".
- **Contradictions are reported, never resolved.** The agent has no basis for picking a winner.
- **Nothing is destroyed by default.** Only an explicit `hard_delete` with `confirm: true` removes bytes.
- **Recall cannot blow your context.** `context_block` is measured, not estimated.
- **The whole memory is one file.** Copy `memory.db`, copy the memory. Which also means: back it up.

## Known limits

- **A-3 / [B-1](BACKLOG.md): no caller authentication.** Reviewer identity is proven; *who is calling* is not. Any orchestration reaching the server can read and write any scope it can name, and `memory_remember` writes are unattributed. Fine for one person on one machine over stdio. **Blocks a work version**, a second user, a network transport, or client data.
- Signing does not stop an attacker who already has code execution and can read an unencrypted key file. Use `--passphrase` at keygen, set `require_passphrase: true`, or move to a hardware key via `ssh-agent`.
- The default `HashingEmbedder` captures lexical overlap only. Install `sentence-transformers` for real semantic recall.
- Reflection's clustering is deliberately crude (by cycle, by repeated action). It queues proposals rather than committing, which is why that is tolerable — see assumption A-6.
