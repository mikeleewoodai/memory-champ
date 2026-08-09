#!/usr/bin/env python
"""Measure recall quality. Turns spec assumption A-4 into a number.

A-4 says local embeddings are good enough. Nothing else in this repo tests that:
`verify.py` checks contracts and the suite checks behaviour, but neither asks
whether recall actually finds the right memory. This does.

    python eval_recall.py

Three arms over one corpus:

    keyword   vector path disabled, FTS5 only. The floor - what recall is worth
              with no embedding at all.
    hashing   the shipped offline default. Lexical vectors, so expected to sit
              close to the floor.
    st        sentence-transformers. Real semantics. Skipped if not installed.

Two query sets, reported separately, because one average hides the answer:

    lexical      the query shares distinctive words with the gold record.
                 Keyword search should already win these. They are the control:
                 an embedding that helps here has proved nothing.
    paraphrase   the query shares the concept and not the words, and the corpus
                 holds records that DO share the query's words. This is the only
                 set that discriminates, and it is the realistic one - an agent
                 asks in its own words, not in the words a fact was stored in.

READ THE NUMBERS WITH THIS IN MIND. The corpus and the gold labels are
hand-written, so this is indicative and not authoritative. With ~10 paraphrase
queries a one-query swing moves R@1 by 10 points, so single metric differences
are noise. What is worth trusting is the per-query table at the bottom and
whether the ordering is consistent across all four metrics.

`in context block` is reported but is weak at this corpus size: the block holds
most of a 30-record store, so it mostly measures "was it returned at all"
rather than how well it ranked. Grow the corpus before leaning on it.
"""
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))  # so this runs without an editable install

from memory_agent.config import Policy
from memory_agent.embedding import HashingEmbedder
from memory_agent.service import MemoryService
from memory_agent.store import Store

SCOPE = "eval.clients"
MAX_TOKENS = 250

CORPUS = {
    "R01": "Acme requires a purchase order number on every invoice; invoices without one are rejected by their AP system.",
    "R02": "Globex settles by wire transfer on net-30 terms and does not use purchase orders.",
    "R03": "Initech requires their security team to sign off before any new subprocessor handles customer data.",
    "R04": "Umbrella will not accept attachments over 10 MB; anything larger has to go through their SFTP drop.",
    "R05": "Wayne Industries schedules all production releases for Tuesday mornings and freezes changes on Fridays.",
    "R06": "Stark Solutions escalates any ticket untouched for 48 hours straight to their account director.",
    "R07": "Cyberdyne's data retention policy deletes support transcripts after 90 days.",
    "R08": "Tyrell Corp requires two named approvers on any contract above $50,000.",
    "R09": "Soylent's finance team closes the books on the fifth business day and will not process late invoices that month.",
    "R10": "Hooli requires all vendor staff to complete their annual security awareness training before system access.",
    "R11": "Massive Dynamic runs a change advisory board that meets Wednesdays; emergency changes need CAB chair approval.",
    "R12": "Vandelay's primary contact prefers phone calls over email for anything time-sensitive.",
    "R13": "Oceanic requires accessibility conformance to WCAG 2.2 AA on any customer-facing screen we deliver.",
    "R14": "Pied Piper's staging environment resets nightly at 02:00 UTC, so test data must be reseeded each morning.",
    "R15": "Gringotts requires all customer data to remain in EU regions; no processing in US datacenters.",
    "R16": "Duff Beverages runs a hard code freeze for the four weeks before their Q4 peak.",
    "R17": "Bluth Company will only accept invoices submitted through their Coupa portal, not by email.",
    "R18": "Sterling Cooper's brand team must review any public-facing copy before it ships.",
    "R19": "Nakatomi requires a signed change order before any work outside the original statement of work begins.",
    "R20": "Prestige Worldwide's SSO integration uses Okta; local passwords are disabled for their tenant.",
    "R21": "Genco's support hours are 07:00 to 19:00 Central, and after-hours pages go to an on-call rotation.",
    "R22": "Monarch Solutions requires quarterly access reviews; dormant accounts are disabled after 60 days.",
    "R23": "Paper Street Co. does not permit screenshots of production data in tickets or documentation.",
    "R24": "Rekall requires a rollback plan documented in the ticket before any schema migration is approved.",
    "R25": "Zorg Industries batches all user provisioning requests weekly rather than handling them ad hoc.",
    "R26": "Aperture requires a post-incident review within five business days of any Sev-1.",
    "R27": "Virtucon's procurement team requires three competing quotes for any purchase above $25,000.",
    "R28": "Wonka Ltd requires allergen disclosures reviewed by their compliance team on any product content.",
    "R29": "Blue Sun requires all API integrations to use mutual TLS; API keys alone are not accepted.",
    "R30": "Weyland Corp's legal team requires a DPIA before any new processing of biometric data.",
}

# (query, gold, category). The comments name the lexical distractors each
# paraphrase query has to beat - a paraphrase query with no competing record
# would be easy for the wrong reason.
QUERIES = [
    # -- paraphrase: the set that discriminates ---------------------------
    ("who has to say yes before we bring in an outside company that touches client records",
     "R03", "paraphrase"),          # vs R10 vendor staff, R27 procurement, R08 approvers
    ("what is the biggest thing I can send them without it bouncing",
     "R04", "paraphrase"),          # no lexical overlap with the gold at all
    ("which day of the week do their releases go out",
     "R05", "paraphrase"),          # vs R11 "meets Wednesdays"
    ("how long do we keep chat logs from support conversations",
     "R07", "paraphrase"),          # "chat logs" vs "support transcripts"
    ("can their information sit on machines in America",
     "R15", "paraphrase"),
    ("what happens to a login nobody has touched in months",
     "R22", "paraphrase"),          # vs R06 "ticket untouched for 48 hours"
    ("do we have to write down how we would undo a database change",
     "R24", "paraphrase"),          # "undo" vs "rollback"
    ("who checks wording before customers see it",
     "R18", "paraphrase"),          # vs R28 compliance review, R13 customer-facing
    ("what do we owe them after a major outage",
     "R26", "paraphrase"),          # "major outage" vs "Sev-1"
    ("how do their people authenticate",
     "R20", "paraphrase"),          # vs R10 "system access"
    # -- lexical: the control --------------------------------------------
    ("purchase order number required on every invoice", "R01", "lexical"),
    ("wire transfer net-30 terms", "R02", "lexical"),
    ("WCAG 2.2 AA accessibility conformance", "R13", "lexical"),
    ("invoices through the Coupa portal", "R17", "lexical"),
    ("mutual TLS for API integrations", "R29", "lexical"),
    ("hard code freeze before Q4 peak", "R16", "lexical"),
    ("signed change order for work outside the statement of work", "R19", "lexical"),
    ("three competing quotes from procurement", "R27", "lexical"),
]


def build(arm, embedder):
    policy = Policy()
    policy.db_path = ":memory:"
    policy.require_vector_extension = False
    policy.embedding_provider = "hashing"   # the embedder is injected directly
    store = Store(":memory:", dimensions=embedder.dimensions)
    if arm == "keyword":
        # Disabled before any write, so nothing is embedded and nothing is
        # vector-searched. A true floor, not a store with vectors it ignores.
        store.vector_ok = False
    svc = MemoryService(policy, store, embedder)
    ids = {}
    for key, text in CORPUS.items():
        ids[svc.remember(scope=SCOPE, type="semantic", content=text)["record_id"]] = key
    return svc, ids


def evaluate(arm, embedder):
    svc, ids = build(arm, embedder)
    rows, latencies = [], []
    for query, gold, category in QUERIES:
        t0 = time.perf_counter()
        res = svc.recall(scope=SCOPE, query=query, max_tokens=MAX_TOKENS)
        latencies.append((time.perf_counter() - t0) * 1000)

        ranked = [ids.get(r["record"]["id"]) for r in res["records"]]
        rows.append(dict(
            gold=gold, category=category,
            rank=ranked.index(gold) + 1 if gold in ranked else None,
            in_block=any(ids.get(r["record"]["id"]) == gold and r["in_context_block"]
                         for r in res["records"])))
    svc.close()
    return rows, latencies


def summarise(rows, category=None):
    sel = [r for r in rows if category is None or r["category"] == category]
    n = len(sel)
    return dict(
        n=n,
        r1=sum(1 for r in sel if r["rank"] and r["rank"] <= 1) / n,
        r3=sum(1 for r in sel if r["rank"] and r["rank"] <= 3) / n,
        r5=sum(1 for r in sel if r["rank"] and r["rank"] <= 5) / n,
        mrr=sum(1 / r["rank"] for r in sel if r["rank"]) / n,
        block=sum(1 for r in sel if r["in_block"]) / n,
    )


def main() -> int:
    arms = [("keyword", HashingEmbedder(384)), ("hashing", HashingEmbedder(384))]
    try:
        from memory_agent.embedding import SentenceTransformerEmbedder
        arms.append(("st", SentenceTransformerEmbedder("all-MiniLM-L6-v2")))
    except Exception as exc:
        print(f"!! sentence-transformers arm skipped: {type(exc).__name__}: {exc}")
        print("   pip install -e \".[embeddings]\" to include it\n")

    results = {}
    for arm, embedder in arms:
        print(f"running {arm} ({embedder.name}) ...", flush=True)
        results[arm] = evaluate(arm, embedder)

    print("\n" + "=" * 78)
    for label, cat in (("PARAPHRASE (the set that discriminates)", "paraphrase"),
                       ("LEXICAL (control - all arms should tie)", "lexical"),
                       ("OVERALL", None)):
        print(f"\n{label}")
        print(f"  {'arm':10} {'R@1':>6} {'R@3':>6} {'R@5':>6} {'MRR':>6} {'in ctx block':>14}")
        for arm in results:
            s = summarise(results[arm][0], cat)
            print(f"  {arm:10} {s['r1']:6.0%} {s['r3']:6.0%} {s['r5']:6.0%} "
                  f"{s['mrr']:6.2f} {s['block']:13.0%}")

    print("\nmedian recall latency (ms)")
    for arm in results:
        print(f"  {arm:10} {statistics.median(results[arm][1]):6.1f}")

    print("\n" + "=" * 78)
    print("per-paraphrase-query rank - trust this over the aggregates above,")
    print("which move 10 points on a single query. '-' = never returned.")
    print(f"\n  {'gold':5} {'kw':>4} {'hash':>5} {'st':>4}   query")
    for i, (q, gold, cat) in enumerate(QUERIES):
        if cat != "paraphrase":
            continue
        cells = []
        for arm in ("keyword", "hashing", "st"):
            if arm not in results:
                cells.append("   ?")
                continue
            r = results[arm][0][i]["rank"]
            cells.append(f"{r:>4}" if r else "   -")
        print(f"  {gold:5} {cells[0]} {cells[1]} {cells[2]}   {q[:50]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
