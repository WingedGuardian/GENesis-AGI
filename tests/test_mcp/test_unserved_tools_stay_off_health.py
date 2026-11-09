"""Importing an unserved tool module must not add tools to the health server.

`genesis.mcp.health.user_job_tools` is imported by genesis-server's runtime
init, but no MCP server serves it (`tool_names._NOT_SERVED`), so no session can
call its tools. When it registered them on the shared health object, the
dashboard (`/api/genesis/mcp/tools`) and the status snapshot listed 96 health
tools where sessions can call 92, and a test that counted tools passed or failed
depending on which test file had imported the module first.
"""

from __future__ import annotations

import importlib

import pytest


async def _health_tool_names() -> set[str]:
    health = importlib.import_module("genesis.mcp.health_mcp")
    return {tool.name for tool in await health.mcp.list_tools()}


@pytest.mark.asyncio
async def test_importing_user_job_tools_leaves_the_health_tool_list_unchanged():
    before = await _health_tool_names()
    importlib.import_module("genesis.mcp.health.user_job_tools")
    after = await _health_tool_names()
    assert after == before
    assert not {name for name in after if name.startswith("user_job_")}
