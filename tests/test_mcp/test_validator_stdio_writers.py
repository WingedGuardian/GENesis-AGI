"""Transport-level writer checks use owned processes, real SQL, and no live services."""

import json
import sqlite3

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError

from tests.test_mcp.validator_stdio_fixture import prepare
from tests.test_scripts.test_pr_verification_closer import _doc


def client(fixture, server):
    config = fixture["servers"][server]
    return Client(StdioTransport(command=config["command"], args=config["args"],
                                 env=fixture["environment"], keep_alive=False))


@pytest.mark.parametrize("recording", [False, True])
async def test_stdio_health_records_or_forces_preview(tmp_path, recording):
    fixture = await prepare(tmp_path, recording=recording)
    async with client(fixture, "health") as mcp:
        assert len(await mcp.list_tools()) == 9
        result = (await mcp.call_tool("pr_verification_close", {
            "pr": 7, "evidence_json": json.dumps(_doc()),
        })).data
        assert result["dry_run"] is (not recording)
        assert result["recorded"] is recording
        created = (await mcp.call_tool("follow_up_create", {
            "content": "Synthetic transport finding", "reason": "Synthetic transport evidence",
            "work_state": "ready", "strategy": "user_input_needed", "request_key": "stdio-finding",
        })).data
        replay = (await mcp.call_tool("follow_up_create", {
            "content": "Synthetic transport finding", "reason": "Synthetic transport evidence",
            "work_state": "ready", "strategy": "user_input_needed", "request_key": "stdio-finding",
        })).data
        assert replay["id"] == created["id"]
        await mcp.call_tool("follow_up_update", {"follow_up_id": created["id"], "status": "completed"})
        listing = (await mcp.call_tool("follow_up_list", {})).data
        assert listing["returned"] == 1
    with sqlite3.connect(fixture["database"]) as reader:
        assert reader.execute("SELECT status, attempt_count FROM pr_verifications").fetchall() == [
            ("closed" if recording else "open", int(recording))
        ]
        assert reader.execute("SELECT source, source_session, status FROM follow_ups").fetchall() == [
            ("codex_interactive", None, "completed")
        ]
        assert reader.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0] == 0


@pytest.mark.parametrize("collection", ["episodic_memory", "knowledge_base"])
async def test_stdio_memory_persists_and_supersedes(tmp_path, collection):
    fixture = await prepare(tmp_path)
    arguments = {"source": "owner-directed synthetic transport measurement", "confidence": 0.95,
                 "memory_class": "fact", "wing": "dev_workflow", "room": "validation", "collection": collection}
    async with client(fixture, "memory") as mcp:
        assert len(await mcp.list_tools()) == 9
        old = (await mcp.call_tool("memory_store", arguments | {"content": "Synthetic first observation"})).data
        new = (await mcp.call_tool("memory_store", arguments | {
            "content": "Synthetic corrected observation", "supersedes": old,
        })).data
        assert old != new
    with sqlite3.connect(fixture["database"]) as reader:
        assert reader.execute("SELECT collection, COUNT(*) FROM memory_fts GROUP BY collection").fetchall() == [(collection, 2)]
        assert reader.execute("SELECT deprecated, superseded_by FROM memory_metadata WHERE memory_id=?", (old,)).fetchone() == (1, new)
    vectors = json.loads((tmp_path / "vectors.json").read_text())
    assert len(vectors) == 2 and next(v for v in vectors if v["id"] == old)["payload"]["deprecated"] is True


@pytest.mark.parametrize("server, count, writer, arguments", [
    ("health", 5, "follow_up_create", {"content": "Must not persist", "reason": "Synthetic evidence",
        "strategy": "user_input_needed", "work_state": "ready", "request_key": "external-floor-control"}),
    ("memory", 8, "memory_store", {"content": "Must not persist", "source": "Synthetic evidence"}),
])
async def test_stdio_external_floor_rejects_direct_writers(tmp_path, server, count, writer, arguments):
    fixture = await prepare(tmp_path, profile="external")
    async with client(fixture, server) as mcp:
        assert len(await mcp.list_tools()) == count
        with pytest.raises(ToolError, match="Tool is unavailable to this external client profile"):
            await mcp.call_tool(writer, arguments)
    with sqlite3.connect(fixture["database"]) as reader:
        assert reader.execute("SELECT COUNT(*) FROM follow_ups").fetchone()[0] == 0
        assert reader.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0] == 0
