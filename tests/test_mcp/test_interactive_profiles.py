"""Interactive admission is a closed scope, independent of client filtering."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from genesis.mcp.external_profiles import ExternalProfileMiddleware, profile_tools

DEFERRED = {
    "health": {
        "health_errors", "health_alerts", "task_control", "campaign_trigger",
        "user_job_create", "user_job_list", "user_job_control", "user_job_history",
        "ego_proposal_resolve",
    },
    "memory": {"document_delete"},
}


@pytest.mark.parametrize("server,count", [("health", 89), ("memory", 35)])
def test_interactive_names_exist_in_fresh_standalone_registry(server, count, tmp_path):
    root = Path(__file__).resolve().parents[2]
    code = (
        "import asyncio,json\n"
        f"from genesis.mcp.{server}_mcp import mcp\n"
        "print(json.dumps(sorted(asyncio.run(mcp.get_tools()))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=root,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PYTHONPATH": str(root / "src")},
        capture_output=True, text=True, check=True, timeout=45,
    )
    registered = set(json.loads(result.stdout))
    assert len(registered) == count
    assert profile_tools(server, "interactive") == registered - DEFERRED[server]


@pytest.mark.parametrize("server,count", [("health", 84), ("memory", 34)])
async def test_interactive_scope_dispatches_every_admitted_name_and_refuses_others(server, count):
    allowed = profile_tools(server, "interactive")
    assert len(allowed) == count
    assert profile_tools(server, "external") <= allowed
    assert profile_tools(server, "validator") == profile_tools(server, "external")
    assert not (allowed & DEFERRED[server])
    entered = []
    mcp = FastMCP("interactive-scope-fixture")

    def handler(name):
        def invoke() -> str:
            entered.append(name)
            return name
        return invoke

    for name in allowed | DEFERRED[server] | {"future_unreviewed_tool"}:
        mcp.tool(handler(name), name=name)
    mcp.add_middleware(ExternalProfileMiddleware(server, "interactive"))
    async with Client(mcp) as client:
        assert {tool.name for tool in await client.list_tools()} == allowed
        for name in sorted(allowed):
            assert (await client.call_tool(name)).data == name
        for name in sorted(DEFERRED[server] | {"future_unreviewed_tool"}):
            with pytest.raises(ToolError, match="unavailable"):
                await client.call_tool(name)
    assert entered == sorted(allowed)


@pytest.mark.parametrize("profile", ["external", "validator", "interactive"])
def test_native_cli_accepts_closed_roles(profile):
    from scripts.genesis_mcp_server import parse_args

    assert parse_args(["--server", "memory", "--external-client", profile]).external_client == profile


@pytest.mark.parametrize("profile", ["external", "validator", "interactive"])
@pytest.mark.parametrize("name", ["health", "memory"])
def test_native_entrypoint_sets_secret_policy_before_bootstrap(monkeypatch, tmp_path, profile, name):
    from scripts import genesis_mcp_server as server

    events = []
    monkeypatch.setattr("genesis.env.secrets_path", lambda: tmp_path / "missing.env")
    monkeypatch.setattr(server, "is_genesis_enabled", lambda: True)
    monkeypatch.setattr("genesis.observability.mcp_spawn_identity.capture_spawn_identity", lambda: None)
    monkeypatch.setattr("genesis.routing.standalone.configure_external_secret_loading", lambda keys: events.append(keys))
    monkeypatch.setitem(server._BOOTSTRAPPERS, name, lambda transport, **kw: events.append(kw))
    server.main(["--server", name, "--external-client", profile])
    from genesis.mcp.external_profiles import EXTERNAL_SECRET_BLOCKED_KEYS

    assert events == [EXTERNAL_SECRET_BLOCKED_KEYS, {"external_profile": profile}]


@pytest.mark.parametrize("role", ["external", "validator", "interactive"])
@pytest.mark.parametrize("form", ["split", "equals"])
def test_codex_launcher_forwards_one_role_and_keeps_transport(monkeypatch, role, form):
    from scripts import codex_external_mcp as launcher
    from scripts.genesis_mcp_server import parse_args

    execute = MagicMock()
    monkeypatch.setattr(launcher.os, "execve", execute)
    selection = ["--profile", role] if form == "split" else [f"--profile={role}"]
    launcher.main(["--server", "health", *selection, "--transport", "streamable-http", "--port", "8109"])
    argv = execute.call_args.args[1]
    assert argv.count("--external-client") == 1
    parsed = parse_args(argv[1:])
    assert (parsed.external_client, parsed.transport, parsed.port) == (role, "streamable-http", 8109)


@pytest.mark.parametrize("args", [
    ["--profile", "unknown"], ["--profile", "interactive", "--profile", "validator"],
    ["--external-client"], ["--external-client=validator"],
])
def test_codex_launcher_refuses_invalid_or_conflicting_role_before_exec(monkeypatch, args):
    from scripts import codex_external_mcp as launcher

    execute = MagicMock()
    monkeypatch.setattr(launcher.os, "execve", execute)
    with pytest.raises(SystemExit) as error:
        launcher.main(["--server", "health", *args])
    assert error.value.code == 2
    execute.assert_not_called()
