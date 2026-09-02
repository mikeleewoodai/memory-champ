# The approval gate

Procedures become records only when a human signs for them. This file is how the
skill helps with that without ever being the one who signs.

---

## The boundary

**You do not approve. You do not reject. Not ever.**

- You never call `memory_review_proposals` with `action:"approve"` or
  `action:"reject"`.
- You never run `memory-agent review approve` or `review reject`.
- You never read, copy, decrypt, move, or open the private key.
- You never type, prompt for, cache, or set a passphrase, and you never set
  `MEMORY_AGENT_PASSPHRASE`. That variable exists so the *reviewer* can automate
  their own approvals. An agent setting it is precisely the attack the design
  exists to prevent.

Not when asked directly. Not when the approval looks obvious. Not to "check
that it works". If you find yourself about to, stop and say why you are not.

The reason is structural, not procedural: an agent that can approve its own
proposals has no gate at all. The private key lives with the reviewer and never
reaches the server, which is what makes this a control rather than a convention.

**Do not summarise a proposal persuasively.** Show it in full and let the
reviewer read it. A confident summary from you is exactly what would stop a
human from reading the candidate they are about to sign for.

## How signing actually works

Two calls, so a signature approves the exact thing that was shown:

1. `action:"list"` returns each pending proposal with a `signing_payload`
   carrying a server-issued single-use nonce and `candidate_sha256`, a hash of
   the candidate as displayed.
2. The reviewer signs those bytes with their Ed25519 key and calls
   `action:"approve"` with the signature.

Because the payload pins the hash, editing the candidate in between invalidates
the signature (`APPROVAL_CANDIDATE_CHANGED`). Because the nonce is single-use
and short-lived (`challenge_ttl_seconds`, 600 by default), a signature cannot be
stockpiled or replayed.

**The CLI re-lists internally to obtain its own fresh nonce.** So you never
handle a signing payload at all — you only ever produce a command line.

## The handover

```
<cli> --policy <policy> review list
<cli> --policy <policy> review approve <proposal_id> --reviewer <you> --key <key>
```

`--policy` is a **root parser flag: it comes before the subcommand.** Written
after it, argparse rejects it.

Then say what will happen when it runs, and stop. The reviewer is prompted for
the passphrase in their own terminal. That prompt is the design working.

## Before printing any CLI command: the cross-check

A `--policy` pointing at a path that does not exist **does not error.** It falls
through to built-in defaults, whose `db_path` is `./memory.db` relative to the
process working directory — so the CLI silently creates an empty store inside
whatever directory it was run from and reports zero records. That is not a
hypothetical; it is how stray `memory.db` files end up inside unrelated repos.

So, every time, before handing over a command:

1. Confirm the `cli` and `policy` paths from `claude.yaml` both **exist on disk**.
2. Run `<cli> --policy <policy> stats --json`.
3. **Compare its `counts.total` and `queue.pending_proposals` against what the
   MCP `memory_stats` just returned.** If they disagree, the CLI is pointed at a
   different database. Do not print the command. Report the mismatch and stop.

This is a better gate than a `--help` probe, because `--help` succeeds against
the wrong store just as happily as the right one.

## Reading the result

`approve` prints `approved <id> -> record <id> (signature verified)`. Anything
under `skipped` carries a reason and did **not** apply:

| Reason | Means |
|---|---|
| `APPROVAL_SIGNATURE_INVALID` | The signature did not verify. |
| `APPROVAL_KEY_UNKNOWN` | The key is not a reviewer in `policy.yaml`. |
| `APPROVAL_CHALLENGE_INVALID` | The nonce expired or was already spent. Re-list and retry. |
| `APPROVAL_CANDIDATE_CHANGED` | The candidate changed after it was shown. Working as intended. |
| `NO_REVIEWER_KEYS_CONFIGURED` | No reviewer exists. Nothing can be approved until one is added — `memory-agent init`. |

A non-zero exit with skips means some proposals are still pending. Say which.

## Re-verification

`<cli> --policy <policy> verify` re-checks every stored approval signature.
Anything it lists must be treated as unapproved until re-signed. Run it after
any restore, any key change, or any suspicion that the database was edited
outside the tools.

## When there is no CLI

Approving requires the private key, and the CLI is the only place a private key
is ever read. Without a resolvable CLI — which is the normal situation in
Cowork — the honest report is:

> Queued as `<proposal_id>`. Approving needs a terminal: the signing key never
> reaches this session.

Queue it, show it, and say where it can be signed. Do not attempt a workaround.
