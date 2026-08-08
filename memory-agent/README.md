# memory-agent

A memory service for agent orchestrations, structured on **CoALA** — the cognitive architecture from *Cognitive Architectures for Language Agents* (Sumers, Yao, Narasimhan & Griffiths, Princeton; TMLR 2024; arXiv:2309.02427).

**Status: specification only.** No implementation yet. Everything here is the contract to build against.

It stores three kinds of memory rather than one — **what happened** (episodic), **what is true** (semantic), **how to do something** (procedural) — exposes them over MCP as nine tools, and also runs on its own schedule to consolidate and prune its own store with no host present.

It has no grounding actions. It never reaches the network, never touches the filesystem outside its database, never talks to a user. That is what makes it safe to attach to an arbitrary agentic loop.

## Contents

| Path | What it is |
|---|---|
| [`docs/memory-agent-coala-spec.md`](docs/memory-agent-coala-spec.md) | **The spec.** Architecture, CoALA mapping, data layer, action contract, integration patterns, numbered acceptance criteria, failure modes, decision log |
| [`contracts/mcp-tools.json`](contracts/mcp-tools.json) | All nine MCP tools with input/output JSON Schema and error codes |
| [`contracts/schemas/`](contracts/schemas/) | JSON Schema (draft 2020-12) for every record type |
| [`contracts/db/schema.sql`](contracts/db/schema.sql) | SQLite DDL — tables, FTS5, sqlite-vec, constraints, views |
| [`contracts/policy.example.yaml`](contracts/policy.example.yaml) | Retrieval weights, decay, TTLs, gates, daemon schedule |

## Two run modes

**Attached** — the host orchestration owns the decision cycle; the memory agent serves it over MCP.

**Independent** — a scheduled daemon runs the CoALA cycle over the store itself: consolidate episodes into facts, surface contradictions, propose procedures, expire and re-embed. Memory improves between sessions, not only during them.

## The shape of it

```
recall  →  act  →  remember  →  reflect
```

```json
{ "tool": "memory_recall",
  "arguments": { "scope": "acme.crm", "query": "how does this client want invoices", "max_tokens": 1200 } }
```

`context_block` comes back token-measured and ready to paste into a prompt. See §2 of the spec for the full quick start.

## Design commitments

- **No grounding actions.** The worst it can do is return a bad memory.
- **Procedural writes are human-gated, and the gate is real.** CoALA names procedural updates the riskiest learning modality. Unapproved candidates live in a separate table, unreachable by recall even if a query is wrong — and approving requires an **Ed25519 signature** from a key the server never holds the private half of. An agent has the tool but not the key, so it cannot approve its own proposals. Every approval is verifiable offline, years later, from the record alone.
- **Contradictions are reported, never resolved.** The agent has no basis for picking a winner, and a store that silently picks wrong is worse than one that admits conflict.
- **Nothing is destroyed by default.** Only an explicit `hard_delete` with `confirm: true` removes bytes.
- **Recall cannot blow your context.** `context_block` is measured, not estimated, and never exceeds `max_tokens`.
- **The whole memory is one file.** Copy `memory.db`, copy the memory. Which also means: back it up.

## Verifying the contracts

```bash
pip install jsonschema pyyaml

# schemas are valid draft 2020-12 and all $refs resolve
python -c "
import json,glob
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
items=[]
for f in glob.glob('contracts/schemas/*.json'):
    s=json.load(open(f)); Draft202012Validator.check_schema(s)
    items.append((s['\$id'],Resource.from_contents(s)))
reg=Registry().with_resources(items)
for _,r in items: list(Draft202012Validator(r.contents,registry=reg).iter_errors({}))
print('schemas ok')"

# DDL applies cleanly
python -c "
import sqlite3
sqlite3.connect(':memory:').executescript(open('contracts/db/schema.sql').read())
print('ddl ok')"

# the example approval signature is real - this is the golden test for signing code
pip install cryptography
python -c "
import json,base64,hashlib
from cryptography.hazmat.primitives.serialization import load_ssh_public_key as L
e=json.load(open('contracts/examples/records.json'))
s=e['procedural']['record']['approval']['signature']
L(e['approval_signature_fixture']['reviewer_public_key_openssh'].encode()).verify(
    base64.b64decode(s['sig']), s['signed_payload'].encode())
c=e['procedural']['record']
cand={k:c[k] for k in ('trigger','preconditions','steps','success_signal','failure_signal')}
assert hashlib.sha256(json.dumps(cand,sort_keys=True,separators=(',',':'),
                                 ensure_ascii=False).encode()).hexdigest()==s['candidate_sha256']
print('approval signature verifies and the candidate hash matches')"
```

If that last check fails in your implementation, your canonical payload or your candidate hashing disagrees with the contract — and it will reject genuine approvals.

The `vec0` virtual table is commented out in the DDL because it requires the sqlite-vec extension; everything else runs on a stock SQLite build.

## Before building

Set up a reviewer key first — the server refuses to start without one:

```bash
ssh-keygen -t ed25519 -f ~/.memory-agent/approval -C "memory-agent approval"
# put the .pub line and its fingerprint in policy.yaml under learning.approval.reviewers
```

An existing SSH key works too; only the public half goes in the config.

Then read §12 of the spec for the full assumption set. The one that matters:

- **A-3 — caller authentication is still missing.** Reviewer identity is now proven (signed approvals, §8, F18–F23), but nothing authenticates *who is calling*: any orchestration reaching the server can read and write any scope it can name, and `memory_remember` writes are unattributed. Scope isolates logically, not securely. Accepted for solo use on one machine over stdio, where anything that could reach the server could already open `memory.db` directly. **Blocks a work version**, or any second user, second machine, network transport, or client data. Tracked as [B-1 in `BACKLOG.md`](BACKLOG.md).

The CoALA mapping in §3 is verified against the published paper (TMLR 02/2024, OpenReview `1i6ZCvflQJ`): memory modules, action taxonomy, learning modalities, and the planning/execution decision cycle all check out. §3 also casts this agent into the paper's own Table 2 format — it is the only entry with all three long-term memory modules and no external action space.
