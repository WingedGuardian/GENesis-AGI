"""HTTP Tool API — exposes Genesis MCP tool functions as REST endpoints.

Provides a second door into the same tool implementations that MCP serves
over stdio. CC sessions keep using MCP; external consumers (voice pipeline,
Home Assistant, future agent backends) use these HTTP endpoints.

Route pattern: POST /api/t/{tool_name}
Body: JSON with tool parameters
Response: JSON tool result
"""

from __future__ import annotations

import inspect
import json
import logging
from typing import Any

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)


def _unwrap_mcp_tool(tool_or_fn: Any) -> Any:
    """Unwrap a FastMCP FunctionTool to get the underlying async function.

    @mcp.tool() wraps functions in a FunctionTool object. The original
    async function is available as .fn. Plain functions pass through.
    """
    if hasattr(tool_or_fn, "fn") and callable(tool_or_fn.fn):
        return tool_or_fn.fn
    return tool_or_fn


def _build_tool_registry() -> dict[str, dict[str, Any]]:
    """Build the tool name → callable registry.

    Lazy-imports MCP tool functions to avoid import-time side effects.
    Called once on first request, cached at module level.
    """
    # Health server tools — prefer _impl_* where available
    from genesis.mcp.health.status import _impl_health_status
    from genesis.mcp.health.web_tools import _impl_web_fetch, _impl_web_search

    # Memory server tools — @mcp.tool() wraps these in FunctionTool,
    # so we unwrap to get the raw async function.
    from genesis.mcp.memory.core import memory_recall, memory_store
    from genesis.mcp.memory.knowledge import knowledge_recall

    # Outreach server tools — also FunctionTool-wrapped
    from genesis.mcp.outreach_mcp import outreach_send

    return {
        "health_status": {"fn": _impl_health_status, "method": "GET"},
        "memory_recall": {"fn": _unwrap_mcp_tool(memory_recall), "method": "POST"},
        "memory_store": {"fn": _unwrap_mcp_tool(memory_store), "method": "POST"},
        "knowledge_recall": {"fn": _unwrap_mcp_tool(knowledge_recall), "method": "POST"},
        "outreach_send": {"fn": _unwrap_mcp_tool(outreach_send), "method": "POST"},
        "web_fetch": {"fn": _impl_web_fetch, "method": "POST"},
        "web_search": {"fn": _impl_web_search, "method": "POST"},
    }


# Lazy singleton — safe without a lock because: (1) GIL makes reference
# assignment atomic, (2) _build_tool_registry() is deterministic and
# side-effect-free, so double-init on concurrent first requests is harmless.
_registry: dict[str, dict[str, Any]] | None = None


def _get_registry() -> dict[str, dict[str, Any]]:
    global _registry
    if _registry is None:
        _registry = _build_tool_registry()
    return _registry


def _normalize_result(result: Any) -> Any:
    """Normalize tool result to a JSON-serializable value.

    MCP tools return different types:
    - dict: pass through
    - list[dict]: pass through
    - str: may be JSON-encoded (outreach_send) — try to parse, else wrap

    Always returns a dict or list — never a bare scalar — so callers
    can rely on structured JSON responses.
    """
    if isinstance(result, (dict, list)):
        return result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            if isinstance(parsed, (dict, list)):
                return parsed
            return {"result": parsed}
        except (json.JSONDecodeError, ValueError):
            return {"result": result}
    return {"result": str(result)}


@blueprint.route("/api/t/", methods=["GET"])
@_async_route
async def tool_list():
    """List available tools."""
    registry = _get_registry()
    tools = []
    for name, entry in registry.items():
        fn = entry["fn"]
        sig = inspect.signature(fn)
        params = {}
        for pname, param in sig.parameters.items():
            pinfo: dict[str, Any] = {}
            if param.default is not inspect.Parameter.empty:
                pinfo["default"] = param.default
            if param.annotation is not inspect.Parameter.empty:
                pinfo["type"] = str(param.annotation)
            params[pname] = pinfo
        tools.append({
            "name": name,
            "method": entry["method"],
            "endpoint": f"/api/t/{name}",
            "parameters": params,
        })
    return jsonify({"tools": tools})


@blueprint.route("/api/t/<tool_name>", methods=["GET", "POST"])
@_async_route
async def tool_invoke(tool_name: str):
    """Invoke a tool by name."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped:
        return jsonify({"error": "Genesis runtime not bootstrapped"}), 503

    registry = _get_registry()
    entry = registry.get(tool_name)
    if entry is None:
        available = sorted(registry.keys())
        return jsonify({
            "error": f"Unknown tool: {tool_name}",
            "available_tools": available,
        }), 404

    fn = entry["fn"]
    expected_method = entry["method"]

    # GET tools (no params needed)
    if expected_method == "GET":
        try:
            result = await fn()
            return jsonify(_normalize_result(result))
        except Exception:
            logger.exception("Tool %s failed", tool_name)
            return jsonify({"error": f"Tool {tool_name} execution failed"}), 500

    # POST tools — extract params from JSON body
    if request.method == "GET":
        # Caller used GET on a POST tool — return usage info
        sig = inspect.signature(fn)
        params = {}
        for pname, param in sig.parameters.items():
            pinfo: dict[str, Any] = {}
            if param.default is not inspect.Parameter.empty:
                pinfo["default"] = param.default
            if param.annotation is not inspect.Parameter.empty:
                pinfo["type"] = str(param.annotation)
            params[pname] = pinfo
        return jsonify({
            "tool": tool_name,
            "method": "POST",
            "parameters": params,
            "usage": f"POST /api/t/{tool_name} with JSON body",
        })

    body = request.get_json(silent=True) or {}

    # Filter body to only include params the function accepts
    sig = inspect.signature(fn)
    valid_params = set(sig.parameters.keys())
    kwargs = {k: v for k, v in body.items() if k in valid_params}

    # Check required params
    missing = []
    for pname, param in sig.parameters.items():
        if param.default is inspect.Parameter.empty and pname not in kwargs:
            missing.append(pname)
    if missing:
        return jsonify({
            "error": f"Missing required parameters: {missing}",
            "tool": tool_name,
        }), 400

    try:
        result = await fn(**kwargs)
        return jsonify(_normalize_result(result))
    except Exception as exc:
        logger.exception("Tool %s failed", tool_name)
        return jsonify({"error": f"Tool {tool_name} execution failed", "detail": str(exc)}), 500
