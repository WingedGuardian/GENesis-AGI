"""Preflight ordering and actual owner admission at Uvicorn shutdown."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from genesis.db.request_database import DatabaseScopeError, RequestDatabase


@pytest.fixture
def preflight(monkeypatch):
    from genesis import caller_context, caller_credentials
    from genesis.mcp import shared_http, shared_runner, shared_server, shared_settings
    from genesis.mcp.shared_http import _auth
    from genesis.observability import mcp_spawn_identity

    state = SimpleNamespace(events=[], enabled=True, commit="a" * 40,
                            started="2026-11-08T00:00:00+00:00")
    state.source_root = Path(shared_runner.__file__).resolve().parents[3]
    monkeypatch.setattr(caller_context, "_shared_service", False)
    for name in (*caller_context.IDENTITY_ENV_VARS, "GENESIS_SLOT"):
        monkeypatch.setenv(name, "inherited")
    monkeypatch.setattr(shared_http, "require_qualified_stack", lambda: state.events.append("qualified"))
    monkeypatch.setattr(mcp_spawn_identity, "capture_spawn_identity",
        lambda **kwargs: state.events.append(("capture", kwargs)))
    monkeypatch.setattr(mcp_spawn_identity, "spawn_commit", lambda: state.commit)
    monkeypatch.setattr(mcp_spawn_identity, "spawn_at", lambda: state.started)
    monkeypatch.setattr(shared_settings, "load_shared_settings",
        lambda *args: state.events.append("settings") or "settings")
    resolver = object()
    monkeypatch.setattr(caller_credentials.CallerResolver, "load",
        lambda: state.events.append("credentials") or resolver)
    monkeypatch.setattr(_auth, "usable_credentials",
        lambda *args: state.events.append("usable"))

    async def serve(*args, **kwargs):
        import os

        assert args == ("settings",) and kwargs["resolver"] is resolver
        assert "GENESIS_SLOT" not in os.environ
        assert caller_context.caller_context_from_env().origin == "external_untrusted"
        state.events.append("serve")

    monkeypatch.setattr(shared_server, "serve_shared", serve)
    state.run = lambda root=state.source_root: shared_runner.run_shared("health", source_root=root,
        db_path=Path("/unused/db"), status_path=Path("/unused/status"), cache_dir=Path("/unused/cache"),
        enabled=lambda: state.enabled, setup_routing=Mock(), setup_health_actions=Mock())
    return state


def test_shared_preflight_captures_before_settings_credentials_and_construction(preflight):
    preflight.run()
    assert preflight.events == ["qualified", ("capture", {
        "source_root": preflight.source_root, "persist_session": False}),
        "settings", "credentials", "usable", "serve"]


@pytest.mark.parametrize("commit", [None, "", "unknown", "A" * 40, "a" * 39])
def test_unknown_identity_refuses_before_config_or_credentials(preflight, commit):
    preflight.commit = commit
    with pytest.raises(ValueError, match="known loaded commit"):
        preflight.run()
    assert len(preflight.events) == 2


@pytest.mark.parametrize("started", [None, "", "bad", "2026-11-08T00:00:00"])
def test_naive_or_missing_startup_time_refuses_before_resources(preflight, started):
    preflight.started = started
    with pytest.raises(ValueError, match="aware startup timestamp"):
        preflight.run()
    assert len(preflight.events) == 2


def test_disabled_shared_never_loads_credentials_or_constructs(preflight):
    preflight.enabled = False
    with pytest.raises(ValueError, match="cannot start disabled"):
        preflight.run()
    assert preflight.events[-1] == "settings" and "credentials" not in preflight.events


def test_mixed_checkout_refuses_before_identity_config_credentials(preflight, tmp_path):
    with pytest.raises(ValueError, match="launcher and imported source differ"):
        preflight.run(root=tmp_path)
    assert preflight.events == []


async def test_shutdown_closes_admission_before_http_shutdown(tmp_path, monkeypatch):
    import uvicorn

    from genesis.mcp.shared_server import SharedServer

    path = tmp_path / "units.db"
    path.touch()
    database = RequestDatabase(path, max_units=1)
    events = []

    async def shutdown(server, sockets=None):
        assert database._closing
        with pytest.raises(DatabaseScopeError):
            async with database.unit_of_work():
                pytest.fail("New unit accepted during shutdown")
        events.append("http-shutdown")
        await asyncio.sleep(0)

    monkeypatch.setattr(uvicorn.Server, "shutdown", shutdown)
    server = SharedServer(uvicorn.Config(lambda: None), database=database, grace_s=0.25)
    async def lifespan_shutdown():
        events.append("sdk-settled")

    server.lifespan = SimpleNamespace(shutdown=lifespan_shutdown)
    await server.shutdown()
    assert events == ["http-shutdown", "sdk-settled"] and database._close_task.done()
    assert server.shutdown_entered
