"""Real owned-store controls for cancellation before the batch transaction."""

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import anyio
import pytest
from qdrant_client import QdrantClient, models

from genesis.db import connection
from genesis.db.schema import create_all_tables
from genesis.knowledge.distillation import KnowledgeUnit
from genesis.knowledge.orchestrator import _BACKGROUND_COMPENSATIONS, KnowledgeOrchestrator
from genesis.knowledge.processors.base import ProcessedContent
from genesis.mcp import memory_mcp
from genesis.memory.store import MemoryStore


@pytest.fixture
async def owned_storage(tmp_path, monkeypatch):
    """Use actual SQLite, local Qdrant and MemoryStore; replace only embeddings."""
    path = tmp_path / "owned.db"
    raw = await aiosqlite.connect(path)
    raw.row_factory = aiosqlite.Row
    await raw.execute("PRAGMA journal_mode=WAL")
    await create_all_tables(raw)
    await raw.commit()
    db = connection.SerializedConnection(raw, db_path=path)
    qdrant = QdrantClient(location=":memory:", force_disable_check_same_thread=True)
    for name in ("episodic_memory", "knowledge_base"):
        qdrant.create_collection(name, vectors_config=models.VectorParams(
            size=2, distance=models.Distance.COSINE,
        ))
    embeddings = MagicMock(model_name="owned")
    embeddings.embed = AsyncMock(return_value=[0.1, 0.2])
    store = MemoryStore(embedding_provider=embeddings, qdrant_client=qdrant, db=db)
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: path)
    monkeypatch.setenv("GENESIS_DB_PATH", str(path))
    monkeypatch.setattr(memory_mcp, "_require_init", lambda: None)
    monkeypatch.setattr(memory_mcp, "_store", store)
    monkeypatch.setattr(memory_mcp, "_db", db)
    monkeypatch.setattr("genesis.memory.store.record_last_run", AsyncMock())
    try:
        yield store, db, qdrant
    finally:
        if _BACKGROUND_COMPENSATIONS:
            await asyncio.gather(*list(_BACKGROUND_COMPENSATIONS))
        qdrant.close()
        await raw.close()


async def rows(db, table):
    """Read an owned fixture table (names are constants in this module)."""
    return await (await db.execute(f"SELECT * FROM {table}")).fetchall()


async def store_units(units):
    """Exercise the production batch path without distillation or providers."""
    orch = KnowledgeOrchestrator.__new__(KnowledgeOrchestrator)
    return await orch._store_units(units, project_type="owned", source="owned-source",
                                   content=ProcessedContent(text="owned"), purpose=None)


@pytest.mark.parametrize("phase", ["phase1", "acquisition", "batch"])
@pytest.mark.parametrize("failure", ["cancel", "error"])
async def test_partial_ingest_cleans_only_created_ids(owned_storage, monkeypatch, phase, failure):
    """Repeated cancellation must preserve dedup hits and clean every new store."""
    store, db, qdrant = owned_storage
    units = [KnowledgeUnit(domain="owned", concept=f"concept-{i}", body=f"owned body {i}")
             for i in range(3)]
    await store_units(units[:1])
    original = (await rows(db, "knowledge_units"))[0]["qdrant_id"]
    actual_store = store.store_reporting_creation
    actual_upsert = memory_mcp.knowledge.upsert
    calls = 0
    reached = False
    cancelled = False

    async def fault():
        nonlocal reached
        reached = True
        if failure == "error":
            raise OSError("owned boundary failure")
        scope.cancel()
        await anyio.sleep(0)  # Real suspension in a scope that redelivers cancellation.
        pytest.fail("cancellation did not arrive")

    async def observed_store(*args, **kwargs):
        nonlocal calls
        calls += 1
        if phase == "phase1" and calls == 3:
            await fault()
        return await actual_store(*args, **kwargs)

    @contextlib.asynccontextmanager
    async def failed_acquisition(_path):
        await fault()
        yield  # pragma: no cover

    async def failed_upsert(*args, **kwargs):
        result = await actual_upsert(*args, **kwargs)
        await fault()
        return result

    monkeypatch.setattr(store, "store_reporting_creation", observed_store)
    if phase == "acquisition":
        monkeypatch.setattr(connection, "get_raw_db", failed_acquisition)
    if phase == "batch":
        monkeypatch.setattr(memory_mcp.knowledge, "upsert", failed_upsert)
    with anyio.CancelScope() as scope:
        try:
            await store_units(units)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except OSError as exc:
            assert failure == "error" and str(exc) == "owned boundary failure"
        else:
            pytest.fail("boundary fault did not propagate")
    assert reached
    assert cancelled == (failure == "cancel")
    if _BACKGROUND_COMPENSATIONS:
        await asyncio.gather(*list(_BACKGROUND_COMPENSATIONS))
    assert len(await rows(db, "knowledge_units")) == 1
    assert {r["memory_id"] for r in await rows(db, "memory_metadata")} == {original}
    assert len(await rows(db, "memory_fts")) == 1
    assert not await rows(db, "pending_embeddings")
    assert qdrant.count("knowledge_base", exact=True).count == 1
    assert qdrant.retrieve("knowledge_base", [original])


@pytest.mark.parametrize("phase", ["phase1", "acquisition", "batch"])
async def test_cleanup_keeps_request_database_lease_until_finished(owned_storage, monkeypatch, phase):
    """A second task.cancel must not detach deletes from their database lease."""
    from genesis.db.request_database import RequestDatabase

    store, db, qdrant = owned_storage
    path = (await (await db.execute("PRAGMA database_list")).fetchone())[2]
    manager = RequestDatabase(path, max_units=1)  # No background capacity needed.
    monkeypatch.setattr(store, "_db", manager)
    entered = asyncio.Event()
    release = asyncio.Event()
    cleanup_started = asyncio.Event()
    actual_store = store.store_reporting_creation
    actual_delete = store.delete
    actual_upsert = memory_mcp.knowledge.upsert
    calls = 0

    async def park():
        entered.set()
        await asyncio.Event().wait()

    async def observed_store(*args, **kwargs):
        nonlocal calls
        calls += 1
        if phase == "phase1" and calls == 2:
            await park()
        return await actual_store(*args, **kwargs)

    @contextlib.asynccontextmanager
    async def parked_acquisition(_path):
        await park()
        yield  # pragma: no cover

    async def parked_upsert(*args, **kwargs):
        result = await actual_upsert(*args, **kwargs)
        await park()
        return result

    async def observed_delete(mid):
        cleanup_started.set()
        await release.wait()
        return await actual_delete(mid)

    monkeypatch.setattr(store, "store_reporting_creation", observed_store)
    monkeypatch.setattr(store, "delete", observed_delete)
    if phase == "acquisition":
        monkeypatch.setattr(connection, "get_raw_db", parked_acquisition)
    if phase == "batch":
        monkeypatch.setattr(memory_mcp.knowledge, "upsert", parked_upsert)

    async def request():
        async with manager.unit_of_work():
            await store_units([KnowledgeUnit(domain="owned", concept=f"req-{i}",
                                            body=f"request body {i}") for i in range(2)])

    task = asyncio.create_task(request())
    try:
        await entered.wait()
        task.cancel()
        # Allow the initial failure handler to run before cancelling it again.
        # The old pre-envelope handlers return immediately without starting cleanup.
        for _ in range(20):
            await asyncio.sleep(0.01)
            if cleanup_started.is_set() or task.done():
                break
        assert cleanup_started.is_set(), "completed writes were never compensated"
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "cleanup lost its foreground database lifetime"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if _BACKGROUND_COMPENSATIONS:
            await asyncio.gather(*list(_BACKGROUND_COMPENSATIONS))
        await manager.close()
    assert not await rows(db, "knowledge_units")
    assert not await rows(db, "memory_metadata")
    assert qdrant.count("knowledge_base", exact=True).count == 0


@pytest.mark.parametrize("phase", ["phase1", "acquisition"])
async def test_cancelled_owner_finishes_before_competing_ingest_borrows_id(
    owned_storage, monkeypatch, phase,
):
    """The prerequisite mutex must stay held until cancelled cleanup completes."""
    from genesis.memory.knowledge_ingest import ingest_knowledge_unit

    store, db, qdrant = owned_storage
    entered, release = asyncio.Event(), asyncio.Event()
    actual_store = store.store_reporting_creation
    actual_db = connection.get_raw_db
    created = []

    async def observed_store(*args, **kwargs):
        if phase == "phase1" and created:
            entered.set()
            await release.wait()
        result = await actual_store(*args, **kwargs)
        created.append(result[0])
        return result

    @contextlib.asynccontextmanager
    async def parked_acquisition(path):
        entered.set()
        await release.wait()
        async with actual_db(path) as own:
            yield own

    monkeypatch.setattr(store, "store_reporting_creation", observed_store)
    if phase == "acquisition":
        monkeypatch.setattr(connection, "get_raw_db", parked_acquisition)
    body = "owned competing content"
    owner = asyncio.create_task(store_units([
        KnowledgeUnit(domain="owned", concept="owner", body=body),
        *([KnowledgeUnit(domain="owned", concept="never", body="never stored")]
          if phase == "phase1" else []),
    ]))
    other = None
    try:
        await entered.wait()
        # Only the owning batch is parked; the competitor uses the actual store.
        monkeypatch.setattr(store, "store_reporting_creation", actual_store)
        other = asyncio.create_task(ingest_knowledge_unit(
            store=store, db=db, content=body, project="owned", domain="owned", concept="other",
        ))
        await asyncio.sleep(0.05)
        assert not other.done(), "competing ingest escaped the lifetime lock"
        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        await other
    finally:
        release.set()
        if not owner.done():
            owner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await owner
        if other is not None:
            await other
    result = (await rows(db, "knowledge_units"))[0]
    assert result["concept"] == "other"
    assert result["qdrant_id"] != created[0], "owner left its created memory uncompensated"
    assert not qdrant.retrieve("knowledge_base", [created[0]])
    assert qdrant.retrieve("knowledge_base", [result["qdrant_id"]])
