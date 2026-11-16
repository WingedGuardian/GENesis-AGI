"""Shared-vector lifetime controls using actual owned stores."""

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
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



@pytest.mark.parametrize("entry", ["batch", "manual"])
async def test_replacement_preserves_other_committed_references(owned_storage, entry):
    from genesis.memory.knowledge_ingest import ingest_knowledge_unit

    store, db, qdrant = owned_storage
    shared = "identical owned content for two concepts"
    await store_units([KnowledgeUnit(domain="owned", concept=key, body=shared)
                       for key in ["a", "b"]])
    before = await rows(db, "knowledge_units")
    old_id = before[0]["qdrant_id"]
    assert {r["qdrant_id"] for r in before} == {old_id}

    async def replace(key):
        if entry == "batch":
            await store_units([KnowledgeUnit(domain="owned", concept=key, body=f"new owned {key}")])
        else:
            await ingest_knowledge_unit(store=store, db=db, content=f"new owned {key}",
                                        project="owned", domain="owned", concept=key)

    await replace("a")
    after = {r["concept"]: r["qdrant_id"] for r in await rows(db, "knowledge_units")}
    assert after["b"] == old_id and after["a"] != old_id
    assert qdrant.retrieve("knowledge_base", [after["a"]])
    assert qdrant.retrieve("knowledge_base", [old_id]), "surviving unit lost its point"
    await replace("b")
    assert not qdrant.retrieve("knowledge_base", [old_id]), "last-reference point leaked"
    assert all(qdrant.retrieve("knowledge_base", [r["qdrant_id"]])
               for r in await rows(db, "knowledge_units"))


async def test_manual_failed_replacement_preserves_old_point(owned_storage, monkeypatch):
    from genesis.memory.knowledge_ingest import ingest_knowledge_unit

    store, db, qdrant = owned_storage
    await store_units([KnowledgeUnit(domain="owned", concept="a", body="old owned")])
    old_id = (await rows(db, "knowledge_units"))[0]["qdrant_id"]
    monkeypatch.setattr(memory_mcp.knowledge, "upsert", AsyncMock(side_effect=OSError("owned failure")))
    with pytest.raises(OSError):
        await ingest_knowledge_unit(store=store, db=db, content="new owned", project="owned",
                                    domain="owned", concept="a")
    assert qdrant.retrieve("knowledge_base", [old_id]), "precommit cleanup destroyed prior point"


@pytest.mark.parametrize("publisher", ["manual", "batch", "upload", "recon"])
async def test_other_ingest_waits_until_failed_owner_finishes_cleanup(owned_storage, monkeypatch, tmp_path, publisher):
    from genesis.memory.knowledge_ingest import ingest_knowledge_unit

    store, db, qdrant = owned_storage
    parked, release = asyncio.Event(), asyncio.Event()
    actual_db = connection.get_raw_db

    @contextlib.asynccontextmanager
    async def failed_acquisition(path):
        if parked.is_set():
            async with actual_db(path) as own:
                yield own
            return
        parked.set()
        await release.wait()
        raise OSError("owned acquisition failure")
        yield  # pragma: no cover

    body = "owned content published by a competing ingest"
    monkeypatch.setattr(connection, "get_raw_db", failed_acquisition)
    owner = asyncio.create_task(store_units([KnowledgeUnit(domain="owned", concept="a", body=body)]))
    other = None
    try:
        await parked.wait()
        if publisher == "manual":
            work = ingest_knowledge_unit(store=store, db=db, content=body,
                                         project="owned", domain="owned", concept="b")
        elif publisher == "batch":
            work = store_units([KnowledgeUnit(domain="owned", concept="b", body=body)])
        elif publisher == "upload":
            from genesis.knowledge.ingest_upload import _store_as_is
            source = tmp_path / "upload.txt"
            source.write_text(body)
            work = _store_as_is(str(source), source.name, project_type="owned",
                                domain="owned", purpose=None, context="b")
        else:
            from genesis.recon.cc_update_analyzer import CCUpdateAnalyzer
            work = CCUpdateAnalyzer(db=db, memory_store=store)._ingest_to_knowledge(
                "owned", {"details": body, "summary": "b"}, "",
            )
        other = asyncio.create_task(work)
        await asyncio.sleep(0.05)
        assert not other.done(), "competing writer borrowed an uncommitted id"
    finally:
        release.set()
        with pytest.raises(OSError):
            await owner
        monkeypatch.setattr(connection, "get_raw_db", actual_db)
        if other is not None:
            await other
    result = await rows(db, "knowledge_units")
    assert len(result) == 1 and result[0]["concept"] == "b"
    assert qdrant.retrieve("knowledge_base", [result[0]["qdrant_id"]])


async def test_lifetime_lock_is_cross_process_and_cancellation_safe(tmp_path):
    """An independently opened fd contends; cancelled waits never own a lock."""
    import sys

    from genesis.knowledge.lifetime import writer

    database = tmp_path / "owned.db"
    lock_path = tmp_path / "owned.db.knowledge.lock"
    program = (
        "import fcntl,sys; f=open(sys.argv[1], 'a'); "
        "fcntl.flock(f,fcntl.LOCK_EX); print('held',flush=True); sys.stdin.readline()"
    )
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", program, str(lock_path),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )
    waiting = None
    entered = False

    async def acquire():
        nonlocal entered
        async with writer(path=database):
            entered = True

    try:
        assert await child.stdout.readline() == b"held\n"
        inode = lock_path.stat().st_ino
        waiting = asyncio.create_task(acquire())
        await asyncio.sleep(0.05)
        assert not entered and not waiting.done()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        child.kill()  # Kernel must release on process death, no stale-lock cleanup.
        await child.wait()
        await acquire()
        assert entered and lock_path.stat().st_ino == inode
    finally:
        if waiting is not None and not waiting.done():
            waiting.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiting
        if child.returncode is None:
            child.kill()
            await child.wait()


async def test_private_memory_database_lock_serializes_same_facade():
    """A private in-memory connection has no file, but still shares one lock."""
    from genesis.knowledge.lifetime import writer

    async with aiosqlite.connect(":memory:") as db:
        entered = False

        async def acquire():
            nonlocal entered
            async with writer(db):
                entered = True

        async with writer(db):
            task = asyncio.create_task(acquire())
            await asyncio.sleep(0.01)
            assert not entered
        await task
        assert entered


@pytest.mark.parametrize("entry", ["batch", "manual"])
async def test_unknown_references_skip_cleanup_without_losing_committed_result(
    owned_storage, monkeypatch, entry, caplog,
):
    """A failed safety read must preserve both the durable result and old point."""
    from genesis.memory.knowledge_ingest import ingest_knowledge_unit

    store, db, qdrant = owned_storage
    await store_units([KnowledgeUnit(domain="owned", concept="a", body="old owned body")])
    old_id = (await rows(db, "knowledge_units"))[0]["qdrant_id"]
    monkeypatch.setattr(memory_mcp.knowledge, "get_by_qdrant_ids", AsyncMock(
        side_effect=OSError("owned reference lookup fault"),
    ))
    if entry == "batch":
        ids = await store_units([KnowledgeUnit(domain="owned", concept="a", body="new owned body")])
    else:
        ids = [await ingest_knowledge_unit(store=store, db=db, content="new owned body",
                                          project="owned", domain="owned", concept="a")]
    result = (await rows(db, "knowledge_units"))[0]
    assert ids == [result["id"]]
    assert qdrant.retrieve("knowledge_base", [result["qdrant_id"]])
    assert qdrant.retrieve("knowledge_base", [old_id])
    assert "cleanup skipped" in caplog.text
