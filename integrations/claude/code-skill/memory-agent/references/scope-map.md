# Resolving the scope

Scope is required on six of the nine tools, matching is exact, and a wrong
value fails silently. This is how the Claude Code skill decides which one to
use, and why it asks rather than guesses.

---

## The config file

`~/.memory-agent/claude.yaml`, sibling of `policy.yaml` and `memory.db`.
Template: `integrations/claude/claude.example.yaml` in the repo.

Resolution order for the file itself, mirroring `Policy.load()` so the project
has one idiom rather than two:

1. `$MEMORY_AGENT_CLAUDE_CONFIG`
2. `$MEMORY_AGENT_HOME/claude.yaml`
3. `~/.memory-agent/claude.yaml`

A missing file is valid. It means every scope decision is a question.

```yaml
version: 1
cli: { command: "", policy: "", reviewer: "", key: "" }
scopes:
  "<drive>:/path/to/a-project": a-project
  "<drive>:/path/to/a-project/packages/api": a-project.api
default_scope: null
ritual: { recall_on_start: true, open_cycle: true }
```

`~/.memory-agent/` is outside every checkout, so the file cannot be committed by
accident. `claude.yaml` is also in this repo's `.gitignore` as insurance against
someone copying it into one — but the location is the protection, not the ignore
rule.

## Resolution order for the scope itself

Stop at the first hit.

1. **Stated in the invocation.** `--scope <name>`, or the user naming it in the
   sentence.
2. **Longest-prefix match against the working directory** in `scopes:`.
3. **`$MEMORY_AGENT_SCOPE`**, if set.
4. **Ask.** Never derive, never default.

### Matching rule

- **Longest prefix, not exact.** Working three directories down inside a project
  must resolve through that project's entry. Longest-prefix also gives
  monorepo sub-scoping for free: a deeper entry beats a shallower one.
- **On a path-segment boundary.** `<drive>:/path/to/a-project-old` must not
  match `<drive>:/path/to/a-project`. Compare segment lists, not string prefixes.
- **Normalise before comparing.** Case-insensitively on Windows; `\` and `/`
  are the same separator; trailing separators are stripped; a drive letter's
  case is not significant. `<drive>:/projects/thing` and
  `<DRIVE>:\Projects\Thing\` are the same key.

### Asking well

Never ask an open question. Call `memory_stats` with
`include_scope_breakdown:true` first and present the real `scopes[]` array as a
menu with record counts, plus an explicit "this is a new scope" option the user
has to name themselves. A pick cannot be a typo; free text can.

## Never derive from the folder name

This is the rule most likely to be broken, because breaking it feels helpful.

A directory named `some-tool` is not evidence of a scope named `some-tool`. If
that scope does not already exist, deriving it invents an eleventh scope that
fragments the store permanently and matches nothing on any future recall — and
it looks like it worked. The store's own scope list is the only authority on
which scopes exist.

The same goes for the repo name, the git remote, and the branch.

## A dotted scope is not a hierarchy

`a-project.api` is not a child of `a-project` at query time. It is a different
string. Never pass a truncated scope hoping for prefix behaviour, and never
present recall results from one as though they covered the other.

## Writing to the map

The skill **may append** an entry, and only append:

- Only after the user has stated the scope explicitly in that turn.
- Only with an explicit yes.
- Only appending. Never edit an existing entry, never remove one, never rewrite
  the file wholesale.
- It may create `claude.yaml` from a single dictated entry. It may **never**
  populate it by scanning directories or inferring from names.
- It **never touches `policy.yaml`.** That file holds the reviewer public keys.
  Two config files: one appendable with consent, one never written.

Appending is allowed here where scaffolding a store would not be, and the
distinction is worth keeping straight: scaffolding a store invents data;
recording a mapping the user just dictated writes down a decision they made.
Without the append path, every new project costs a hand-edit that will not
happen, and the skill re-asks the same question every session — which is the
friction that leaves a store unused in the first place.

## When a project moves

The map keys are absolute paths, so moving a checkout breaks its entry. The
symptom is the skill asking about a project it used to know. Fix by appending
the new path; leave the old entry or remove it by hand. The scope name itself
never changes — renaming a scope would orphan every record under the old one,
and there is no rename operation.
