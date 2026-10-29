"""Actual standalone tools and SQL through isolated FastMCP clients."""

import asyncio
import json
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

from genesis.db.crud import cost_events
from genesis.db.request_database import RequestDatabase
from genesis.mcp.request_service import initialize_request_service
from genesis.mcp.standalone_health import StandaloneHealthDataService
from tests.test_mcp.test_request_service_initialization import database as database
from tests.test_mcp.test_request_service_initialization import isolate_module

pytestmark = pytest.mark.asyncio


@pytest.fixture(params=[1, 2, 4])
async def health_db(database, request):
    db = RequestDatabase(database.path, max_units=request.param)
    async with db.unit_of_work():
        await cost_events.create(
            db, id="synthetic-cost", event_type="llm_call", cost_usd=2.5,
            created_at=datetime.now(UTC).isoformat(), provider="synthetic",
        )
        await db.execute(
            "INSERT INTO observations(id,source,type,content,created_at,priority) "
            "VALUES (?,?,?,?,?,?)",
            ("synthetic", "genesis_version", "genesis_update_available",
             json.dumps({"target_tag": "synthetic-v2", "commits_behind": 3}),
             datetime.now(UTC).isoformat(), "low"),
        )
    try:
        yield db, request.param
    finally:
        await db.close()


@pytest.fixture
def standalone(health_db, monkeypatch, tmp_path):
    import genesis.mcp.standalone_health as health_module
    from genesis.runtime import GenesisRuntime

    db, _ = health_db
    module = isolate_module(monkeypatch, "health")
    monkeypatch.setattr(module, "_alert_history", {})
    status = tmp_path / "status.json"
    status.write_text("{}")
    service = StandaloneHealthDataService(status_path=status, db=db)
    cc = import_module("genesis.observability.snapshots.cc_sessions")
    infrastructure = import_module("genesis.observability.snapshots.infrastructure")

    async def local_infrastructure(connection, *args, **kwargs):
        rows = await connection.execute_fetchall("SELECT 42")
        return {"synthetic_value": rows[0][0]}

    # Replace external host/provider/file readers only. All tool and SQL bodies run.
    monkeypatch.setattr(cc, "_peer_availability_snapshot", AsyncMock(return_value=[]))
    monkeypatch.setattr(cc, "_fallback_snapshot", lambda: {})
    monkeypatch.setattr(infrastructure, "infrastructure", local_infrastructure)
    monkeypatch.setattr("genesis.observability.service_status.collect_service_status", lambda: {})
    monkeypatch.setattr(health_module, "_load_mcp_crash_status", lambda: {})
    monkeypatch.setattr(GenesisRuntime, "peek", lambda: None)
    monkeypatch.setattr(GenesisRuntime, "instance", lambda: SimpleNamespace(_routing_config=None))
    monkeypatch.setattr(module.errors, "_backups_enabled", lambda: False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        "genesis.mcp.health.manifest.compute_heartbeat_staleness",
        AsyncMock(return_value={"status": "no_heartbeat"}),
    )
    return module, service


def assert_seeded_reply(reply, tool, *, failed_enrichment=False):
    if tool == "health_status":
        assert reply.data["cost"]["daily_usd"] == (None if failed_enrichment else 2.5)
        if not failed_enrichment:
            assert reply.data["infrastructure"]["synthetic_value"] == 42
            assert reply.data["cc_sessions"]["foreground"]["active"] == 0
            assert reply.data["awareness"]["status"] == "no_ticks"
            assert reply.data["outreach_stats"]["total"] == 0
    else:
        # Decode the actual MCP response; list[dict] convenience models vary.
        alerts = json.loads(reply.content[0].text)
        matches = [a for a in alerts if a["id"] == "genesis:update_available"]
        assert len(matches) == 1 and "synthetic-v2" in matches[0]["message"]


@pytest.mark.parametrize("tool", ["health_status", "health_alerts"])
@pytest.mark.parametrize("outcome", ["success", "cancel", "error", "shutdown"])
async def test_standalone_requests_keep_independent_ownership(health_db, standalone, monkeypatch, tool, outcome):
    db, capacity = health_db
    module, service = standalone
    entered, second_entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    leases = []
    original = service._enrich_from_db

    async def enrich(result):
        leases.append(db._current.get())
        if len(leases) == 1:
            entered.set()
            await release.wait()
            if outcome == "error":
                raise RuntimeError("synthetic enrichment failure")
        else:
            second_entered.set()
        await original(result)

    monkeypatch.setattr(service, "_enrich_from_db", enrich)
    server = await initialize_request_service("health", db=db, service=service)
    server.add_tool(module.status.health_status)
    server.add_tool(module.errors.health_alerts)
    async with Client(server) as first, Client(server) as second:
        request_id = first.session._request_id
        caller = asyncio.create_task(first.call_tool(tool))
        peer = None
        try:
            await entered.wait()
            peer = asyncio.create_task(second.call_tool(tool))
            if capacity > 1:
                await second_entered.wait()
            if outcome == "cancel":
                await first.cancel(request_id, reason="synthetic cancellation")
                caller.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await caller
                while leases[0].active:
                    await asyncio.sleep(0)
            elif outcome == "shutdown":
                await db.close()
                # Manager shutdown owns DB work, not transport reply completion.
                caller.cancel()
                peer.cancel()
            release.set()
            if outcome == "shutdown":
                await asyncio.gather(caller, peer, return_exceptions=True)
            else:
                if outcome != "cancel":
                    assert_seeded_reply(await caller, tool, failed_enrichment=outcome == "error")
                assert_seeded_reply(await peer, tool)
                assert len(leases) == 2 and leases[0] is not leases[1]
        finally:
            release.set()
            for task in (caller, peer):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in (caller, peer) if t is not None), return_exceptions=True)
    await db.close()
    assert all(not lease.active and lease.closed for lease in leases)
    assert not db._owners and not db._workers and not db._jobs
