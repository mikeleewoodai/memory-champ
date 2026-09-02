# Memory hygiene

What belongs in the store, what does not, and the rules that keep a scope
argument from becoming a data-handling failure. Shared by both hosts.

---

## Scope is exact, and nothing binds it to the filesystem

Scope matching is a string equality test. There is no wildcard and no hierarchy
at query time: a dotted scope looks like a tree and is not one. Recalling
`parent` when the records are under `parent.child` returns nothing — not "the
parent's records", not an error. Nothing.

Nothing in the system ties a scope to a directory, a repo, or a git remote.
**A wrong scope argument is the only way memory crosses between projects**, and
it fails silently in both directions: a wrong-scope read looks like an empty
project, a wrong-scope write looks like a successful save.

So:

- **Never invent a scope.** Not from the folder name, not from the repo name,
  not from the remote. A folder called `widget-tool` is not evidence that a
  scope named `widget-tool` exists.
- **Validate before writing.** `memory_stats` with
  `include_scope_breakdown:true` returns every scope that exists with its
  record count. If the resolved scope is not in that list, this is a *new*
  scope — say so and confirm before writing. This is what catches a typo, which
  is otherwise indistinguishable from an empty project.
- **Say the scope and its count on every operation.** `scope <name> · N records`.
  If the wrong project is in play, the count is wrong on the first line, before
  anything is written.
- **Reads and writes have different bars.** A wrong-scope read costs a message.
  A wrong-scope write is durable and invisible. Reads may proceed from any
  resolution; writes require a scope that was either stated explicitly in this
  turn or matched from configuration.

## Caller authentication is not built (B-1)

Any caller reaching this server can read and write any scope it can name, and
`memory_remember` writes are unattributed. **Scope isolates logically, not
securely.** It is accepted for one person on one machine over stdio.

Two consequences that are not negotiable:

- **No third-party or client data goes into this store, in any scope.** The
  check is on the *content*, not the scope argument — otherwise a filing error
  becomes a disclosure. If the content belongs to someone who did not choose
  this store, it does not go in.
- A scope is a filing decision, not a security boundary. Do not describe it as
  one to the user.

## What is worth writing

Durable, and true beyond this conversation:

- A decision and the tension behind it — not just the outcome.
- A constraint discovered the hard way, with its consequence.
- A correction to something the store already believes (use `supersedes`).
- What was actually done, when the *doing* is what matters later (`episodic`).

Not worth writing:

- **"Keep this in mind for now" is not a write.** Conversational scratch belongs
  in the conversation. This is the single fastest way to turn a useful store
  into an unsearchable one.
- Anything derivable from the code, the git history, or a file that is already
  in the repo.
- A restatement of something already stored. Recall first; supersede if it
  changed, otherwise leave it alone.
- Speculation. If confidence is low, either say so in the `confidence` field or
  do not write it.

## What makes a procedure worth proposing

All four must hold. Any one missing and it is a `remember`, or nothing.

1. **It is a sequence, not a fact.** "When X, do 1..n" is procedural. "X is
   true" is semantic.
2. **Evidence exists in this session.** It was done twice, or once after a
   failed attempt. A failure that taught the correct order is the most valuable
   procedure there is.
3. **It is not already stored.** Recall with `types:["procedural"]` against the
   trigger text first.
4. **It survives a cold read.** No "the file we edited", no "that command from
   earlier". Someone with no memory of this session can execute it.

Trigger moments, in descending order of signal:

- **A recall returned nothing and the session then solved the problem.** The
  store just proved it has that gap. This is the highest-value moment and the
  one most often missed.
- The session hit a documented trap and recovered — the recovery is the procedure.
- The user says: "remember how to do this", "next time do it this way", "always
  X before Y", "don't do that again".
- Something already done once successfully got done again.

## Standing refusals

- **Never write `type: "procedural"` through `memory_remember`.** Gated by design.
- **`memory_forget` runs `dry_run:true` first, every time**, and the real call
  needs an explicit confirmation naming what will be removed. Prefer
  `supersedes` — forgetting loses the chain that explains why a fact changed.
- **Never open the SQLite file, `policy.yaml`, or the key files directly.** All
  reads and writes go through the tools. `policy.yaml` holds the reviewer public
  keys; it is the trust root.
- **Never approve or reject a proposal.** See `review-gate.md`. There is no
  argument that makes this yours.
- **A `degraded` recall is "could not look", not "nothing is known."**
- **Report zeros as findings**, not as health.
