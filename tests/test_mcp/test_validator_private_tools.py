"""Actual private tool routing uses owned stores and constructor-selected roles."""

import sqlite3

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from genesis.db.crud import follow_ups
from genesis.mcp.external_profiles import ExternalProfileMiddleware
from tests.test_mcp.test_validator_verification_writer import recording as recording


async def client_server(profile="validator"):
    from genesis.mcp.health import mcp

    source = {tool.name: tool for tool in await mcp.list_tools()}
    server = FastMCP("owned-private-tools")
    for name in ("follow_up_create", "follow_up_list", "follow_up_update"):
        server.add_tool(source[name])
    server.add_middleware(ExternalProfileMiddleware("health", profile))
    return server


CREATE = dict(content="Synthetic finding", reason="Synthetic evidence", strategy="user_input_needed",
              work_state="ready", request_key="owned-finding")


def stored(path, fid):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute("SELECT * FROM follow_ups WHERE id=?", (fid,)).fetchone())


async def test_actual_mcp_create_replay_cross_client_update_and_private_listing(recording):
    path, shared, _ = recording
    foreign = await follow_ups.create(
        shared, source="foreground_session", source_session="existing-owner", content="Synthetic cross-client",
        strategy="user_input_needed",
    )
    await follow_ups.create(shared, source="ego", content="Unrelated task", strategy="ego_judgment")
    async with Client(await client_server()) as client:
        created = (await client.call_tool("follow_up_create", CREATE)).data
        fid = created["id"]
        actual = stored(path, fid)
        assert actual["source"] == "codex_interactive" and actual["source_session"] is None
        assert actual["strategy"] == "user_input_needed" and actual["pinned"] == 0
        assert all(actual[k] is None for k in ("linked_task_id", "scheduled_at", "goal_id"))
        assert (await client.call_tool("follow_up_update", dict(
            follow_up_id=foreign[:8], status="completed", resolution_notes="Synthetic measurement",
        ))).data["updated"] is True
        assert stored(path, foreign)["status"] == "completed"
        await client.call_tool("follow_up_update", dict(follow_up_id=fid, status="completed"))
        replay = (await client.call_tool("follow_up_create", CREATE)).data
        assert replay["id"] == fid and replay["status"] == "completed"
        listing = (await client.call_tool("follow_up_list", {"limit": 1})).data
        assert listing["returned"] == 1 and listing["limit"] == 1 and listing["limit_hit"] is True
        assert listing["counts"] == {"completed": 2}
        assert listing["items"][0]["strategy"] == "user_input_needed"
    async with Client(await client_server("external")) as client:
        assert await client.list_tools() == []
        with pytest.raises(ToolError, match="unavailable"):
            await client.call_tool("follow_up_create", CREATE | {"request_key": "external-attempt"})
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM follow_ups").fetchone()[0] == 3


@pytest.mark.parametrize("forbidden", [
    {"strategy": "surplus_task"}, {"strategy": "scheduled_task"},
    {"scheduled_at": "2030-01-01"}, {"pinned": True}, {"source_session": "unrelated-session"},
])
async def test_validator_create_rejects_dispatch_and_provenance_authority(recording, forbidden):
    path, _, _ = recording
    async with Client(await client_server()) as client:
        with pytest.raises(ToolError):
            await client.call_tool("follow_up_create", CREATE | forbidden)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM follow_ups").fetchone()[0] == 0


@pytest.mark.parametrize("forbidden", [
    {"pinned": "true"}, {"pinned": "false"}, {"pinned": "not-a-boolean"},
    {"work_state": "ready"}, {"revisit_condition": "Changed trigger"}, {"status": "scheduled"},
])
async def test_validator_update_checks_raw_authority_before_pin_normalization(recording, forbidden):
    path, _, _ = recording
    async with Client(await client_server()) as client:
        fid = (await client.call_tool("follow_up_create", CREATE)).data["id"]
        before = stored(path, fid)
        with pytest.raises(ToolError):
            await client.call_tool("follow_up_update", {"follow_up_id": fid} | forbidden)
        assert stored(path, fid) == before


@pytest.mark.parametrize("name,args", [
    ("follow_up_create", CREATE), ("follow_up_list", {}),
    ("follow_up_update", {"follow_up_id": "0" * 32, "resolution_notes": "Synthetic"}),
])
async def test_task_augmented_calls_cannot_bypass_profile_or_private_routing(recording, name, args):
    # FastMCP 4 runs background tasks only through the tasks extension, which
    # Genesis does not install: every tool forbids task mode, and task metadata
    # on a call starts no task, so the call meets the profile like any other.
    from genesis.mcp.health import mcp

    path, _, _ = recording
    assert (await mcp.get_tool(name)).task_config.mode == "forbidden"
    async with Client(await client_server("external")) as client:
        result = await client.call_tool_mcp(
            name, args, meta={"modelcontextprotocol.io/task": {"ttl": 60000}},
        )
        assert result.is_error is True
        assert "modelcontextprotocol.io/task" not in (result.meta or {})
        assert "unavailable to this external client profile" in result.content[0].text
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM follow_ups").fetchone()[0] == 0


async def test_foreign_pending_transaction_is_not_committed_by_private_writer(recording):
    path, shared, _ = recording
    fid = await follow_ups.create(
        shared, source="foreground_session", content="Foreign pending", strategy="user_input_needed",
    )
    await shared.execute("UPDATE follow_ups SET priority='high' WHERE id=?", (fid,))
    try:
        async with Client(await client_server()) as client:
            with pytest.raises(ToolError):
                await client.call_tool("follow_up_create", CREATE)
        assert shared.in_transaction
        assert stored(path, fid)["priority"] == "medium"
    finally:
        await shared.rollback()
    assert stored(path, fid)["priority"] == "medium"


@pytest.mark.parametrize("missing", [False, True])
async def test_known_create_completion_survives_readback_failure(recording, monkeypatch, missing):
    from genesis.mcp.health import validator_writes

    path, shared, _ = recording
    original = follow_ups.get_by_id

    async def failed_read(*args, **kwargs):
        if missing:
            return None
        raise OSError("Synthetic postcommit read failure")

    monkeypatch.setattr(follow_ups, "get_by_id", failed_read)
    args = dict(request_key="readback-failure", content="Synthetic finding",
                reason="Synthetic evidence", work_state="ready")
    try:
        result = await validator_writes.private_create(shared, **args)
    except Exception as exc:
        with sqlite3.connect(path) as fresh:
            count = fresh.execute("SELECT COUNT(*) FROM follow_ups WHERE dedup_key='codex_private:readback-failure'").fetchone()[0]
        pytest.fail(f"Lost known completion after {type(exc).__name__}; fresh reader sees {count} retained row")
    with sqlite3.connect(path) as fresh:
        fid = fresh.execute("SELECT id FROM follow_ups WHERE dedup_key='codex_private:readback-failure'").fetchone()[0]
    assert result["id"] == fid and result["warning"]
    assert "status" not in result or result["status"] is None
    monkeypatch.setattr(follow_ups, "get_by_id", original)
    replay = await validator_writes.private_create(shared, **args)
    assert replay["id"] == fid and replay["status"] == "pending"
