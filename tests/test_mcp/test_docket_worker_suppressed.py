"""Real MCP servers must remain usable without an idle task worker.

FastMCP v4 makes task execution optional. Genesis does not install that extra,
so the v2 private _is_mounted workaround and its framework-source tests are
retired. Keep the actual no-worker and registration coverage contracts.
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastmcp import Client

from tests.conftest import private_module

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SERVER_SCRIPT = _REPO_ROOT / "scripts" / "genesis_mcp_server.py"
_REAL_SERVER_MODULES = (
    "genesis.mcp.health",
    "genesis.mcp.memory",
    "genesis.mcp.outreach_mcp",
    "genesis.mcp.recon_mcp",
    "genesis.mcp.discord_bot_mcp",
)


def _server_module():
    return private_module("_genesis_mcp_server_under_test", _SERVER_SCRIPT)


@pytest.mark.parametrize("module_path", _REAL_SERVER_MODULES)
async def test_real_servers_have_tools_and_do_not_start_a_task_worker(module_path):
    server = importlib.import_module(module_path).mcp
    assert await server.local_provider.list_tools(), "empty registry would make this vacuous"
    assert await server.get_tasks() == []
    async with Client(server) as client:
        assert await client.list_tools()
        assert server._docket is None
        assert server._worker is None


def _modules_imported_by_the_real_servers() -> set[str]:
    """Measure only a fresh server import closure, from this checkout."""
    probe = f"""
import importlib, json, sys
mods = [importlib.import_module(n) for n in {list(_REAL_SERVER_MODULES)!r}]
print(json.dumps({{
    "modules": sorted(m for m in sys.modules if m.startswith("genesis.")),
    "resolved_from": mods[0].__file__,
}}))
"""
    env = dict(os.environ)
    src = str(_REPO_ROOT / "src")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [src, env.get("PYTHONPATH")]))
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True,
        cwd=str(_REPO_ROOT), env=env, timeout=300,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    payload = json.loads(result.stdout)
    assert _REPO_ROOT in Path(payload["resolved_from"]).resolve().parents
    return set(payload["modules"])


_TOOL_MODULES_NOT_SERVED_BY_MCP = {
    # These register on health only when the in-process runtime initializes
    # user jobs. The standalone MCP bootstrappers do not import this module.
    "genesis.mcp.health.user_job_tools",
}


def test_every_tool_registering_module_is_visible_to_the_inventory():
    imported = _modules_imported_by_the_real_servers()
    decorator = re.compile(r"^\s*@mcp\.tool\b", re.MULTILINE)
    registering = set()
    # Enumeration bound: this covers the @mcp.tool spelling, not arbitrary
    # aliased decorators or runtime provider composition.
    for path in sorted((_REPO_ROOT / "src" / "genesis" / "mcp").rglob("*.py")):
        if decorator.search(path.read_text(encoding="utf-8", errors="ignore")):
            rel = path.relative_to(_REPO_ROOT / "src")
            registering.add(str(rel.with_suffix("")).replace("/", ".").removesuffix(".__init__"))
    assert len(registering) > 20, "the registration scan did not find the known population"
    unimported = registering - imported
    assert not unimported - _TOOL_MODULES_NOT_SERVED_BY_MCP
    assert not _TOOL_MODULES_NOT_SERVED_BY_MCP - unimported


def test_every_bootstrapper_routes_through_the_run_mcp_chokepoint():
    bootstrappers = _server_module()._BOOTSTRAPPERS
    assert set(bootstrappers) == {"health", "memory", "outreach", "recon", "discord-bot"}
    for name, bootstrap in bootstrappers.items():
        source = inspect.getsource(bootstrap)
        assert "_run_mcp(" in source, name
        assert ".run(" not in source.replace("_run_mcp(", ""), name


def test_run_mcp_preserves_stdio_transport_arguments():
    class Server:
        def run(self, **kwargs):
            self.kwargs = kwargs

    server = Server()
    _server_module()._run_mcp(server, {"transport": "stdio"})
    assert server.kwargs == {"transport": "stdio"}
    assert not hasattr(server, "_is_mounted")
