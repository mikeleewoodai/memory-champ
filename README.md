# memory-agent

A memory service for agent orchestrations, structured on **CoALA** — the cognitive architecture from *Cognitive Architectures for Language Agents* (Sumers, Yao, Narasimhan & Griffiths, Princeton; TMLR 2024; arXiv:2309.02427).

**Status: implemented.** Nine MCP tools, SQLite + sqlite-vec storage, an independent daemon, and a signing CLI. `verify.py` and the test suite are both green, with one test per acceptance criterion in the spec.

It stores three kinds of memory rather than one — **what happened** (episodic), **what is true** (semantic), **how to do something** (procedural) — exposes them over MCP so any orchestration can attach, and also runs on its own schedule to consolidate and prune its own store with no host present.

It has no grounding actions. It never reaches the network, never touches the filesystem outside its database, never talks to a user. That is what makes it safe to attach to an arbitrary agentic loop.

## Start here — the brief

**[mikeleewoodai.github.io/memory-champ](https://mikeleewoodai.github.io/memory-champ/)** explains this project to someone who has never seen it: the problem it solves, how it works in plain English, and the technical detail folded into collapsible sections underneath. It names the limits too, including the one that blocks a work version.

The source is [`docs/memory-champ-brief.html`](docs/memory-champ-brief.html), a single self-contained file — all CSS and graphics inline, nothing fetched — so it reads the same offline. GitHub serves `.html` as source rather than rendering it, which is what the published copy is for.

The tool contract is published too: every `$id` in `contracts/` resolves under **[/v1/](https://mikeleewoodai.github.io/memory-champ/v1/)**.

## Install

Two commands. The second one prints the host config you paste in.

```bash
uv tool install "memory-agent[recommended] @ git+https://github.com/mikeleewoodai/memory-champ"
memory-agent init
```

Plain pip works the same way, in a venv of your own:

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install "memory-agent[recommended] @ git+https://github.com/mikeleewoodai/memory-champ"
memory-agent init
```

`recommended` is vector recall, exact token counting, and the MCP server — everything except semantic embeddings, which pull torch and about 2 GB. Add them when lexical overlap stops being good enough, a call worth making against your own corpus rather than up front:

```bash
pip install "memory-agent[all]"     # adds sentence-transformers
```

Only `cryptography`, `PyYAML`, and `bcrypt` are truly required. The rest degrades visibly rather than failing: no `sqlite-vec` means keyword-only recall that says so, no `sentence-transformers` means the built-in hashing embedder, no `tiktoken` means a conservative token bound.

Working on the code instead of using it? `pip install -e ".[all,dev]"` — `dev` is what brings in `pytest` and `jsonschema`, and `[all]` deliberately does not include it, so a bare `.[all]` leaves the `pytest -q` below with nothing to run.

## What `init` does

```
$ memory-agent init --id ada
reviewer key: ~/.memory-agent/approval  (created, mode 600, passphrase-protected)
policy:       ~/.memory-agent/policy.yaml  (written, reviewer already filled in)
database:     ~/.memory-agent/memory.db  (created on first write)

Add this to your MCP host config — no env var needed, policy.yaml
is found at the conventional path:

{
  "mcpServers": {
    "memory-champ": {
      "command": "/abs/path/to/.venv/bin/python",
      "args": ["-m", "memory_agent.server"]
    }
  }
}
```

Three things that used to be manual: it generates the reviewer key, writes `policy.yaml` with the public key and fingerprint **already substituted in**, and prints the config with `sys.executable` resolved — the interpreter that just ran `init`, which is by construction the one that has the package. Hand-writing that path is the most common way the server ends up unstartable.

The server name is the label a host shows you and prefixes its tools with, so it is worth making specific. `memory` collides with the general idea of memory in a host that already talks about remembering things; `memory-champ` names *this* service. Change it with `--server-name`.

**`init` never regenerates an existing key.** Re-running it is safe and idempotent; `--force` rewrites `policy.yaml` only. Replacing a reviewer key invalidates every signature made with the old one — stored approvals stop verifying and `memory-agent verify` reports them as tampered, which is indistinguishable from an actual attack. Use `keygen --force` if you genuinely mean to.

**It asks for a passphrase by default.** The gate's security claim is that an agent holds the tool but not the key, and that only holds while the key is unreadable to the agent. The usual deployment is an agent with shell access on the same machine as the key, so an unencrypted private key at a predictable path lets it sign its own approvals — the exact thing the gate exists to prevent. `--no-passphrase` exists, and says `UNENCRYPTED` in the output when you use it. An existing SSH key works too; only the public half goes in the config.

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
memory-agent review approve p_80dcd7f5 --reviewer me --key ~/.memory-agent/approval
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
- Signing does not stop an attacker who already has code execution and can read an unencrypted key file. `init` asks for a passphrase by default and sets `require_passphrase` to match; for more, move to a hardware key via `ssh-agent`.
- The default `HashingEmbedder` captures lexical overlap only. Install `sentence-transformers` for real semantic recall.
- Reflection's clustering is deliberately crude (by cycle, by repeated action). It queues proposals rather than committing, which is why that is tolerable — see assumption A-6.

## License

MIT — see [LICENSE](LICENSE).
