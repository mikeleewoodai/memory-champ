"""The implementation must produce what contracts/ promises.

Acceptance tests prove behaviour. These prove shape: every tool's real output is
validated against the outputSchema published in contracts/mcp-tools.json, and
every record inside a recall response against its record schema. Without this,
the contract is documentation rather than a contract.
"""

import glob
import json
import os
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from memory_agent import approval as A
from memory_agent.server import HANDLERS, _strip_refs, load_contract

ROOT = Path(__file__).resolve().parents[1]
SCOPE = "proj.a"


@pytest.fixture(scope="module")
def registry_and_tools():
    items = []
    for f in sorted(glob.glob(str(ROOT / "contracts" / "schemas" / "*.json"))):
        schema = json.load(open(f, encoding="utf-8"))
        items.append((schema["$id"], Resource.from_contents(schema)))
    contract = load_contract()
    for tool in contract["tools"]:
        for key in ("inputSchema", "outputSchema"):
            if "$id" in tool[key]:
                items.append((tool[key]["$id"], Resource.from_contents(tool[key])))
    return Registry().with_resources(items), {t["name"]: t for t in contract["tools"]}


def validate(registry, schema, instance, label):
    errors = sorted(Draft202012Validator(schema, registry=registry).iter_errors(instance),
                    key=lambda e: list(e.path))
    assert not errors, (
        f"{label} does not match its published schema:\n  "
        + "\n  ".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors[:5]))


def test_every_contract_tool_has_a_handler():
    contract = load_contract()
    assert {t["name"] for t in contract["tools"]} == set(HANDLERS)


def test_advertised_input_schemas_are_self_contained():
    """MCP clients cannot fetch sibling files, so what we advertise must not
    contain a $ref that would dangle on their side."""
    for tool in load_contract()["tools"]:
        blob = json.dumps(_strip_refs(tool["inputSchema"]))
        assert "$ref" not in blob, f"{tool['name']} advertises an unresolvable $ref"


def test_recall_output_conforms(svc, registry_and_tools, a_procedure, approve):
    registry, tools = registry_and_tools
    svc.remember(scope=SCOPE, type="semantic", content="Acme wants invoices as PDF.",
                 semantic={"subject": "acme", "predicate": "invoice_format", "object": "PDF"})
    oc = svc.open_cycle(scope=SCOPE, session_id="run-1", goal="g", preload={"enabled": False})
    svc.remember(scope=SCOPE, type="episodic", content="Sent the invoice as Word.",
                 episodic={"session_id": "run-1", "cycle_id": oc["cycle_id"], "step_no": 0,
                           "outcome": "failure"})
    approve(a_procedure())

    out = svc.recall(scope=SCOPE, query="invoice PDF Word Acme",
                     types=["episodic", "semantic", "procedural"], k=10)

    assert len(out["records"]) == 3, "expected one record of each type in this fixture"
    validate(registry, tools["memory_recall"]["outputSchema"], out, "memory_recall output")


@pytest.mark.parametrize("rtype", ["episodic", "semantic", "procedural"])
def test_each_record_type_conforms_to_its_own_schema(svc, registry_and_tools, rtype,
                                                     a_procedure, approve):
    registry, _ = registry_and_tools
    schema_id = (f"https://mikeleewoodai.github.io/memory-champ/v1/"
                 f"{rtype}-record.schema.json")
    schema = registry.get_or_retrieve(schema_id).value.contents

    if rtype == "semantic":
        svc.remember(scope=SCOPE, type="semantic", content="A fact worth keeping.")
    elif rtype == "episodic":
        oc = svc.open_cycle(scope=SCOPE, session_id="s", goal="g", preload={"enabled": False})
        svc.remember(scope=SCOPE, type="episodic", content="Something happened.",
                     episodic={"session_id": "s", "cycle_id": oc["cycle_id"], "step_no": 0,
                               "outcome": "success"})
    else:
        approve(a_procedure())

    out = svc.recall(scope=SCOPE, query="fact happened invoice", types=[rtype], k=5)
    assert out["records"], f"no {rtype} record came back"
    for entry in out["records"]:
        validate(registry, schema, entry["record"], f"{rtype} record from recall")


def test_approved_procedure_carries_a_verifiable_signature_in_recall(svc, a_procedure,
                                                                     approve, reviewer):
    """A host can check the signature on a procedure before following it, without
    trusting the server that handed it over."""
    priv, _ = reviewer
    approve(a_procedure())
    out = svc.recall(scope=SCOPE, query="invoice PDF", types=["procedural"], k=5)

    sig = out["records"][0]["record"]["approval"]["signature"]
    pub = priv.public_key()
    A.Verifier([A.ReviewerKey("mike", pub, A.fingerprint(pub))]).verify_bytes(
        sig["key_id"], sig["signed_payload"], sig["sig"])


def test_open_and_close_cycle_output_conforms(svc, registry_and_tools):
    registry, tools = registry_and_tools
    svc.remember(scope=SCOPE, type="semantic", content="Preloadable context about invoices.")

    opened = svc.open_cycle(scope=SCOPE, session_id="run-1", goal="send the invoice")
    validate(registry, tools["memory_open_cycle"]["outputSchema"], opened, "open_cycle output")

    closed = svc.close_cycle(cycle_id=opened["cycle_id"], outcome="success", summary="Done.")
    validate(registry, tools["memory_close_cycle"]["outputSchema"], closed, "close_cycle output")


def test_remember_output_conforms(svc, registry_and_tools):
    registry, tools = registry_and_tools
    out = svc.remember(scope=SCOPE, type="semantic", content="Acme wants PDF invoices.",
                       semantic={"subject": "acme", "predicate": "fmt", "object": "PDF"})
    validate(registry, tools["memory_remember"]["outputSchema"], out, "remember output")


def test_forget_output_conforms(svc, registry_and_tools):
    registry, tools = registry_and_tools
    rid = svc.remember(scope=SCOPE, type="semantic", content="Forget me.")["record_id"]
    for kwargs in ({"dry_run": True}, {"mode": "tombstone"}):
        out = svc.forget(scope=SCOPE, selector={"record_ids": [rid]}, reason="test",
                         max_records=5, **kwargs)
        validate(registry, tools["memory_forget"]["outputSchema"], out, "forget output")


def test_reflect_output_conforms(svc, registry_and_tools):
    registry, tools = registry_and_tools
    oc = svc.open_cycle(scope=SCOPE, session_id="run-1", goal="g", preload={"enabled": False})
    for i in range(4):
        svc.remember(scope=SCOPE, type="episodic", content=f"step {i}",
                     episodic={"session_id": "run-1", "cycle_id": oc["cycle_id"],
                               "step_no": i, "outcome": "success"})
    out = svc.reflect(scope=SCOPE, modes=["consolidate", "contradictions", "promote", "importance"])
    validate(registry, tools["memory_reflect"]["outputSchema"], out, "reflect output")


def test_propose_and_review_output_conforms(svc, registry_and_tools, a_procedure, reviewer):
    registry, tools = registry_and_tools
    priv, key_id = reviewer

    proposed = svc.propose_procedure(
        scope=SCOPE, content="Invoicing Acme", trigger="Sending any invoice to Acme",
        steps=[{"n": 1, "instruction": "Export as PDF."}], rationale="because",
        proposed_by="crm-builder")
    validate(registry, tools["memory_propose_procedure"]["outputSchema"], proposed,
             "propose_procedure output")

    listed = svc.review_proposals(action="list", scope=SCOPE)
    validate(registry, tools["memory_review_proposals"]["outputSchema"], listed, "review list output")

    entry = listed["proposals"][0]
    payload = A.build_payload(
        scope=SCOPE, proposal_id=entry["proposal_id"],
        candidate_sha256=entry["candidate_sha256"], decision="approve", reviewer="mike",
        nonce=entry["nonce"], expires=entry["challenge_expires_at"])
    approved = svc.review_proposals(
        action="approve", proposal_ids=[entry["proposal_id"]], reviewed_by="mike",
        signatures=[{"proposal_id": entry["proposal_id"], "alg": "ed25519", "key_id": key_id,
                     "signed_payload": payload, "sig": A.sign_payload(priv, payload)}])
    validate(registry, tools["memory_review_proposals"]["outputSchema"], approved,
             "review approve output")

    # and a failure path, which is where output schemas usually rot
    rejected = svc.review_proposals(
        action="approve", proposal_ids=[entry["proposal_id"]], reviewed_by="mike",
        signatures=[{"proposal_id": entry["proposal_id"], "alg": "ed25519", "key_id": key_id,
                     "signed_payload": payload, "sig": A.sign_payload(priv, payload)}])
    validate(registry, tools["memory_review_proposals"]["outputSchema"], rejected,
             "review skipped output")


def test_stats_output_conforms(svc, registry_and_tools):
    registry, tools = registry_and_tools
    svc.remember(scope=SCOPE, type="semantic", content="Something to count.")
    out = svc.stats(scope=SCOPE, include_scope_breakdown=True, include_top_accessed=3)
    validate(registry, tools["memory_stats"]["outputSchema"], out, "stats output")


def test_published_fixture_still_matches_the_implementation():
    """contracts/examples/records.json is the golden test for signing code. If the
    implementation's canonical forms drift, this catches it."""
    ex = json.load(open(ROOT / "contracts" / "examples" / "records.json", encoding="utf-8"))
    fixture, sig = ex["approval_signature_fixture"], ex["procedural"]["record"]["approval"]["signature"]

    pub = A.load_public_key(fixture["reviewer_public_key_openssh"])
    assert A.fingerprint(pub) == fixture["key_id"]
    A.Verifier([A.ReviewerKey("mike", pub, fixture["key_id"])]).verify_bytes(
        sig["key_id"], sig["signed_payload"], sig["sig"])
    assert A.candidate_hash(ex["procedural"]["record"]) == sig["candidate_sha256"]
    assert set(A.parse_payload(sig["signed_payload"])) == set(A.PAYLOAD_FIELDS)


def test_constrained_values_agree_across_python_ddl_and_contract():
    """One list, three homes, previously checked in none.

    ACTION_CLASSES and OUTCOMES exist in Python (to refuse a bad value with a
    typed error), in schema.sql (as a CHECK), and in mcp-tools.json (as an
    enum). Nothing compared them, so they could drift until a caller was told
    one set and constrained by another.
    """
    import re

    from memory_agent.service import ACTION_CLASSES, OUTCOMES

    ddl = (ROOT / "contracts" / "db" / "schema.sql").read_text(encoding="utf-8")
    contract = load_contract()

    def ddl_check(column):
        m = re.search(rf"{column}\s+TEXT.*?IN \((.*?)\)", ddl, re.S)
        assert m, f"no CHECK found for {column}"
        return set(re.findall(r"'([^']+)'", m.group(1)))

    def contract_enum(tool, *path):
        node = [t for t in contract["tools"] if t["name"] == tool][0]["inputSchema"]
        for step in path:
            node = node["properties"][step]
        return set(node["enum"])

    assert set(ACTION_CLASSES) == ddl_check("action_class"), "python vs DDL"
    assert set(ACTION_CLASSES) == contract_enum(
        "memory_remember", "episodic", "action", "class"), "python vs contract"

    assert set(OUTCOMES) == ddl_check("outcome"), "python vs DDL"
    assert set(OUTCOMES) == contract_enum("memory_remember", "episodic", "outcome")


def test_every_enum_tells_a_caller_its_allowed_values():
    """A bare `enum` is invisible to the model calling the tool.

    Clients normalise a property with no `type` and no `description` down to
    `{}`, so the allowed values can only be discovered by sending a wrong one
    and reading the error. Descriptions survive that normalisation; enums do
    not. Every constrained field must therefore say what it accepts in prose.
    """
    contract = load_contract()
    bare = []
    # Conditional subschemas are matching logic, not documentation. An enum
    # inside an `if` says which instances the branch applies to; nobody reads it
    # to learn what to send, and describing it would be noise.
    CONDITIONAL = {"allOf", "anyOf", "oneOf", "if", "then", "else", "not"}

    def walk(node, path, conditional=False):
        if isinstance(node, dict):
            if "enum" in node and not node.get("description") and not conditional:
                bare.append(path)
            for k, v in node.items():
                walk(v, f"{path}.{k}", conditional or k in CONDITIONAL)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]", conditional)

    for tool in contract["tools"]:
        walk(tool["inputSchema"], tool["name"])

    assert not bare, (
        "enum properties with no description, invisible to a calling model: "
        + ", ".join(p.replace(".properties.", ".") for p in bare))
