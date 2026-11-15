"""Real commit/teardown/manifest controls using only owned storage."""

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



@pytest.mark.parametrize("boundary", ["close", "normal", "acquisition", "write"])
async def test_ingest_result_and_manifest_match_durable_batch(
    owned_storage, tmp_path, monkeypatch, caplog, boundary,
):
    from genesis.knowledge.manifest import ManifestManager
    from genesis.knowledge.processors.registry import ContentProcessorRegistry
    from genesis.knowledge.processors.text import TextProcessor

    store, db, qdrant = owned_storage
    units = [KnowledgeUnit(domain="owned", concept=f"concept-{i}", body=f"owned body {i}")
             for i in range(3)]
    registry = ContentProcessorRegistry()
    registry.register_extensions(TextProcessor(), [".txt"])
    distillation = MagicMock(last_extraction_ratio=1.0)
    distillation.distill = AsyncMock(return_value=units)
    manifest = ManifestManager(root=tmp_path / "manifest")
    orch = KnowledgeOrchestrator(registry=registry, distillation=distillation, manifest=manifest)
    source = tmp_path / "source.txt"
    source.write_text("Owned source with three deterministic extracted units.")
    actual_db = connection.get_raw_db

    @contextlib.asynccontextmanager
    async def observed_connection(path):
        if boundary == "acquisition":
            raise OSError("owned acquisition fault")
        async with actual_db(path) as own:
            yield own
        if boundary == "close":
            raise OSError("owned postcommit close fault")

    monkeypatch.setattr(connection, "get_raw_db", observed_connection)
    if boundary == "write":
        monkeypatch.setattr(memory_mcp.knowledge, "upsert", AsyncMock(
            side_effect=OSError("owned write fault"),
        ))
    result = await orch.ingest_source(str(source), project_type="owned")
    durable = await rows(db, "knowledge_units")
    if boundary in {"acquisition", "write"}:
        assert result.error and result.units_created == 0
        assert not durable
        assert not manifest.has_source(str(source))
        assert qdrant.count("knowledge_base", exact=True).count == 0
        return
    assert result.error is None
    assert result.units_created == len(durable) == 3
    ids = {row["id"] for row in durable}
    assert set(result.unit_ids) == ids
    # Re-open from disk, rather than relying on the manager's in-memory cache.
    assert set(ManifestManager(root=manifest.root).get_units_for_source(str(source))) == ids
    assert qdrant.count("knowledge_base", exact=True).count == 3
    if boundary == "close":
        assert "storage_teardown_failed" in result.quality_flags
        assert "owned postcommit close fault" in caplog.text
    else:
        assert "storage_teardown_failed" not in result.quality_flags
    again = await orch.ingest_source(str(source), project_type="owned")
    assert "duplicate_source" in again.quality_flags
    assert set(again.unit_ids) == ids
    assert distillation.distill.await_count == 1


@pytest.mark.parametrize("close_fails", [False, True])
async def test_postcommit_cancellation_never_compensates_durable_vectors(
    owned_storage, monkeypatch, close_fails,
):
    """Cancelling old-vector cleanup must preserve the replacement batch."""
    _store, db, qdrant = owned_storage
    old = KnowledgeUnit(domain="owned", concept="same", body="old owned body")
    await store_units([old])
    old_id = (await rows(db, "knowledge_units"))[0]["qdrant_id"]
    replacement = KnowledgeUnit(domain="owned", concept="same", body="new owned body")
    reached = False

    def interrupted_delete(*args, **kwargs):
        nonlocal reached
        reached = True
        assert kwargs["point_id"] == old_id
        raise asyncio.CancelledError("owned postcommit cancellation")

    if close_fails:
        actual_db = connection.get_raw_db

        @contextlib.asynccontextmanager
        async def close_error(path):
            try:
                async with actual_db(path) as own:
                    yield own
            finally:
                raise OSError("owned teardown error during cancellation")

        monkeypatch.setattr(connection, "get_raw_db", close_error)
    monkeypatch.setattr("genesis.qdrant.collections.delete_point", interrupted_delete)
    with pytest.raises(asyncio.CancelledError):
        await store_units([replacement])
    assert reached
    row = (await rows(db, "knowledge_units"))[0]
    assert row["qdrant_id"] != old_id
    assert qdrant.retrieve("knowledge_base", [row["qdrant_id"]]), "durable batch compensated"
