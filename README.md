# memory-agent

A memory service for agent orchestrations, structured on **CoALA** — the cognitive architecture from *Cognitive Architectures for Language Agents* (Sumers, Yao, Narasimhan & Griffiths, Princeton; TMLR 2024; arXiv:2309.02427).

**Status: implemented.** Nine MCP tools, SQLite + sqlite-vec storage, an independent daemon, and a signing CLI. `verify.py` and the test suite are both green, with one test per acceptance criterion in the spec.

It stores three kinds of memory rather than one — **what happened** (episodic), **what is true** (semantic), **how to do something** (procedural) — exposes them over MCP so any orchestration can attach, and also runs on its own schedule to consolidate and prune its own store with no host present.

It has no grounding actions. It never reaches the network, never touches the filesystem outside its database, never talks to a user. That is what makes it safe to attach to an arbitrary agentic loop. The one network exception is opt-in: with the `embeddings` extra installed, the first run downloads the model from Hugging Face, once.

## Start here — the brief

**[mikeleewoodai.github.io/memory-champ](https://mikeleewoodai.github.io/memory-champ/)** explains this project to someone who has never seen it: the problem it solves, how it works in plain English, and the technical detail folded into collapsible sections underneath. It names the limits too, including the one that blocks a work version.

The source is [`docs/memory-champ-brief.html`](docs/memory-champ-brief.html), a single self-contained file — all CSS and graphics inline, nothing fetched — so it reads the same offline. GitHub serves `.html` as source rather than rendering it, which is what the published copy is for.

The tool contract is published too: every `$id` in `contracts/` resolves under **[/v1/](https://mikeleewoodai.github.io/memory-champ/v1/)**.

## Install

You need **Python 3.10 or newer** and nothing else: no git, no pipx. Four
steps: install into a virtual environment, run `init`, connect your host, check
it works.

It is **not on PyPI** — `pip install memory-champ` on its own finds nothing.
Install from GitHub as below.

### 1. Install into a virtual environment

**macOS / Linux**

```bash
python3 -m venv ~/memory-champ-venv
```

```bash
~/memory-champ-venv/bin/pip install "memory-champ[recommended] @ https://github.com/mikeleewoodai/memory-champ/archive/refs/heads/main.zip"
```

**Windows (PowerShell)**

```powershell
python -m venv $HOME\memory-champ-venv
```

```powershell
& $HOME\memory-champ-venv\Scripts\pip.exe install "memory-champ[recommended] @ https://github.com/mikeleewoodai/memory-champ/archive/refs/heads/main.zip"
```

Use the form for your shell. bash and zsh expand `~`; PowerShell and cmd pass it
through literally, so the macOS line run on Windows creates a folder named `~`
in the current directory. In cmd, write `%USERPROFILE%` where the PowerShell
lines say `$HOME`, and drop the leading `&`. If Windows cannot find `python`,
try `py`.

Calling `pip` and `memory-agent` by full path is deliberate: nothing has to be
on your PATH, and there is no venv to activate.

**Type `[recommended]` exactly as written.** It is not a placeholder — it is the
name of an optional dependency group. See [Which extras](#which-extras). The URL
is GitHub's zip of `main`, which is why pip needs no git; if you have git,
`git+https://github.com/mikeleewoodai/memory-champ` works as well.

### 2. Run `init`

```bash
~/memory-champ-venv/bin/memory-agent init --no-passphrase
```

```powershell
& $HOME\memory-champ-venv\Scripts\memory-agent.exe init --no-passphrase
```

It creates `~/.memory-agent/` with a reviewer key and `policy.yaml` (the
database follows on first write), then prints the MCP config block for your
host. [What `init` does](#what-init-does) has the detail.

`--no-passphrase` keeps it non-interactive, and writes the reviewer key
**unencrypted**. Drop it to be prompted for a passphrase — do that if an agent
with shell access runs on this machine, because an unencrypted key lets that
agent sign its own approvals. To supply one from a file or the environment, see
[Passphrases without a terminal](#passphrases-without-a-terminal).

### 3. Connect your host

Do the one for the host you use. The commands show macOS/Linux paths; on
Windows, use `& $HOME\memory-champ-venv\Scripts\memory-agent.exe` and
`$HOME\memory-champ-venv\Scripts\python.exe`.

**Claude Desktop** — one command, then restart Claude Desktop:

```bash
~/memory-champ-venv/bin/memory-agent install-claude-desktop
```

It merges into `claude_desktop_config.json`, backs the file up first, leaves
every other server alone, and refuses outright if the file does not parse —
that file holds all your other MCP servers, and rewriting one we could not read
would destroy them. `--dry-run` shows the result without writing.

**Claude Code** — register the server, install the skill, then restart Claude
Code:

```bash
claude mcp add --scope user memory-champ -- ~/memory-champ-venv/bin/python -m memory_agent.server
```

```powershell
claude mcp add --scope user memory-champ '--' $HOME\memory-champ-venv\Scripts\python.exe -m memory_agent.server
```

```bash
~/memory-champ-venv/bin/memory-agent install-claude-code
```

**In PowerShell, quote the `'--'`.** The npm-installed `claude` is a PowerShell
script, and PowerShell consumes a bare `--` itself, so `claude` never sees it
and fails with `unknown option '-m'`.

`--scope user` makes the server available in every project. The interpreter
after `--` must be the one that has the package — the `command` value `init`
printed. If you installed with pipx or uv, use that value instead.

`install-claude-code` copies the `memory-agent` skill into `~/.claude/skills/`,
which registers `/memory-agent`: recall what a project already knows, write down
what was learned, queue procedures for approval, and read store health. It
refuses to overwrite a destination that differs (`--force` replaces it, after a
backup), and `--check` reports drift without writing: exit 0 matches, 1
drifted, 2 absent.

Scope is the one thing the skill cannot derive, so it reads a map from
`~/.memory-agent/claude.yaml`. Copy
[`integrations/claude/claude.example.yaml`](integrations/claude/claude.example.yaml)
there and fill it in. Without it the skill asks instead of guessing, which is
the intended fallback rather than a broken state.

**Cowork** — do the Claude Desktop step first, because Cowork uses that server.
Then build the plugin from a copy of this repo (clone it, or download and
extract the [zip](https://github.com/mikeleewoodai/memory-champ/archive/refs/heads/main.zip)):

```bash
git clone https://github.com/mikeleewoodai/memory-champ
```

```bash
python3 memory-champ/build_plugin.py dist/
```

That writes `dist/memory-champ-<version>.plugin` — install that file as a plugin
in Cowork. `build_plugin.py` uses only the standard library, so any Python 3.10+
runs it (`python` on Windows). The plugin carries the skill only, so Cowork does
not start a second server. Cowork cannot sign approvals: proposals queue there
and get signed from Claude Code or a terminal. See
[`integrations/claude/cowork-plugin/README.md`](integrations/claude/cowork-plugin/README.md).

### 4. Check it works

```bash
~/memory-champ-venv/bin/memory-agent stats
```

A fresh store prints `records: 0` and `queue: 0 pending`. Then open a new session
in your host and ask it to call `memory_stats`. A host can list a server as
connected while its tool calls still fail, so the tool call is the real check.

### Other ways to install

<details>
<summary>pipx (nicer, but one more thing to install)</summary>

pipx keeps the CLI in its own environment and puts it on your PATH, so the bare
`memory-agent` replaces the full paths in the steps above. It is the better
long-term arrangement for a command-line tool; it just is not present by
default, so the plain-venv route is what this README leads with.

```bash
python -m pip install --user pipx
```

```bash
python -m pipx install "memory-champ[recommended] @ https://github.com/mikeleewoodai/memory-champ/archive/refs/heads/main.zip"
```

Invoking it as `python -m pipx` rather than `pipx` avoids the first thing that
goes wrong — a fresh `pip install --user pipx` frequently leaves `pipx` itself
off your PATH. `python -m pipx ensurepath` fixes that for later shells.

</details>

<details>
<summary>uv</summary>

```bash
uv tool install "memory-champ[recommended] @ https://github.com/mikeleewoodai/memory-champ/archive/refs/heads/main.zip"
```

**On Windows, `uv tool install` puts scripts in `%USERPROFILE%\.local\bin`,
which is not on PATH by default.** Either add it, or call the command by its
full path. `uv tool update-shell` adds it for you.

> **Known issue — the uv trampoline.** Some `uv`-installed console scripts fail
> on Windows with an error about a trampoline or a missing interpreter, usually
> after the Python that `uv` linked against moves or is upgraded. It is not
> specific to this package. Use the venv or pipx route instead, or
> `uv tool install --force --reinstall` to relink. A plain venv always works,
> because the script points at an interpreter you control.

</details>

Install it as `memory-champ`; the command is `memory-agent`, and `memory-champ`
works as well. The two names differ because an unrelated project already
publishes under `memory-agent`.

### Which extras

`recommended` is vector recall, exact token counting, and the MCP server —
everything except semantic embeddings, which pull torch and about 2 GB. Add
those when lexical overlap stops being good enough, a call worth making against
your own corpus rather than up front:

```bash
~/memory-champ-venv/bin/pip install "memory-champ[all] @ https://github.com/mikeleewoodai/memory-champ/archive/refs/heads/main.zip"
```

The first run after that downloads the embedding model from Hugging Face, once;
later loads come from the local cache with the Hub switched to offline.

Only `cryptography`, `PyYAML`, and `bcrypt` are truly required. The rest
degrades visibly rather than failing: no `sqlite-vec` means keyword-only recall
that says so, no `sentence-transformers` means the built-in hashing embedder,
no `tiktoken` means a conservative token bound.

Working on the code instead of using it? `pip install -e ".[all,dev]"` — `dev`
is what brings in `pytest` and `jsonschema`, and `[all]` deliberately does not
include it, so a bare `.[all]` leaves the `pytest -q` below with nothing to run.

Python 3.10+, tested on 3.10 through 3.14.

### Passphrases without a terminal

The reviewer key can be encrypted, and by default `init` asks for a passphrase
interactively. Anything scripted should not go near that prompt — pick one of:

| | |
|---|---|
| `--no-passphrase` | write the key unencrypted |
| `--passphrase-file PATH` | read it from the file's first line |
| `MEMORY_AGENT_PASSPHRASE=…` | read it from the environment |
| `MEMORY_AGENT_NONINTERACTIVE=1` | refuse to prompt at all, and fail immediately |

All four work on `init`, `keygen`, and `review approve`.

Why this matters more than it looks: on Windows, `getpass` writes its prompt
with `msvcrt.putwch`, straight to the console device rather than to stdout or
stderr. A caller reading pipes sees **nothing**, and the read then blocks
forever — so an unanswered prompt presents as a silent hang with no clue what
is wanted. `sys.stdin.isatty()` does not save you either: agent harnesses and
CI runners hand the process a pty, so it answers `True` with nobody there.

So the prompt now announces itself on stdout before it appears, and gives up on
a deadline (60s, `MEMORY_AGENT_PROMPT_TIMEOUT` to change) rather than hanging.
Use the table above and you never reach it.

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
| [`eval_recall.py`](eval_recall.py) | Recall quality, as opposed to correctness (A-4) |
| [`HANDOVER.md`](HANDOVER.md) | Read-cold orientation, and the things most likely to trip you up |
| [`integrations/`](integrations/README.md) | Claude Code skill and Cowork plugin, with the I-series gates that keep machine paths out of them |
| [`build_plugin.py`](build_plugin.py) | Packages the Cowork plugin. Refuses to build a bundle carrying an absolute path |
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
