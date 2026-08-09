#!/usr/bin/env python3
"""Verify the memory-agent contracts.

There is no implementation yet, so this is what "the tests pass" means for now:
the schemas are valid and resolve, the DDL applies and its invariants actually
bite, the example fixtures validate, and the published approval signature really
verifies. Run it before and after any change to contracts/.

    pip install jsonschema pyyaml cryptography
    python verify.py

Exit code is 0 on success, 1 on any failure, so it drops straight into CI.
"""

import base64
import glob
import hashlib
import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FAILURES: list[str] = []
CHECKS = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global CHECKS
    CHECKS += 1
    if not ok:
        FAILURES.append(f"{name}{': ' + detail if detail else ''}")
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f"  <- {detail}" if not ok and detail else ""))
    return ok


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def p(*parts: str) -> str:
    return os.path.join(HERE, *parts)


# --------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------
def load_registry():
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    items = []
    for f in sorted(glob.glob(p("contracts", "schemas", "*.json"))):
        schema = json.load(open(f))
        Draft202012Validator.check_schema(schema)
        items.append((schema["$id"], Resource.from_contents(schema)))

    tools = json.load(open(p("contracts", "mcp-tools.json")))
    for tool in tools["tools"]:
        for key in ("inputSchema", "outputSchema"):
            if "$id" in tool[key]:
                items.append((tool[key]["$id"], Resource.from_contents(tool[key])))

    registry = Registry().with_resources(items)
    by_name = {
        os.path.basename(sid): res.contents
        for sid, res in items
        if os.path.basename(sid).endswith(".schema.json")
    }
    return registry, by_name, tools


def verify_schemas(registry, by_name, tools):
    from jsonschema import Draft202012Validator

    section("schemas")
    for name, schema in sorted(by_name.items()):
        try:
            # forces every $ref to resolve
            list(Draft202012Validator(schema, registry=registry).iter_errors({}))
            check(f"{name} valid and $refs resolve", True)
        except Exception as exc:
            check(f"{name} valid and $refs resolve", False, str(exc)[:160])

    section("tool contract")
    check("9 tools defined", len(tools["tools"]) == 9, f"got {len(tools['tools'])}")
    for tool in tools["tools"]:
        has_both = "inputSchema" in tool and "outputSchema" in tool
        has_id = all("$id" in tool[k] for k in ("inputSchema", "outputSchema") if k in tool)
        annotated = all(
            a in tool.get("annotations", {})
            for a in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
        )
        check(
            f"{tool['name']}: schemas + $ids + annotations + coala label",
            has_both and has_id and annotated and bool(tool.get("coala")),
        )
    check(
        "no grounding actions (openWorldHint false everywhere)",
        all(t["annotations"]["openWorldHint"] is False for t in tools["tools"]),
    )


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
FIXTURES = [
    ("episodic", "episodic-record.schema.json", "record"),
    ("semantic", "semantic-record.schema.json", "record"),
    ("procedural", "procedural-record.schema.json", "record"),
    ("working_set", "working-set.schema.json", "record"),
    ("recall_response", "recall-response.schema.json", "response"),
]


def verify_fixtures(registry, by_name):
    from jsonschema import Draft202012Validator

    ex = json.load(open(p("contracts", "examples", "records.json")))
    section("example fixtures validate")
    for name, schema_file, key in FIXTURES:
        errors = list(Draft202012Validator(by_name[schema_file], registry=registry).iter_errors(ex[name][key]))
        check(name, not errors, errors[0].message[:140] if errors else "")

    # The constraints have to reject bad data, or they are decoration.
    section("schema constraints bite")
    proc = ex["procedural"]["record"]
    sig = proc["approval"]["signature"]
    validator = Draft202012Validator(by_name["procedural-record.schema.json"], registry=registry)
    semantic = Draft202012Validator(by_name["semantic-record.schema.json"], registry=registry)
    episodic = Draft202012Validator(by_name["episodic-record.schema.json"], registry=registry)

    def without(d, k):
        return {kk: vv for kk, vv in d.items() if kk != k}

    def approval(**over):
        return {**proc, "approval": {**proc["approval"], **over}}

    def signature(**over):
        return approval(signature={**sig, **over})

    negatives = [
        ("approved without a signature", validator, {**proc, "approval": without(proc["approval"], "signature")}),
        ("rejected without a signature", validator,
         {**proc, "approval": {"state": "rejected", "reviewed_by": "m", "reviewed_at": "2026-04-02T10:29:44Z"}}),
        ("approved without a reviewer", validator, approval(**{"reviewed_by": None})),
        ("signature missing signed_payload", validator, signature(**{"signed_payload": None})),
        ("malformed key_id", validator, signature(key_id="mike")),
        ("candidate_sha256 not a hash", validator, signature(candidate_sha256="nope")),
        ("algorithm other than ed25519", validator, signature(alg="rsa")),
        ("semantic predicate without subject", semantic, without(ex["semantic"]["record"], "subject")),
        ("importance above 1", semantic, {**ex["semantic"]["record"], "importance": 1.4}),
        ("unknown top-level property", semantic, {**ex["semantic"]["record"], "sneaky": True}),
        ("episodic missing step_no", episodic, without(ex["episodic"]["record"], "step_no")),
    ]
    for name, val, instance in negatives:
        check(f"rejects: {name}", not val.is_valid(instance))
    check("allows: pending proposal with no signature", validator.is_valid({**proc, "approval": {"state": "pending"}}))
    return ex


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------
UUID_A = "aaaaaaaa-1111-2222-3333-444444444444"
UUID_B = "bbbbbbbb-1111-2222-3333-444444444444"


def verify_ddl():
    section("DDL applies")
    con = sqlite3.connect(":memory:")
    con.executescript(open(p("contracts", "db", "schema.sql")).read())
    con.execute("PRAGMA foreign_keys=ON")
    objects = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    for expected in ("records", "episodic_attrs", "semantic_attrs", "procedural_attrs",
                     "working_set", "observations", "proposals", "approval_challenges",
                     "records_fts", "v_recallable", "v_approval_audit", "v_pending_proposals"):
        check(f"{expected} exists", expected in objects)

    def attempt(sql, args=()):
        """True if the statement was accepted."""
        try:
            con.execute(sql, args)
            con.commit()
            return True
        except Exception:
            con.rollback()
            return False

    def record(rid, rtype="semantic", scope="proj.a", content="c", idem=None):
        return attempt(
            "INSERT INTO records (id,type,scope,content,created_at,idempotency_key) "
            "VALUES (?,?,?,?,'2026-08-08T00:00:00Z',?)", (rid, rtype, scope, content, idem))

    section("loop safety")
    check("first keyed write accepted", record("11111111-1111-2222-3333-444444444444", content="terse email", idem="loop-1"))
    check("duplicate key in same scope rejected",
          not record("22222222-1111-2222-3333-444444444444", idem="loop-1"))
    check("same key in a different scope accepted",
          record("33333333-1111-2222-3333-444444444444", scope="proj.b", idem="loop-1"))
    check("null keys never collide",
          record("44444444-1111-2222-3333-444444444444") and record("55555555-1111-2222-3333-444444444444"))
    check("episodic step uniqueness enforced",
          record("66666666-1111-2222-3333-444444444444", "episodic")
          and attempt("INSERT INTO episodic_attrs (record_id,session_id,cycle_id,step_no) VALUES (?,'s1','c1',0)",
                      ("66666666-1111-2222-3333-444444444444",))
          and record("77777777-1111-2222-3333-444444444444", "episodic")
          and not attempt("INSERT INTO episodic_attrs (record_id,session_id,cycle_id,step_no) VALUES (?,'s1','c1',0)",
                          ("77777777-1111-2222-3333-444444444444",)))

    section("keyword index stays in sync")
    check("insert trigger indexed content",
          con.execute("SELECT count(*) FROM records_fts WHERE records_fts MATCH 'terse'").fetchone()[0] == 1)
    con.execute("UPDATE records SET content='verbose email' WHERE id=?", ("11111111-1111-2222-3333-444444444444",))
    con.commit()
    check("update trigger re-indexed",
          con.execute("SELECT count(*) FROM records_fts WHERE records_fts MATCH 'terse'").fetchone()[0] == 0
          and con.execute("SELECT count(*) FROM records_fts WHERE records_fts MATCH 'verbose'").fetchone()[0] == 1)

    section("supersession is atomic")
    check("status without a successor rejected",
          not attempt("UPDATE records SET status='superseded' WHERE id=?", ("44444444-1111-2222-3333-444444444444",)))
    check("successor without the status rejected",
          not attempt("UPDATE records SET superseded_by=? WHERE id=?",
                      ("55555555-1111-2222-3333-444444444444", "44444444-1111-2222-3333-444444444444")))
    check("both together accepted",
          attempt("UPDATE records SET status='superseded', superseded_by=? WHERE id=?",
                  ("55555555-1111-2222-3333-444444444444", "44444444-1111-2222-3333-444444444444")))

    section("recall only ever sees active, unexpired records")
    con.execute("INSERT INTO records (id,type,scope,content,created_at,expires_at) VALUES "
                "('88888888-1111-2222-3333-444444444444','semantic','proj.a','stale',"
                "'2026-08-08T00:00:00Z','2020-01-01T00:00:00Z')")
    con.commit()
    total = con.execute("SELECT count(*) FROM records WHERE scope='proj.a'").fetchone()[0]
    recallable = con.execute("SELECT count(*) FROM v_recallable WHERE scope='proj.a'").fetchone()[0]
    check("v_recallable hides superseded and expired", recallable == total - 2, f"{recallable} of {total}")

    section("the approval gate is structural")
    check("pending proposal carrying a reviewer rejected",
          not attempt("INSERT INTO proposals (id,scope,kind,candidate,proposed_by,proposed_at,source,state,reviewed_by) "
                      "VALUES ('p0','proj.a','procedural','{}','d','2026-08-08T00:00:00Z','daemon','pending','mike')"))
    check("valid pending proposal accepted",
          attempt("INSERT INTO proposals (id,scope,kind,candidate,proposed_by,proposed_at,source,dedupe_key) "
                  "VALUES ('p1','proj.a','procedural','{\"trigger\":\"x\"}','daemon','2026-08-08T00:00:00Z','daemon','k1')"))
    check("daemon re-proposing the same candidate rejected",
          not attempt("INSERT INTO proposals (id,scope,kind,candidate,proposed_by,proposed_at,source,dedupe_key) "
                      "VALUES ('p2','proj.a','procedural','{\"trigger\":\"x\"}','daemon','2026-08-08T01:00:00Z','daemon','k1')"))
    check("decided proposal without a signature rejected",
          not attempt("INSERT INTO proposals (id,scope,kind,candidate,proposed_by,proposed_at,source,state,reviewed_by,reviewed_at) "
                      "VALUES ('p3','proj.a','procedural','{}','mike','2026-08-08T00:00:00Z','human','approved','mike','2026-08-08T00:00:00Z')"))

    record(UUID_A, "procedural", content="proc")
    proc_sql = ("INSERT INTO procedural_attrs (record_id,trigger_text,steps,approval_state,reviewed_by,"
                "reviewed_at,sig_key_id,sig_payload,sig_value,candidate_sha256) VALUES (?,'t','[{\"n\":1}]',"
                "'approved','mike','2026-08-08T00:00:00Z',?,'payload','sig',?)")
    check("unsigned approved procedure is unrepresentable",
          not attempt("INSERT INTO procedural_attrs (record_id,trigger_text,steps,approval_state,reviewed_by,reviewed_at) "
                      "VALUES (?,'t','[{\"n\":1}]','approved','mike','2026-08-08T00:00:00Z')", (UUID_A,)))
    check("approval_state 'pending' rejected in this table",
          not attempt(proc_sql.replace("'approved'", "'pending'"), (UUID_A, "SHA256:x", "a" * 64)))
    check("malformed sig_key_id rejected", not attempt(proc_sql, (UUID_A, "mike", "a" * 64)))
    check("short candidate hash rejected", not attempt(proc_sql, (UUID_A, "SHA256:x", "abc")))
    check("well-formed signed approval accepted", attempt(proc_sql, (UUID_A, "SHA256:x", "a" * 64)))

    section("cycles and cascades")
    check("closed cycle without closed_at rejected",
          not attempt("INSERT INTO working_set (cycle_id,session_id,scope,goal,status,opened_at,expires_at) "
                      "VALUES ('c9','s1','proj.a','g','closed','2026-08-08T00:00:00Z','2026-08-09T00:00:00Z')"))
    con.execute("DELETE FROM records WHERE id=?", (UUID_A,))
    con.commit()
    check("attrs cascade with the parent record",
          con.execute("SELECT count(*) FROM procedural_attrs WHERE record_id=?", (UUID_A,)).fetchone()[0] == 0)
    return con


# --------------------------------------------------------------------------
# the published approval signature
# --------------------------------------------------------------------------
CANDIDATE_FIELDS = ("trigger", "preconditions", "steps", "success_signal", "failure_signal")


def canonical(candidate: dict) -> bytes:
    """Exactly what candidate_sha256 covers. A verifier that disagrees here
    rejects the reviewer's genuine approvals."""
    return json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def verify_signature(ex):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_ssh_public_key

    section("published approval signature (golden test for signing code)")
    fixture = ex["approval_signature_fixture"]
    sig = ex["procedural"]["record"]["approval"]["signature"]
    pub = load_ssh_public_key(fixture["reviewer_public_key_openssh"].encode())
    payload = sig["signed_payload"].encode()
    raw_sig = base64.b64decode(sig["sig"])

    def verifies(pl, s=raw_sig, key=pub):
        try:
            key.verify(s, pl)
            return True
        except Exception:
            return False

    check("signature verifies against the published public key", verifies(payload))

    record = ex["procedural"]["record"]
    candidate = {k: record[k] for k in CANDIDATE_FIELDS}
    digest = hashlib.sha256(canonical(candidate)).hexdigest()
    check("candidate hash matches the record's actual content", digest == sig["candidate_sha256"],
          f"{digest} != {sig['candidate_sha256']}")
    check("hash inside the signed payload agrees",
          f"candidate_sha256: {sig['candidate_sha256']}" in sig["signed_payload"])

    fields = dict(re.findall(r"^([a-z0-9_]+): (.*)$", sig["signed_payload"], re.M))
    check("payload is versioned", sig["signed_payload"].startswith("memory-agent-approval-v1\n"))
    check("payload carries every binding field",
          set(fields) == {"scope", "proposal", "candidate_sha256", "decision", "reviewer", "nonce", "expires"},
          str(sorted(fields)))

    def mutate(field, value):
        return re.sub(rf"^{field}: .*$", f"{field}: {value}", sig["signed_payload"], flags=re.M).encode()

    for field, value, why in [
        ("candidate_sha256", "b" * 64, "candidate edited after signing"),
        ("decision", "reject", "decision flipped"),
        ("proposal", "p_OTHER", "replayed onto another proposal"),
        ("reviewer", "daemon", "reviewer swapped"),
        ("nonce", "0" * 16, "stale nonce"),
        ("scope", "other.scope", "replayed into another scope"),
    ]:
        check(f"rejects: {why}", not verifies(mutate(field, value)))
    check("rejects: signed by a non-reviewer key",
          not verifies(payload, key=Ed25519PrivateKey.generate().public_key()))
    check("re-verifies offline from the stored payload alone", verifies(sig["signed_payload"].encode()))


# --------------------------------------------------------------------------
# docs
# --------------------------------------------------------------------------
def verify_docs(tools):
    section("spec and docs")
    spec = open(p("docs", "memory-agent-coala-spec.md")).read()
    readme = open(p("README.md")).read()
    backlog = open(p("BACKLOG.md")).read()

    functional = re.findall(r"\*\*(F\d+) —", spec)
    non_functional = re.findall(r"\*\*(NF\d+) —", spec)
    assumptions = re.findall(r"\*\*(A-\d+) —", spec)
    accepts = spec.count("*accept:*")

    check(f"{len(functional)} functional + {len(non_functional)} non-functional requirements, {accepts} accept criteria",
          accepts == len(functional) + len(non_functional))
    check("F numbering contiguous", functional == [f"F{i}" for i in range(1, len(functional) + 1)])
    check("NF numbering contiguous", non_functional == [f"NF{i}" for i in range(1, len(non_functional) + 1)])
    check(f"{len(assumptions)} assumptions each state a consequence", spec.count("*If wrong") + spec.count("*Still open") >= len(assumptions) - 2)

    sections = [int(n) for n in re.findall(r"^## (\d+)\.", spec, re.M)]
    check(f"{len(sections)} sections numbered contiguously", sections == list(range(1, len(sections) + 1)))

    named = set(re.findall(r"`(memory_[a-z_]+)`", spec))
    defined = {t["name"] for t in tools["tools"]}
    check("every tool is documented in the spec", not defined - named, str(defined - named))
    check("the spec invents no tools", not named - defined, str(named - defined))
    for code in (e["code"] for e in tools["errors"]):
        if code not in ("CYCLE_NOT_FOUND", "CYCLE_ALREADY_CLOSED", "SCOPE_REQUIRED", "STORE_BUSY"):
            check(f"error {code} explained in the spec", code in spec)

    for source, base in ((spec, "docs"), (readme, "."), (backlog, ".")):
        for link in sorted(set(re.findall(r"\]\((\.\./[^)]+|[A-Za-z][^):]*\.(?:md|json|sql|yaml))\)", source))):
            check(f"link resolves: {link}", os.path.exists(os.path.normpath(p(base, link))))


def verify_policy():
    import yaml

    section("policy defaults match what the acceptance criteria assume")
    pol = yaml.safe_load(open(p("contracts", "policy.example.yaml")))
    approval = pol["learning"]["approval"]
    for name, ok in (
        ("procedural gate is 'proposal'", pol["learning"]["gates"]["procedural"] == "proposal"),
        ("daemon may not approve", pol["learning"]["daemon_may_approve"] is False),
        ("signatures required", approval["require_signature"] is True),
        ("at least one reviewer key configured", len(approval["reviewers"]) >= 1),
        ("retrieval weights 0.5/0.2/0.3",
         [pol["retrieval"]["weights"][k] for k in ("relevance", "recency", "importance")] == [0.5, 0.2, 0.3]),
        ("recency half-life 72h", pol["retrieval"]["recency_half_life_hours"] == 72),
        ("default recall budget 1500 tokens", pol["retrieval"]["defaults"]["max_tokens"] == 1500),
        ("loop repeat threshold 3", pol["loop_safety"]["repeat_threshold"] == 3),
        ("hard delete needs an explicit call", pol["forgetting"]["hard_delete_requires_explicit_call"] is True),
        ("unattended LLM adjudication off", pol["daemon"]["llm_adjudication"]["enabled"] is False),
        ("record content never logged", pol["observability"]["log_content"] is False),
    ):
        check(name, ok)


def main() -> int:
    print("memory-agent contract verification")
    try:
        registry, by_name, tools = load_registry()
    except ImportError as exc:
        print(f"\nmissing dependency: {exc}\n  pip install jsonschema pyyaml cryptography")
        return 1

    verify_schemas(registry, by_name, tools)
    ex = verify_fixtures(registry, by_name)
    verify_ddl()
    verify_signature(ex)
    verify_docs(tools)
    verify_policy()

    print("\n" + "=" * 62)
    if FAILURES:
        print(f"FAIL — {len(FAILURES)} of {CHECKS} checks failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"PASS — {CHECKS} checks")
    print("\nNote: the vec0 virtual table is commented out in schema.sql because it")
    print("needs the sqlite-vec extension. Everything above runs on stock SQLite.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
