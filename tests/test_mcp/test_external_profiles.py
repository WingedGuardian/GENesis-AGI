"""Scope enforcement must stop direct protocol calls, not merely hide tools."""

import tomllib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

from genesis.mcp.external_profiles import ExternalProfileMiddleware, profile_tools


@pytest.mark.parametrize("profile", ["external", "validator"])
@pytest.mark.parametrize("server", ["health", "memory"])
async def test_native_protocol_filters_and_blocks_direct_calls(profile, server):
    mcp = FastMCP("fixture")
    entered = []
    allowed = sorted(profile_tools(server, profile))[0]

    @mcp.tool(name=allowed)
    def read_fixture() -> str:
        entered.append("allowed")
        return "fixture"

    @mcp.tool()
    def forbidden_write() -> str:
        entered.append("forbidden")
        return "unexpected"

    mcp.add_middleware(ExternalProfileMiddleware(server, profile))

    class InstrumentationFixture(Middleware):
        async def on_call_tool(self, context, call_next):
            entered.append("instrumented")
            return await call_next(context)

    # Standalone init appends instrumentation after profile admission. Rejected
    # calls must not enter it, including its commit/rollback boundary.
    mcp.add_middleware(InstrumentationFixture())
    async with Client(mcp) as client:
        assert [tool.name for tool in await client.list_tools()] == [allowed]
        assert (await client.call_tool(allowed)).data == "fixture"
        # Client call_tool must attempt the protocol request even though hidden.
        with pytest.raises(ToolError, match="unavailable"):
            await client.call_tool("forbidden_write")
    assert entered == ["instrumented", "allowed"]


async def test_discovery_filters_protocol_keys_for_aliased_tools():
    child = FastMCP("child")
    parent = FastMCP("parent")

    @child.tool()
    def original_name() -> str:
        return "fixture"

    parent.mount(child, tool_names={"original_name": "health_status"})
    parent.add_middleware(ExternalProfileMiddleware("health", "external"))
    async with Client(parent) as client:
        assert [tool.name for tool in await client.list_tools()] == ["health_status"]
        assert (await client.call_tool("health_status")).data == "fixture"
        with pytest.raises(ToolError, match="unavailable"):
            await client.call_tool("original_name")


@pytest.mark.parametrize("profile,server", [
    ("unknown", "health"), ("", "memory"), ("external", "recon"),
    ("validator", "outreach"), ("validator", "discord-bot"),
])
def test_unknown_role_or_server_refused(profile, server):
    with pytest.raises(ValueError):
        ExternalProfileMiddleware(server, profile)


@pytest.mark.parametrize("server", ["health", "memory"])
def test_default_surface_matches_committed_codex_allowlist(server):
    root = Path(__file__).resolve().parents[2]
    config = tomllib.loads((root / ".codex" / "config.toml").read_text())
    assert profile_tools(server, "external") == frozenset(
        config["mcp_servers"][f"genesis-{server}"]["enabled_tools"]
    )


@pytest.mark.parametrize("server", ["outreach", "recon", "discord-bot"])
def test_cli_rejects_external_profile_on_other_servers(server, capsys):
    from scripts.genesis_mcp_server import parse_args

    with pytest.raises(SystemExit) as error:
        parse_args(["--server", server, "--external-client", "validator"])
    assert error.value.code == 2
    assert "External client profiles support only health and memory servers" in capsys.readouterr().err


@pytest.mark.parametrize("profile", ["external", "validator", None])
@pytest.mark.parametrize("has_db", [True, False])
async def test_health_bootstrap_scopes_before_initialization_and_skips_dispatch(
    monkeypatch, tmp_path, profile, has_db,
):
    import scripts.genesis_mcp_server as server
    from genesis.mcp import health_mcp

    mcp = FastMCP("fixture")
    entered = []

    @mcp.tool()
    def health_status() -> str:
        entered.append("read")
        return "fixture"

    @mcp.tool()
    def session_start() -> str:
        entered.append("dispatch")
        return "unexpected"

    db_path = tmp_path / "fixture.db"
    if has_db:
        db_path.touch()
    db = AsyncMock()
    monkeypatch.setattr(server, "_DEFAULT_DB", db_path)
    monkeypatch.setattr(server, "clear_mcp_crash", MagicMock())
    monkeypatch.setattr(server, "_run_mcp", MagicMock())
    monkeypatch.setattr(health_mcp, "mcp", mcp)
    initializer = MagicMock(side_effect=lambda *a, **k: entered.append("init"))
    monkeypatch.setattr(health_mcp, "init_health_mcp", initializer)
    monkeypatch.setattr("genesis.db.connection.get_db", AsyncMock(return_value=db))
    router = MagicMock()
    monkeypatch.setattr("genesis.routing.standalone.create_standalone_router", router)
    direct = MagicMock()
    campaign = MagicMock()
    monkeypatch.setattr("genesis.mcp.health.direct_session_tools.init_direct_session_tools", direct)
    monkeypatch.setattr("genesis.mcp.health.campaign_tools.init_campaign_tools", campaign)
    monkeypatch.setattr("genesis.mcp.health.browser.async_cleanup", AsyncMock())
    monkeypatch.setattr("genesis.observability.provider_activity.ProviderActivityTracker", MagicMock())
    server._bootstrap_health({}, external_profile=profile)
    if profile:
        assert isinstance(mcp.middleware[0], ExternalProfileMiddleware)
    async with Client(mcp) as client:
        assert (await client.call_tool("health_status")).data == "fixture"
        if profile:
            with pytest.raises(ToolError, match="unavailable"):
                await client.call_tool("session_start")
    assert entered == ["init", "read"]
    expected_init = has_db and profile is None
    assert router.called is expected_init
    assert direct.called is expected_init
    assert campaign.called is expected_init
    assert db.execute.called is expected_init
    if has_db:
        db.close.assert_awaited_once()
