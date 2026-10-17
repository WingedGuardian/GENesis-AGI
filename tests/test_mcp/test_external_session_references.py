"""External structured transcript references need recorded Genesis targets."""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from genesis.bookmark.manager import BookmarkManager
from genesis.db.crud import session_charters
from genesis.mcp import memory
from genesis.mcp.external_profiles import ExternalProfileMiddleware
from genesis.mcp.health import follow_up_tools

SID = "aaaabbbb-cccc-dddd-eeee-ffff00001111"
UNKNOWN = "12345678-1234-4321-8123-123456789abc"


@pytest.fixture
async def memory_client(db, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    store = MagicMock()
    store.store = AsyncMock(side_effect=lambda *a, **kw: uuid.uuid4().hex)
    store._embeddings.model_name = "synthetic-embedding"
    manager = BookmarkManager(memory_store=store, hybrid_retriever=MagicMock(), db=db)
    monkeypatch.setattr(manager, "_trigger_extraction", AsyncMock())
    for name, value in {"_db": db, "_store": store, "_qdrant": MagicMock(),
                        "_bookmark_mgr": manager, "_require_init": lambda: None}.items():
        monkeypatch.setattr(memory, name, value)
    server = FastMCP("external-reference-controls")
    registered = await memory.mcp.get_tools()
    for name in ("bookmark_shelve", "knowledge_ingest", "reference_store"):
        server.add_tool(registered[name])
    server.add_middleware(ExternalProfileMiddleware("memory", "interactive"))
    async with Client(server) as client:
        yield client, store, manager


async def seed_target(db, store, sid=SID):
    if store == "charter":
        await session_charters.upsert_stub(db, sid)
    elif store == "recorded":
        await db.execute(
            "INSERT INTO cc_sessions(id,session_type,model,started_at,last_activity_at,cc_session_id)"
            " VALUES (?,'foreground','synthetic','t0','t0',?)", ("row-" + sid, sid),
        )
    else:
        await db.execute("INSERT INTO session_heartbeats(cc_session_id,updated_at) VALUES (?,'t0')", (sid,))
    await db.commit()


def arguments(tool, sid):
    if tool == "bookmark_shelve":
        return {"session_id": sid, "context": "Synthetic directed bookmark"}
    if tool == "reference_store":
        return {"kind": "fact", "identifier": "synthetic-reference", "value": "synthetic-value",
                "description": "Synthetic reference only", "source": {"session_id": sid, "captured_via": "fixture"}}
    return {"content": "Synthetic knowledge only", "project": "fixture", "domain": "fixture",
            "provenance": {"session_id": sid, "source_doc": "synthetic-source"}}


@pytest.mark.parametrize("tool", ["bookmark_shelve", "knowledge_ingest", "reference_store"])
@pytest.mark.parametrize("store", ["charter", "recorded", "heartbeat"])
@pytest.mark.parametrize("prefix", [False, True])
async def test_external_known_session_references(memory_client, db, tool, store, prefix):
    client, writer, _ = memory_client
    await seed_target(db, store)
    await client.call_tool(tool, arguments(tool, SID[:8] if prefix else SID))
    if tool == "bookmark_shelve":
        row = await (await db.execute("SELECT cc_session_id FROM session_bookmarks")).fetchone()
        assert row[0] == SID
        assert writer.store.await_args.kwargs["source"] == "session:" + SID
    else:
        assert writer.store.await_args.kwargs["source_session_id"] == SID
        assert (await (await db.execute("SELECT COUNT(*) FROM knowledge_units")).fetchone())[0] == 1


@pytest.mark.parametrize("tool", ["bookmark_shelve", "knowledge_ingest", "reference_store"])
@pytest.mark.parametrize("target", [UNKNOWN, "aaaabbbb", "bad%prefix", "../outside", "f" * 36])
async def test_external_unknown_references_refuse_before_effects(memory_client, db, tool, target):
    client, writer, manager = memory_client
    with pytest.raises(ToolError, match="existing Genesis session"):
        await client.call_tool(tool, arguments(tool, target))
    writer.store.assert_not_awaited()
    manager._trigger_extraction.assert_not_awaited()
    for table in ("session_bookmarks", "knowledge_units"):
        assert (await (await db.execute("SELECT COUNT(*) FROM " + table)).fetchone())[0] == 0


@pytest.mark.parametrize("tool", ["bookmark_shelve", "knowledge_ingest", "reference_store"])
async def test_external_cross_store_ambiguity_and_wrong_namespace(memory_client, db, tool):
    client, writer, _ = memory_client
    await seed_target(db, "charter")
    await seed_target(db, "heartbeat", "aaaabbbb-1111-2222-3333-444455556666")
    await db.execute(
        "INSERT INTO cc_sessions(id,session_type,model,started_at,last_activity_at,cc_session_id)"
        " VALUES (?,'foreground','synthetic','t0','t0',?)", (UNKNOWN, SID),
    )
    await db.commit()
    for target in ("aaaabbbb", UNKNOWN):
        with pytest.raises(ToolError, match="existing Genesis session"):
            await client.call_tool(tool, arguments(tool, target))
    writer.store.assert_not_awaited()


async def test_external_bookmark_rejects_filesystem_only_before_read(memory_client, tmp_path, monkeypatch):
    client, writer, _ = memory_client
    transcript = tmp_path / ".genesis" / "sessions" / SID / "messages.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"text":"synthetic legacy transcript"}\n')
    from pathlib import Path
    original = Path.read_text

    def guarded_read(path, *args, **kwargs):
        assert path != transcript, "Rejected target transcript was read"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    with pytest.raises(ToolError, match="existing Genesis session"):
        await client.call_tool("bookmark_shelve", arguments("bookmark_shelve", SID))
    writer.store.assert_not_awaited()


async def test_ordinary_bookmark_keeps_filesystem_resolution(memory_client, tmp_path):
    _, writer, _ = memory_client
    (tmp_path / ".genesis" / "sessions" / SID).mkdir(parents=True)
    tool = (await memory.mcp.get_tools())["bookmark_shelve"]
    await tool.fn(**arguments("bookmark_shelve", SID[:8]))
    assert writer.store.await_args.kwargs["source"] == "session:" + SID


@pytest.mark.parametrize("tool", ["knowledge_ingest", "reference_store"])
@pytest.mark.parametrize("sid", [None, "", 0, 7, {}, [], {"unknown": "value"}, ["unknown"]])
async def test_external_structured_metadata_types(memory_client, tool, sid):
    client, writer, _ = memory_client
    if sid is None or (tool == "reference_store" and not sid):
        await client.call_tool(tool, arguments(tool, sid))
        assert writer.store.await_args.kwargs["source_session_id"] is None
    else:
        with pytest.raises(ToolError, match="existing Genesis session"):
            await client.call_tool(tool, arguments(tool, sid))
        writer.store.assert_not_awaited()


@pytest.mark.parametrize("store", ["charter", "recorded", "heartbeat"])
@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("target", [UNKNOWN, "bad%prefix", "", SID[:8]])
async def test_followup_provenance_boundary(db, monkeypatch, external, target, store):
    monkeypatch.delenv("GENESIS_CC_SESSION", raising=False)
    monkeypatch.setattr(follow_up_tools, "_get_db", lambda: db)
    await seed_target(db, store)
    from genesis.db.crud import follow_ups
    monkeypatch.setattr(follow_ups, "get_session_id", lambda: UNKNOWN)
    server = FastMCP("followup-reference-control")
    server.add_tool((await follow_up_tools.mcp.get_tools())["follow_up_create"])
    if external:
        server.add_middleware(ExternalProfileMiddleware("health", "interactive"))
    async with Client(server) as client:
        result = await client.call_tool("follow_up_create", {
            "content": "Synthetic follow-up", "reason": "directed", "strategy": "ego_judgment",
            "work_state": "ready", "source_session": target,
        })
    row = await (await db.execute("SELECT source_session FROM follow_ups WHERE id=?", (result.data["id"],))).fetchone()
    expected = SID if target == SID[:8] else UNKNOWN if target == UNKNOWN and not external else None
    assert result.data["source_session"] == row[0] == expected
    if external and target == UNKNOWN:
        assert "existing Genesis session" in result.data["message"]


@pytest.mark.parametrize("tool", ["knowledge_ingest", "reference_store"])
async def test_ordinary_knowledge_keeps_unregistered_provenance(memory_client, tool):
    _, writer, _ = memory_client
    await (await memory.mcp.get_tools())[tool].fn(**arguments(tool, UNKNOWN))
    assert writer.store.await_args.kwargs["source_session_id"] == UNKNOWN


@pytest.mark.parametrize("tool", ["knowledge_ingest", "reference_store"])
async def test_external_normalization_does_not_mutate_input(memory_client, db, tool):
    from genesis.mcp.external_profiles import _CALL_PROFILE, ExternalProfile

    _, writer, _ = memory_client
    await seed_target(db, "heartbeat")
    payload = arguments(tool, SID[:8])
    original = payload["source" if tool == "reference_store" else "provenance"]
    token = _CALL_PROFILE.set(ExternalProfile.INTERACTIVE)
    try:
        await (await memory.mcp.get_tools())[tool].fn(**payload)
    finally:
        _CALL_PROFILE.reset(token)
    assert original["session_id"] == SID[:8]
    assert writer.store.await_args.kwargs["source_session_id"] == SID


@pytest.mark.parametrize("tool", ["bookmark_shelve", "knowledge_ingest", "reference_store"])
async def test_external_duplicate_identity_and_whitespace(memory_client, db, tool):
    client, writer, _ = memory_client
    for store in ("charter", "recorded", "heartbeat"):
        await seed_target(db, store)
    await client.call_tool(tool, arguments(tool, " " + SID[:8] + " "))
    assert writer.store.await_count == 1


@pytest.mark.parametrize("tool", ["bookmark_shelve", "knowledge_ingest", "reference_store"])
async def test_external_lookup_failure_precedes_storage(memory_client, monkeypatch, tool):
    client, writer, manager = memory_client
    monkeypatch.setattr(session_charters, "session_exists", AsyncMock(side_effect=RuntimeError("synthetic lookup unavailable")))
    with pytest.raises(ToolError, match="synthetic lookup unavailable"):
        await client.call_tool(tool, arguments(tool, SID))
    writer.store.assert_not_awaited()
    manager._trigger_extraction.assert_not_awaited()
