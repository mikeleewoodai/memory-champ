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
    """A cheap structural guard, and NECESSARY BUT NOT SUFFICIENT.

    It reads our own modules, so it can only see our own imports. It stayed
    green while `SentenceTransformer(name)` contacted Hugging Face on every
    construction, because that call lives two layers down in a dependency.
    `test_nf5_the_registered_server_makes_no_outbound_connection` is the one
    that actually tests the claim; this catches the accidental `import requests`
    that would start the drift.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "memory_agent"
    banned = ("import requests", "import httpx", "import urllib.request",
              "from urllib.request", "import socket", "aiohttp")
    for path in src.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in text, f"{path.name} imports {token}"


# Installed as sitecustomize.py ahead of src/ on the spawned server's PYTHONPATH,
# so it is in place before the server imports anything that could open a socket.
_NETWORK_TRACE_HOOK = r'''
import ipaddress
import json
import os
import socket
import sys
import threading

_TRACE = os.environ["NF5_TRACE"]
_AF_UNIX = getattr(socket, "AF_UNIX", object())
_lock = threading.Lock()


def _write(entry):
    with _lock, open(_TRACE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _host(address):
    return address[0] if isinstance(address, tuple) and address else address


def _local(host, family=None):
    # None asks for a passive (bind) lookup and an AF_UNIX address is a path;
    # neither leaves the machine. Loopback must pass: on Windows asyncio's
    # self-pipe is a socketpair built by connecting to 127.0.0.1.
    if host is None or family == _AF_UNIX:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


def _refuse(call, target):
    # Recorded before raising: the verdict is this file, never whether the
    # OSError makes it back out of library code that swallows exceptions.
    _write({"refused": call, "target": repr(target)})
    raise OSError(f"network refused by the NF5 trace: {call} {target!r}")


_create_connection = socket.create_connection


def _create_connection_traced(address, *args, **kwargs):
    if not _local(_host(address)):
        _refuse("socket.create_connection", address)
    return _create_connection(address, *args, **kwargs)


socket.create_connection = _create_connection_traced


def _trace_connect(name):
    original = getattr(socket.socket, name)

    def traced(self, address):
        if not _local(_host(address), self.family):
            _refuse(f"socket.socket.{name}", address)
        return original(self, address)

    setattr(socket.socket, name, traced)


_trace_connect("connect")
_trace_connect("connect_ex")

_getaddrinfo = socket.getaddrinfo


def _getaddrinfo_traced(host, *args, **kwargs):
    if not _local(host):
        _refuse("socket.getaddrinfo", (host, *args[:1]))
    return _getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = _getaddrinfo_traced

# Backstop for callers that bypass the names above: `_socket` used directly, or a
# resolver that is not wrapped. CPython raises these events from C. A wrapped
# call is refused before it gets there, so nothing is recorded twice.
_RESOLVERS = {"socket.getaddrinfo", "socket.gethostbyname",
              "socket.gethostbyaddr", "socket.getnameinfo"}


def _audit(event, args):
    if event in ("socket.connect", "socket.sendto"):
        if not _local(_host(args[1]), args[0].family):
            _refuse(f"audit {event}", args[1])
    elif event in _RESOLVERS and not _local(_host(args[0])):
        _refuse(f"audit {event}", args[0])


sys.addaudithook(_audit)
_write({"loaded": os.getpid()})
'''


@pytest.mark.slow
def test_nf5_the_registered_server_makes_no_outbound_connection(tmp_path):
    """NF5, traced in a real server process rather than inside pytest.

    The NF4 test proves one call stays off the network - the model load - with
    sockets patched in the test process. Everything else a real start does goes
    unwatched there: the interpreter and module `init` registers, the MCP SDK
    and its transport, the handshake, the background build, a tool call. So this
    spawns exactly the command `init` prints, with a sitecustomize hook in place
    before the server imports anything, and huggingface_hub's registry fetch
    forced the way the NF4 test forces it (HANDOVER item 11). The hook records
    every non-loopback connect and DNS lookup and refuses it with the OSError an
    offline machine raises; the verdict is the record.

    An empty record means nothing unless three things hold, so each is asserted
    first: the hook loaded; the server logged the real model as its embedder - a
    failed load falls back to hashing without a word, and a server that never
    loads the model never reaches for the Hub; and the tool call succeeded.

    Blind spots: native code opening its own sockets, and an asyncio proactor
    connect to an IP literal, which raises no audit event. The file-handle half
    of NF5 is not traced. Spec §13.
    """
    import importlib.util
    import os
    import queue
    import subprocess
    import sys

    if importlib.util.find_spec("sentence_transformers") is None:
        pytest.skip("sentence-transformers is not installed; no model load to trace")
    pytest.importorskip("mcp")
    from huggingface_hub import constants
    from mcp.types import LATEST_PROTOCOL_VERSION

    model_cache = constants.HF_HUB_CACHE
    if not any(Path(model_cache).glob("models--sentence-transformers--all-MiniLM-L6-v2")):
        pytest.skip("all-MiniLM-L6-v2 is not cached; a first run may download")

    src = Path(__file__).resolve().parents[1] / "src"
    home = tmp_path / "home"
    # Inherited settings could make the trace vacuous (HF_HUB_OFFLINE=1), hide the
    # ready line (MEMORY_AGENT_LOG), or aim the server at the real store.
    inherited = ("HF_", "HUGGINGFACE_", "TRANSFORMERS_", "SENTENCE_TRANSFORMERS_", "MEMORY_AGENT_")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(inherited)}
    env.update(MEMORY_AGENT_HOME=str(home), PYTHONPATH=str(src))

    def run_python(code):
        done = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp_path,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
        assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
        return done.stdout.decode("utf-8", "replace")

    printed = run_python("import sys; from memory_agent.cli import main; "
                         "sys.exit(main(['init', '--id', 'e2e', '--no-passphrase']))")
    (registered,) = json.loads(printed[printed.index("\n{") + 1:])["mcpServers"].values()

    db_path, provider, model = json.loads(run_python(
        "import json; from memory_agent.config import Policy; p = Policy.load(); "
        "print(json.dumps([p.db_path, p.embedding_provider, p.embedding_model]))"))
    assert Path(db_path).resolve().is_relative_to(home.resolve()), \
        f"the server would open {db_path}, outside this test's home"
    assert (provider, model) == ("sentence-transformers", "all-MiniLM-L6-v2")

    hf_home = tmp_path / "hf-home"
    hf_home.mkdir()
    registry = hf_home / ".agent_harnesses.json"
    registry.write_text(json.dumps({"standardEnvVars": [], "harnesses": {}}), encoding="utf-8")
    stale = time.time() - 25 * 3600  # huggingface_hub refetches past 24h
    os.utime(registry, (stale, stale))

    hook_dir = tmp_path / "hook"
    hook_dir.mkdir()
    (hook_dir / "sitecustomize.py").write_text(_NETWORK_TRACE_HOOK, encoding="utf-8")
    trace = tmp_path / "network-trace.jsonl"
    stderr_log = tmp_path / "server-stderr.log"
    server_env = {**env, "HF_HOME": str(hf_home), "HF_HUB_CACHE": model_cache,
                  "NF5_TRACE": str(trace),
                  "PYTHONPATH": os.pathsep.join([str(hook_dir), str(src)])}

    with open(stderr_log, "wb") as stderr:
        proc = subprocess.Popen([registered["command"], *registered["args"]], env=server_env,
                                cwd=tmp_path, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=stderr)
    replies: queue.Queue = queue.Queue()

    def pump():
        for line in proc.stdout:
            replies.put(line)
        replies.put(None)  # stdout closed: the server has exited

    pumping = threading.Thread(target=pump, daemon=True)
    pumping.start()

    def server_log():
        return stderr_log.read_text(encoding="utf-8", errors="replace")

    def send(message):
        proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        proc.stdin.flush()

    def reply(request_id, timeout):
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = replies.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                line = None
            if line is None:
                pytest.fail(f"no reply to request {request_id} (exit code {proc.poll()})\n"
                            f"{server_log()[-3000:]}")
            message = json.loads(line)
            if message.get("id") == request_id:
                return message

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": LATEST_PROTOCOL_VERSION, "capabilities": {},
                         "clientInfo": {"name": "nf5-trace", "version": "1"}}})
        assert "result" in reply(1, 60)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "memory_stats", "arguments": {}}})
        called = reply(2, 300)  # answered after the build (NF11), so the load is traced too
        proc.stdin.close()
        proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        pumping.join(10)
        proc.stdout.close()

    records = ([json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
               if trace.exists() else [])
    assert any("loaded" in r for r in records), \
        f"the trace hook never loaded, so an empty record proves nothing\n{server_log()[-3000:]}"

    ready = [line for line in server_log().splitlines() if "memory-agent ready" in line]
    assert ready and "embedder=all-MiniLM-L6-v2" in ready[-1], (
        "the server never loaded the real model, so it had no cause to reach the Hub\n"
        f"{server_log()[-3000:]}")

    assert "result" in called, called
    assert not called["result"].get("isError"), called
    assert "counts" in json.loads(called["result"]["content"][0]["text"]), called

    attempts = [r for r in records if "refused" in r]
    assert attempts == [], (
        f"the registered server reached for the network: {attempts}. NF5 is broken; for "
        "huggingface.co, see the offline switch in SentenceTransformerEmbedder")


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


def test_nf11_tools_answer_before_the_service_has_loaded(svc):
    """NF11. The handshake must not wait for the embedder; tool calls must.

    `_run` used to build MemoryService - the torch import and the model load -
    before opening stdio, so `initialize` went unanswered for 10-30s and Claude
    Code dropped the server at its 30s connect limit on about a third of starts.
    Crosses the same seam as the test above: the SDK handlers as registered, over
    a service that does not exist yet.
    """
    pytest.importorskip("mcp")
    import asyncio

    from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest

    from memory_agent.server import _LoadingService, build_server

    release = threading.Event()

    def slow_build():
        assert release.wait(10), "the test never released the build"
        return svc

    loading = _LoadingService(slow_build)
    server = build_server(loading)
    if not hasattr(server, "get_request_handler"):
        pytest.skip("1.x decorator API; dispatch is owned by the SDK there")
    loading.start()

    listed_h = server.get_request_handler(ListToolsRequest.model_fields["method"].default)
    call_h = server.get_request_handler(CallToolRequest.model_fields["method"].default)

    async def exercise():
        listed = await asyncio.wait_for(listed_h.handler(None, None), 1)
        assert {t.name for t in listed.tools} == TOOL_NAMES

        call = asyncio.ensure_future(
            call_h.handler(None, CallToolRequestParams(name="memory_stats", arguments={})))
        await asyncio.sleep(0.2)
        assert not call.done(), "a call made mid-load must wait for the service, not fail"

        release.set()
        result = await asyncio.wait_for(call, 10)
        assert not getattr(result, "isError", getattr(result, "is_error", False))
        assert "counts" in json.loads(result.content[0].text)

    try:
        asyncio.run(exercise())
    finally:
        release.set()  # never leave the loader blocked if an assertion fired first


def test_a_service_that_fails_to_load_says_so_on_every_call():
    """A build failure must neither hang callers nor be blamed on their arguments.

    `_dispatch` maps TypeError to INVALID_ARGUMENTS, so a startup TypeError
    re-raised as-is would tell the caller its arguments were wrong when the
    server never started.
    """
    from memory_agent.server import _dispatch, _LoadingService

    def broken_build():
        raise TypeError("store would not open")

    loading = _LoadingService(broken_build)
    loading.start()
    for _ in range(2):  # the failure is sticky, not a one-shot
        result = _dispatch(loading, "memory_stats", {})
        assert result["error"] == "INTERNAL"
        assert "failed to start" in result["message"]


def test_nf4_real_embedder_loads_with_the_network_blocked(monkeypatch, tmp_path):
    """The README's first claim is "It never reaches the network." Test it.

    The structural guard above greps our own imports and could never have seen
    the violation: `SentenceTransformer(name)` revalidated an already-cached
    model against Hugging Face on every construction, so every CLI command and
    every server start made an outbound request.

    `local_files_only` closed that and left a second leak, which the first
    version of this test missed twice over. `hf_hub_download` builds its headers
    before it checks the flag, and building the user-agent fetches
    huggingface_hub's agent-harness registry whenever its copy on disk is
    missing or a day old. So the request came and went with the mtime of a
    shared cache file, usually fresh on a test run. And on a run where it did
    fire, the AssertionError raised to block it landed in two `except Exception`
    blocks inside huggingface_hub, so a blocked request read as a pass.

    Hence: force the condition instead of hoping for it, and RECORD every
    attempt. Each is then refused with the OSError a machine with no network
    raises, so nothing leaves the host, but the verdict is the record - never
    whether an exception made it back out of library code.

    A genuine first run is still allowed to download - refusing would be worse
    than a slow start - so this skips when nothing is cached.
    """
    pytest.importorskip("sentence_transformers")
    from huggingface_hub import constants
    from huggingface_hub.utils import _detect_agent

    if not any(Path(constants.HF_HUB_CACHE).glob("models--sentence-transformers--all-MiniLM-L6-v2")):
        pytest.skip("all-MiniLM-L6-v2 is not cached; a first run may download")

    # The registry fetch fires only when all three hold. setattr raises on a
    # missing name, so if huggingface_hub moves them this fails loudly rather
    # than leaving a test whose trigger can no longer fire.
    monkeypatch.setattr(_detect_agent, "_registry", None)  # nothing resolved in-process
    monkeypatch.setattr(constants, "AGENT_HARNESSES_PATH",
                        str(tmp_path / ".agent_harnesses.json"))  # no copy on disk
    monkeypatch.setattr(constants, "HF_HUB_OFFLINE", False)  # not already offline

    import socket

    attempts: list[str] = []

    def record(name):
        def refuse(*args, **kwargs):
            target = tuple(a for a in args if not isinstance(a, socket.socket))
            attempts.append(f"{name}{target}")
            raise OSError(f"network refused by test: {name}")
        return refuse

    monkeypatch.setattr(socket.socket, "connect", record("socket.connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", record("socket.connect_ex"))
    monkeypatch.setattr(socket, "create_connection", record("socket.create_connection"))
    # a DNS lookup is an outbound request in its own right, and precedes any connect
    monkeypatch.setattr(socket, "getaddrinfo", record("socket.getaddrinfo"))

    from memory_agent.embedding import SentenceTransformerEmbedder

    embedder = SentenceTransformerEmbedder("all-MiniLM-L6-v2")
    vector = embedder.embed(["offline"])[0]

    assert attempts == [], (
        f"loading a cached model reached for the network: {attempts}. The offline "
        "guarantee is broken; see the offline switch in SentenceTransformerEmbedder")
    assert len(vector) == embedder.dimensions
