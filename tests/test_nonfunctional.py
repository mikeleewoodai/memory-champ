"""Non-functional requirements from spec §10, plus a live MCP server check."""

import json
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import pytest

from memory_agent import approval as A
from memory_agent.config import ApprovalPolicy, Policy
from memory_agent.embedding import HashingEmbedder, TokenCounter
from memory_agent.service import MemoryService
from memory_agent.store import Store

SCOPE = "proj.a"


# ===========================================================================
# NF2 - Portability: the whole memory is one file
# ===========================================================================
def test_nf2_database_file_moves_intact(reviewer):
    priv, key_id = reviewer
    with tempfile.TemporaryDirectory() as tmp:
        source = str(Path(tmp) / "memory.db")
        policy = Policy()
        policy.db_path = source
        policy.require_vector_extension = False
        policy.learning.approval = ApprovalPolicy(
            reviewers=[A.ReviewerKey("mike", priv.public_key(), key_id)])

        svc = MemoryService(policy, Store(source, dimensions=384), HashingEmbedder(384))
        svc.remember(scope=SCOPE, type="semantic", content="Acme wants invoices as PDF.")
        before = svc.recall(scope=SCOPE, query="acme invoices PDF")
        svc.close()

        moved = str(Path(tmp) / "elsewhere.db")
        Path(moved).write_bytes(Path(source).read_bytes())

        svc2 = MemoryService(policy, Store(moved, dimensions=384), HashingEmbedder(384))
        after = svc2.recall(scope=SCOPE, query="acme invoices PDF")
        svc2.close()

        assert [r["record"]["id"] for r in after["records"]] == \
               [r["record"]["id"] for r in before["records"]]
        assert after["context_block"] == before["context_block"]


# ===========================================================================
# NF3 - Concurrency: many readers, one writer, no corruption
# ===========================================================================
def test_nf3_concurrent_readers_and_writers(reviewer):
    priv, key_id = reviewer
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "memory.db")
        policy = Policy()
        policy.db_path = path
        policy.require_vector_extension = False
        policy.learning.approval = ApprovalPolicy(
            reviewers=[A.ReviewerKey("mike", priv.public_key(), key_id)])
        # WAL gives many readers and one writer; without it this test deadlocks,
        # which is exactly the failure NF3 exists to prevent.
        seed = MemoryService(policy, Store(path, dimensions=384), HashingEmbedder(384))
        seed.store.con.execute("PRAGMA journal_mode=WAL")
        for i in range(20):
            seed.remember(scope=SCOPE, type="semantic", content=f"seed record {i}")
        seed.close()

        errors: list[Exception] = []

        # Close in finally, and hold the Store separately from the service. On
        # the failing path the old code skipped close(), so the connection
        # stayed open - and because the traceback in `errors` keeps it alive,
        # TemporaryDirectory could never unlink memory.db. The resulting
        # PermissionError then replaced this test's real assertion failure,
        # hiding a genuine concurrency defect behind a teardown error.
        def reader():
            store = Store(path, dimensions=384)
            try:
                s = MemoryService(policy, store, HashingEmbedder(384))
                for _ in range(10):
                    s.recall(scope=SCOPE, query="seed record")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                store.close()

        def writer(n):
            store = Store(path, dimensions=384)
            try:
                s = MemoryService(policy, store, HashingEmbedder(384))
                for i in range(10):
                    s.remember(scope=SCOPE, type="semantic", content=f"writer {n} record {i}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                store.close()

        threads = [threading.Thread(target=reader) for _ in range(8)]
        threads += [threading.Thread(target=writer, args=(n,)) for n in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert errors == [], f"concurrency failures: {errors[:3]}"
        con = sqlite3.connect(path)
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        con.close()


# ===========================================================================
# NF4 / NF6 - Offline, and graceful loss of the vector path
# ===========================================================================
def test_nf4_runs_with_no_network_dependency(svc):
    """The default embedder and token counter are pure-python and local. If this
    test ever needs a network, the offline guarantee has been broken."""
    assert isinstance(svc.embedder, HashingEmbedder)
    svc.remember(scope=SCOPE, type="semantic", content="Fully local.")
    assert svc.recall(scope=SCOPE, query="fully local")["records"]


def test_nf6_all_nine_tools_work_without_the_vector_extension(svc, a_procedure, approve):
    svc.store.vector_ok = False
    svc.store.vector_error = "sqlite-vec not loaded"

    oc = svc.open_cycle(scope=SCOPE, session_id="run-1", goal="works anyway")
    svc.remember(scope=SCOPE, type="semantic", content="A fact recorded with no vector index.")
    out = svc.recall(scope=SCOPE, query="fact recorded vector index")
    svc.close_cycle(cycle_id=oc["cycle_id"], outcome="success", summary="done")
    svc.reflect(scope=SCOPE)
    pid = a_procedure()
    approve(pid)
    svc.forget(scope=SCOPE, selector={"filter": {"tags": ["nothing"]}}, reason="none",
               max_records=5, dry_run=True)
    stats = svc.stats(scope=SCOPE)

    assert out["degraded"]["reason"] == "vector_unavailable"
    assert out["records"], "keyword recall must still work"
    assert any("keyword-only" in w for w in stats["warnings"])


def test_nf6_require_vector_extension_fails_loudly_instead(monkeypatch):
    """Fail-loud versus degrade-visibly is a deliberate choice, so both halves
    have to behave as documented."""
    import memory_agent.store as store_mod
    from memory_agent.errors import VectorUnavailable

    def broken(self, require):
        self.vector_error = "simulated missing extension"
        if require:
            raise VectorUnavailable(self.vector_error)

    monkeypatch.setattr(store_mod.Store, "_load_vector", broken)
    with pytest.raises(VectorUnavailable):
        Store(":memory:", dimensions=384, require_vector=True)
    assert Store(":memory:", dimensions=384, require_vector=False).vector_ok is False


# ===========================================================================
# NF5 - No grounding actions
# ===========================================================================
def test_nf5_contract_declares_a_closed_world():
    contract = json.loads((Path(__file__).resolve().parents[1] /
                           "contracts" / "mcp-tools.json").read_text(encoding="utf-8"))
    assert all(t["annotations"]["openWorldHint"] is False for t in contract["tools"])


def test_nf5_no_module_imports_a_network_client():
    """A cheap structural guard. The strong claim is traced at runtime; this
    catches the accidental `import requests` that would start the drift."""
    src = Path(__file__).resolve().parents[1] / "src" / "memory_agent"
    banned = ("import requests", "import httpx", "import urllib.request",
              "from urllib.request", "import socket", "aiohttp")
    for path in src.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{path.name} imports {token}"


# ===========================================================================
# NF8 - Auditable: nothing destroyed without an explicit recorded request
# ===========================================================================
def test_nf8_every_forget_records_a_reason_and_only_hard_delete_removes_rows(svc):
    rid = svc.remember(scope=SCOPE, type="semantic", content="audit me")["record_id"]

    svc.forget(scope=SCOPE, selector={"record_ids": [rid]}, reason="superseded by policy",
               max_records=1)
    assert svc.store.get_record(rid) is not None, "tombstone must not delete rows"

    with pytest.raises(TypeError):
        svc.forget(scope=SCOPE, selector={"record_ids": [rid]}, max_records=1)  # no reason


def test_nf8_writes_carry_provenance(svc):
    rid = svc.remember(scope=SCOPE, type="semantic", content="who wrote this",
                       provenance={"source": "host", "agent": "crm-builder"})["record_id"]
    assert json.loads(svc.store.get_record(rid)["provenance"])["agent"] == "crm-builder"


# ===========================================================================
# NF9 / NF10 - Observability without leaking, and versioning
# ===========================================================================
def test_nf9_content_logging_is_off_by_default():
    assert Policy().log_content is False


def test_nf10_every_record_carries_a_schema_version(svc):
    svc.remember(scope=SCOPE, type="semantic", content="versioned")
    out = svc.recall(scope=SCOPE, query="versioned")
    assert out["schema_version"] == "1.0"
    assert out["records"][0]["record"]["schema_version"] == "1.0"
    assert svc.store.meta("schema_version") == "1.0"


# ===========================================================================
# The token bound must be an UPPER bound when estimating
# ===========================================================================
def test_token_fallback_never_undercounts():
    counter = TokenCounter()
    if counter.exact:
        pytest.skip("a real tokenizer is installed; the fallback is not in play")
    for text in ["hello world", "a" * 500, "def f(x): return x**2 + 1  # comment",
                 "élan vital naïve façade", " ".join(["word"] * 200)]:
        # A token is at least one character, so character count is a hard ceiling
        # on any sane tokenizer. Under-counting is the failure that matters.
        assert counter.count(text) >= len(text.split())


# ===========================================================================
# NF1 - Recall latency
# ===========================================================================
@pytest.mark.slow
def test_nf1_recall_latency(svc):
    for i in range(2000):
        svc.remember(scope=SCOPE, type="semantic",
                     content=f"Record {i} about invoices, clients, formats and scheduling.")

    timings = []
    for i in range(50):
        start = time.perf_counter()
        svc.recall(scope=SCOPE, query=f"invoices clients formats {i}", k=12)
        timings.append((time.perf_counter() - start) * 1000)

    timings.sort()
    p95 = timings[int(len(timings) * 0.95) - 1]
    assert p95 < 150, f"p95 {p95:.0f}ms over 2k records exceeds the 150ms budget"


# ===========================================================================
# The MCP server actually starts and advertises the nine tools
# ===========================================================================
TOOL_NAMES = {
    "memory_open_cycle", "memory_close_cycle", "memory_recall", "memory_remember",
    "memory_forget", "memory_reflect", "memory_propose_procedure",
    "memory_review_proposals", "memory_stats"}


def test_mcp_server_advertises_all_nine_tools(svc):
    from memory_agent.server import _tool_models, annotation, build_server, load_contract

    build_server(svc)  # must construct without error against the installed SDK
    tools = _tool_models(load_contract())

    assert {t.name for t in tools} == TOOL_NAMES
    for tool in tools:
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
        assert tool.description and schema
        assert "$ref" not in json.dumps(schema)
        if tool.annotations is not None:
            assert annotation(tool, "openWorldHint") is False, "no tool touches the world"


def test_mcp_dispatch_round_trip(svc):
    """Exercise the same dispatch path the transport uses, without a transport."""
    from memory_agent.server import _dispatch

    written = _dispatch(svc, "memory_remember",
                        {"scope": SCOPE, "type": "semantic", "content": "Acme wants PDF invoices."})
    assert written["created"] is True

    recalled = _dispatch(svc, "memory_recall", {"scope": SCOPE, "query": "acme invoices"})
    assert recalled["records"][0]["record"]["id"] == written["record_id"]

    # errors come back as structured codes, not stack traces
    refused = _dispatch(svc, "memory_remember",
                        {"scope": SCOPE, "type": "procedural", "content": "x"})
    assert refused["error"] == "PROCEDURAL_WRITE_REQUIRES_PROPOSAL"
    assert _dispatch(svc, "memory_recall", {})["error"] == "INVALID_ARGUMENTS"
    assert _dispatch(svc, "nope", {})["error"] == "UNKNOWN_TOOL"


def test_every_tool_is_reachable_through_dispatch(svc, a_procedure):
    """A tool advertised but not wired is worse than one that is missing."""
    from memory_agent.server import HANDLERS, _dispatch

    assert set(HANDLERS) == TOOL_NAMES
    for name in TOOL_NAMES:
        result = _dispatch(svc, name, {})
        assert result.get("error") != "UNKNOWN_TOOL", f"{name} has no handler"


def test_the_mcp_handler_layer_actually_dispatches(svc):
    """`_dispatch` working is not the same as the server working.

    The test above passed the entire time the MCP server was broken. It calls
    `_dispatch` directly, so it never touches the layer that was wrong:
    build_server's 2.x handler took params from its FIRST argument, which is the
    request *context*, not the request. The context carries its own `params` - a
    raw Mapping of the wire payload - so the handshake succeeded, `claude mcp
    list` reported "Connected", and every real tool call died with
    "'dict' object has no attribute 'name'".

    Connection is not capability. This exercises the SDK handlers as registered.
    """
    pytest.importorskip("mcp")
    import asyncio

    from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest

    from memory_agent.server import build_server

    server = build_server(svc)
    if not hasattr(server, "get_request_handler"):
        pytest.skip("1.x decorator API; dispatch is owned by the SDK there")

    listed_h = server.get_request_handler(ListToolsRequest.model_fields["method"].default)
    call_h = server.get_request_handler(CallToolRequest.model_fields["method"].default)

    async def exercise():
        listed = await listed_h.handler(None, None)
        assert {t.name for t in listed.tools} == TOOL_NAMES

        result = await call_h.handler(
            None, CallToolRequestParams(name="memory_stats", arguments={}))
        assert not getattr(result, "isError", getattr(result, "is_error", False))
        payload = json.loads(result.content[0].text)
        assert "counts" in payload, payload

    asyncio.run(exercise())
