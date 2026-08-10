"""MCP server. A thin adapter over MemoryService.

Tool names, descriptions, and input schemas come straight out of
contracts/mcp-tools.json rather than being restated here, so the contract stays
the single source of truth and cannot drift from what the server advertises.

    python -m memory_agent.server
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path

from .config import Policy
from .errors import MemoryAgentError
from .service import MemoryService

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "mcp-tools.json"
log = logging.getLogger("memory_agent.server")


def load_contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def _strip_refs(schema):
    """MCP clients get a self-contained inputSchema.

    $refs point at sibling files that a client cannot fetch, so they are replaced
    with the permissive shape they describe. Validation still happens server-side
    against the real schemas - this only affects what the client is shown.
    """
    if isinstance(schema, dict):
        if "$ref" in schema:
            # A $ref may carry siblings (typically a description). Keep those and
            # replace only the reference, or the sibling text is lost and - worse -
            # the dangling $ref survives into what the client is shown.
            ref = schema["$ref"]
            keep = {k: _strip_refs(v) for k, v in schema.items()
                    if k not in ("$ref", "$schema", "$id")}
            if "uuid" in ref:
                resolved = {"type": "string", "description": "UUID"}
            elif "timestamp" in ref:
                resolved = {"type": "string", "description": "RFC 3339 UTC timestamp"}
            elif "scope" in ref:
                resolved = {"type": "string", "description": "Memory scope"}
            elif "unit_interval" in ref:
                resolved = {"type": "number", "minimum": 0, "maximum": 1}
            elif "provenance" in ref:
                resolved = {"type": "object", "description": "Where this record came from"}
            else:
                resolved = {"type": "object"}
            return {**resolved, **keep}
        return {k: _strip_refs(v) for k, v in schema.items() if k not in ("$schema", "$id")}
    if isinstance(schema, list):
        return [_strip_refs(v) for v in schema]
    return schema


HANDLERS = {
    "memory_open_cycle": "open_cycle",
    "memory_close_cycle": "close_cycle",
    "memory_recall": "recall",
    "memory_remember": "remember",
    "memory_forget": "forget",
    "memory_reflect": "reflect",
    "memory_propose_procedure": "propose_procedure",
    "memory_review_proposals": "review_proposals",
    "memory_stats": "stats",
}


def _tool_models(contract: dict):
    """Build mcp.types.Tool objects from the contract.

    The SDK renamed inputSchema -> input_schema between 1.x and 2.x. Rather than
    pinning a version, construct by whichever field name the installed Tool model
    actually declares - the contract is the source of truth either way.
    """
    from mcp.types import Tool, ToolAnnotations

    field = "input_schema" if "input_schema" in getattr(Tool, "model_fields", {}) else "inputSchema"
    ann_fields = getattr(ToolAnnotations, "model_fields", {})
    out = []
    for t in contract["tools"]:
        kwargs = {"name": t["name"], "description": t["description"],
                  field: _strip_refs(t["inputSchema"])}
        try:
            # 1.x used camelCase here, 2.x uses snake_case. Map by whichever the
            # installed model declares.
            ann = {}
            for camel, value in t["annotations"].items():
                snake = "".join("_" + c.lower() if c.isupper() else c for c in camel)
                key = camel if camel in ann_fields else (snake if snake in ann_fields else None)
                if key:
                    ann[key] = value
            if ann:
                kwargs["annotations"] = ToolAnnotations(**ann)
        except Exception:  # annotations are advisory; never fail startup over them
            pass
        out.append(Tool(**kwargs))
    return out


def annotation(tool, camel: str):
    """Read a tool annotation regardless of the SDK's field naming."""
    snake = "".join("_" + c.lower() if c.isupper() else c for c in camel)
    for name in (camel, snake):
        if hasattr(tool.annotations, name):
            return getattr(tool.annotations, name)
    return None


def _dispatch(service: MemoryService, name: str, arguments: dict | None):
    """Run one tool call. Synchronous; the async layers wrap it."""
    if name not in HANDLERS:
        return {"error": "UNKNOWN_TOOL", "tool": name}
    try:
        return getattr(service, HANDLERS[name])(**(arguments or {}))
    except MemoryAgentError as exc:
        log.info("%s -> %s", name, exc.code)
        return exc.to_dict()
    except TypeError as exc:
        return {"error": "INVALID_ARGUMENTS", "message": str(exc)}
    except sqlite3.IntegrityError as exc:
        # A DDL constraint is the last line of defence, not an API. Reaching here
        # means some field is unvalidated upstream - a caller should still get a
        # typed refusal rather than a raw "CHECK constraint failed: ..." string,
        # which names a column they cannot see and no allowed set.
        log.warning("%s -> unvalidated constraint violation: %s", name, exc)
        return {"error": "INVALID_FIELD_VALUE", "message": str(exc), "retryable": False}


def build_server(service: MemoryService):
    """Wire the contract onto whichever MCP SDK generation is installed.

    1.x exposes @server.list_tools()/@server.call_tool() decorators; 2.x replaced
    them with add_request_handler(method, params_type, handler). Supporting both
    keeps this runnable as the SDK moves, which it is actively doing.
    """
    from mcp.server import Server
    from mcp.types import TextContent

    contract = load_contract()
    missing = {t["name"] for t in contract["tools"]} - set(HANDLERS)
    if missing:
        raise RuntimeError(f"contract declares tools with no handler: {sorted(missing)}")

    server = Server(contract["server"]["name"])
    tools = _tool_models(contract)

    async def run_tool(name: str, arguments: dict | None):
        # Off the event loop: every handler is synchronous SQLite work, and
        # blocking the loop would stall concurrent readers.
        return await asyncio.to_thread(_dispatch, service, name, arguments)

    if hasattr(server, "list_tools") and hasattr(server, "call_tool"):
        @server.list_tools()
        async def _list():
            return tools

        @server.call_tool()
        async def _call(name: str, arguments: dict):
            return [TextContent(type="text",
                                text=json.dumps(await run_tool(name, arguments), indent=2, default=str))]
        return server

    from mcp.types import (
        CallToolRequest,
        CallToolRequestParams,
        CallToolResult,
        ListToolsRequest,
        ListToolsResult,
        PaginatedRequestParams,
    )

    # 2.x invokes handlers as (context, validated_params). The context is NOT the
    # request: it carries its own `params`, a raw Mapping of the wire payload. An
    # earlier version reached for `getattr(req, "params", req)` on the first
    # argument, which therefore returned that dict and failed on `.name` - the
    # server connected happily and then every tool call errored. Take params from
    # the second argument, which the SDK has already validated against
    # CallToolRequestParams.
    def _field(obj, key):
        return obj.get(key) if isinstance(obj, Mapping) else getattr(obj, key, None)

    async def _list_handler(_ctx, _params=None):
        return ListToolsResult(tools=tools)

    async def _call_handler(_ctx, params):
        result = await run_tool(_field(params, "name"), _field(params, "arguments") or {})
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, indent=2, default=str))],
            is_error=isinstance(result, dict) and "error" in result,
        )

    server.add_request_handler(ListToolsRequest.model_fields["method"].default,
                               PaginatedRequestParams, _list_handler)
    server.add_request_handler(CallToolRequest.model_fields["method"].default,
                               CallToolRequestParams, _call_handler)
    return server


async def _run() -> None:
    from mcp.server.stdio import stdio_server

    logging.basicConfig(
        level=os.environ.get("MEMORY_AGENT_LOG", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    policy = Policy.load()
    policy.require_reviewers()  # fail at startup, not at the first approval
    service = MemoryService(policy)
    log.info("memory-agent ready: db=%s vector=%s embedder=%s reviewers=%d",
             policy.db_path, service.store.vector_ok, service.embedder.name,
             len(policy.learning.approval.reviewers))
    server = build_server(service)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
