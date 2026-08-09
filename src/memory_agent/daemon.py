"""Independent mode: the CoALA decision cycle run over the agent's own store.

No host present. Because the agent has no grounding actions, its cycles can only
yield learning actions, so this is a pure learning loop - planning (propose,
evaluate, select) then execution, exactly as in CoALA §4.6.

    python -m memory_agent.daemon --once
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import timedelta

from .approval import rfc3339, utcnow
from .config import Policy
from .service import MemoryService

log = logging.getLogger("memory_agent.daemon")


def run_once(service: MemoryService, scopes: list[str] | None = None) -> dict:
    policy = service.policy
    now = utcnow()
    report: dict = {"started_at": rfc3339(now), "scopes": {}}

    # --- housekeeping that is not scope-specific --------------------------
    if policy.reap_abandoned_cycles:
        report["cycles_reaped"] = service.reap_cycles(now)

    expired = service.store.expired_records(rfc3339(now))
    report["ttl_expired"] = service.store.set_status(expired, "tombstoned")

    cutoff = rfc3339(now - timedelta(days=policy.learning.proposal_expiry_days))
    report["proposals_expired"] = service.store.expire_proposals(cutoff)

    # Re-embed anything whose vector is missing or was made by another model.
    # Recall stays available on the keyword path while this runs.
    reembedded = 0
    if service.store.vector_ok:
        for row in service.store.unembedded(service.embedder.name, 500):
            vec = service.embedder.embed([row["content"]])[0]
            service.store.set_embedding(row["seq"], row["id"], vec, service.embedder.name)
            reembedded += 1
    report["reembedded"] = reembedded

    if policy.learning.approval.reverify_on_daemon_run:
        # A procedure edited after approval must stop being trusted rather than
        # quietly continuing to be recalled as approved.
        report["approval_problems"] = service.reverify_approvals()

    # --- the learning cycle, per scope ------------------------------------
    targets = scopes or [s["scope"] for s in service.store.scopes()]
    for scope in targets:
        # auto_commit stays false: the daemon runs unattended, and an unattended
        # process committing distilled facts is how a store starts believing
        # things nobody checked. Semantic results queue as proposals.
        result = service.reflect(
            scope=scope,
            modes=["consolidate", "contradictions", "promote", "importance"],
            window={"lookback_days": policy.forgetting.consolidate_episodes_older_than_days},
            auto_commit=False, now=now)
        report["scopes"][scope] = {
            "episodes_examined": result["episodes_examined"],
            "proposals_created": len(result["proposals_created"]),
            "contradictions": len(result["contradictions"]),
            "importance_revised": result["importance_revised"],
        }

    service.store.set_meta("last_daemon_run_at", rfc3339(utcnow()))
    report["finished_at"] = rfc3339(utcnow())
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="memory-agent-daemon", description=__doc__)
    ap.add_argument("--policy", help="path to policy.yaml")
    ap.add_argument("--scope", action="append", help="limit to this scope (repeatable)")
    ap.add_argument("--once", action="store_true", help="run one pass and exit (the default)")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = ap.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
    service = MemoryService(Policy.load(args.policy))
    try:
        report = run_once(service, args.scope)
    finally:
        service.close()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"reaped {report.get('cycles_reaped', 0)} cycle(s), "
              f"expired {report.get('ttl_expired', 0)} record(s) and "
              f"{report.get('proposals_expired', 0)} proposal(s), "
              f"re-embedded {report.get('reembedded', 0)}")
        for scope, s in report["scopes"].items():
            print(f"  {scope}: {s['episodes_examined']} episodes -> "
                  f"{s['proposals_created']} proposal(s), {s['contradictions']} contradiction(s)")
        problems = report.get("approval_problems") or []
        if problems:
            print(f"\n  {len(problems)} APPROVAL PROBLEM(S) - treat these as unapproved:")
            for p in problems:
                print(f"    {p['record_id']}: {p['problem']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
