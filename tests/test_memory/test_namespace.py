"""Real SQLite/vector storage checks for external namespace ownership."""

from __future__ import annotations

import asyncio
import importlib
import threading
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

from genesis.db.connection import get_db
from genesis.db.crud import memory as memory_crud
from genesis.db.schema import create_all_tables
from genesis.memory.namespace import DedupNamespace, _connection, reserve
from genesis.memory.store import MemoryStore


@pytest.fixture
async def harness(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    db = await get_db(path)
    await create_all_tables(db)
    await db.commit()
    vectors = QdrantClient(":memory:")
    for name in ("episodic_memory", "knowledge_base"):
        vectors.create_collection(name, vectors_config=VectorParams(size=1024, distance=Distance.COSINE))
    provider = MagicMock()
    provider.embed = AsyncMock(return_value=[0.1] * 1024)
    provider.tracker = None
    linker = MagicMock()
    linker.auto_link = AsyncMock()
    store = MemoryStore(embedding_provider=provider, qdrant_client=vectors, db=db, linker=linker)
    monkeypatch.setattr("genesis.memory.entity_resolution.normalize_content", lambda text: text)
    try:
        yield path, db, vectors, store, provider, linker
    finally:
        vectors.close()
        await db.close()


async def write(store, content="Shared exact content", peer="muse", **kwargs):
    return await store.store_reporting_creation(
        content, "peer offer", collection="knowledge_base",
        dedup_namespace=DedupNamespace(peer, "knowledge_base"), **kwargs,
    )


async def scalar(db, sql, params=()):
    return (await (await db.execute(sql, params)).fetchone())[0]


async def test_owner_and_each_peer_remain_distinct_in_both_orders(harness):
    _, db, vectors, store, _, linker = harness
    text = "Shared exact content"
    muse, created = await write(store, text)
    other, other_created = await write(store, text, peer="other")
    owner, owner_created = await store.store_reporting_creation(text, "owner", collection="knowledge_base", origin_class="owner", auto_link=False)
    assert created and other_created and owner_created
    assert len({muse, other, owner}) == 3
    assert uuid.UUID(muse).version == 8 and uuid.UUID(owner).version == 4
    assert await write(store, text) == (muse, False)
    assert await store.store_reporting_creation(text, "owner again") == (owner, False)
    assert await scalar(db, "SELECT COUNT(*) FROM memory_fts WHERE content=?", (text,)) == 3
    points = vectors.retrieve("knowledge_base", [muse, other, owner])
    assert {p.id: p.payload["origin_class"] for p in points} == {muse: "external_untrusted", other: "external_untrusted", owner: "owner"}
    linker.auto_link.assert_not_awaited()
    owner_first, _ = await store.store_reporting_creation("Owner first", "owner", origin_class="owner", auto_link=False)
    peer_second, _ = await write(store, "Owner first")
    assert owner_first != peer_second


async def test_exact_bytes_preserved_and_collection_part_of_identity(harness, monkeypatch):
    _, db, _, store, _, _ = harness
    monkeypatch.setattr("genesis.memory.entity_resolution.normalize_content", lambda text: "normalized")
    text = "alias\r\nUnicode café  "
    first, _ = await write(store, text)
    second, _ = await write(store, text.rstrip())
    episodic, _ = await store.store_reporting_creation(text, "peer", dedup_namespace=DedupNamespace("muse", "episodic_memory"))
    assert len({first, second, episodic}) == 3
    assert await scalar(db, "SELECT content FROM memory_fts WHERE memory_id=?", (first,)) == text


@pytest.mark.parametrize("kwargs", [{"origin_class": "owner"}, {"source_subsystem": "ego"}, {"supersedes": "anything"}])
async def test_incompatible_provenance_rejected_before_effect(harness, kwargs):
    _, db, vectors, store, provider, _ = harness
    with pytest.raises(ValueError, match="incompatible"):
        await write(store, **kwargs)
    provider.embed.assert_not_awaited()
    assert await scalar(db, "SELECT COUNT(*) FROM memory_namespaces") == 0
    assert vectors.count("knowledge_base").count == 0


async def test_open_writer_transaction_rejected_without_deadlock(harness):
    _, db, _, store, _, _ = harness
    await db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ValueError, match="open writer"):
            await asyncio.wait_for(write(store), timeout=1)
    finally:
        await db.rollback()


async def test_namespace_lookup_failure_is_closed_ordinary_dedup_still_works(harness):
    _, db, vectors, store, provider, _ = harness
    peer, _ = await write(store)
    owner, _ = await store.store_reporting_creation("Shared exact content", "owner", auto_link=False)
    await db.execute("DROP TABLE memory_namespaces")
    await db.commit()
    provider.embed.reset_mock()
    with pytest.raises(Exception, match="no such table"):
        await write(store, "new content")
    provider.embed.assert_not_awaited()
    assert vectors.count("knowledge_base").count == 1
    assert await memory_crud.find_exact_duplicate(db, content="Shared exact content") == owner
    assert owner != peer


@pytest.mark.parametrize("seam", ["upsert", "create_metadata", "complete"])
async def test_retry_repairs_only_reserved_id_and_retains_compensation_ownership(harness, monkeypatch, seam):
    _, db, vectors, store, _, _ = harness
    owner, _ = await store.store_reporting_creation("Shared exact content", "owner", origin_class="owner", auto_link=False)
    other, _ = await write(store, peer="other")
    if seam == "complete":
        import genesis.memory.namespace as module
    else:
        module = memory_crud
    real = getattr(module, seam)
    monkeypatch.setattr(module, seam, AsyncMock(side_effect=RuntimeError("injected storage failure")))
    with pytest.raises(RuntimeError, match="injected"):
        await write(store)
    reserved = DedupNamespace("muse", "knowledge_base").memory_id("Shared exact content")
    assert await scalar(db, "SELECT status FROM memory_namespaces WHERE memory_id=?", (reserved,)) == "pending"
    monkeypatch.setattr(module, seam, real)
    assert await write(store) == (reserved, False)
    assert await scalar(db, "SELECT COUNT(*) FROM memory_fts WHERE memory_id=?", (reserved,)) == 1
    assert await scalar(db, "SELECT status FROM memory_namespaces WHERE memory_id=?", (reserved,)) == "complete"
    assert vectors.retrieve("episodic_memory", [owner])[0].payload["origin_class"] == "owner"
    assert vectors.retrieve("knowledge_base", [other])[0].payload["origin_class"] == "external_untrusted"


async def test_concurrent_reservations_single_creator_and_cancel_rollback(harness):
    path, db, _, _, _, _ = harness
    ns = DedupNamespace("muse", "knowledge_base")
    rows = await asyncio.gather(*(reserve(path, ns, "race") for _ in range(8)))
    assert len({row[0] for row in rows}) == 1
    assert sum(row[1] for row in rows) == 1
    with pytest.raises(asyncio.CancelledError):
        async with _connection(path) as private:
            await private.execute("DELETE FROM memory_namespaces")
            raise asyncio.CancelledError
    assert await scalar(db, "SELECT COUNT(*) FROM memory_namespaces") == 1


async def test_conflicting_reserved_storage_refused_without_relabel(harness):
    path, db, _, store, _, _ = harness
    ns = DedupNamespace("muse", "knowledge_base")
    mid, _, _ = await reserve(path, ns, "collision")
    await memory_crud.upsert(db, memory_id=mid, content="different content")
    with pytest.raises(ValueError, match="storage conflict"):
        await write(store, "collision")
    assert await scalar(db, "SELECT content FROM memory_fts WHERE memory_id=?", (mid,)) == "different content"


async def test_pending_embedding_retry_cleans_only_own_retry_rows(harness, monkeypatch):
    import genesis.memory.namespace as module
    from genesis.memory.embeddings import EmbeddingUnavailableError

    _, db, _, store, provider, _ = harness
    provider.embed.side_effect = EmbeddingUnavailableError("isolated provider unavailable")
    other, _ = await write(store, "other pending", peer="other")
    real_complete = module.complete
    monkeypatch.setattr(module, "complete", AsyncMock(side_effect=RuntimeError("after pending write")))
    with pytest.raises(RuntimeError, match="after pending"):
        await write(store)
    own = DedupNamespace("muse", "knowledge_base").memory_id("Shared exact content")
    assert await scalar(db, "SELECT COUNT(*) FROM pending_embeddings WHERE memory_id=?", (own,)) == 1
    monkeypatch.setattr(module, "complete", real_complete)
    provider.embed.side_effect = None
    assert await write(store) == (own, False)
    assert await scalar(db, "SELECT embedding_status FROM memory_metadata WHERE memory_id=?", (own,)) == "embedded"
    assert await scalar(db, "SELECT COUNT(*) FROM pending_embeddings WHERE memory_id=?", (own,)) == 0
    assert await scalar(db, "SELECT COUNT(*) FROM pending_embeddings WHERE memory_id=?", (other,)) == 1


async def test_embedding_recovery_keeps_namespace_unlinked_but_links_legacy(harness):
    from genesis.memory.embeddings import EmbeddingUnavailableError
    from genesis.resilience.embedding_recovery import EmbeddingRecoveryWorker

    _, db, vectors, store, provider, linker = harness
    provider.embed.side_effect = EmbeddingUnavailableError("isolated provider unavailable")
    peer, _ = await write(store)
    owner, _ = await store.store_reporting_creation("Owner recovery", "owner")
    provider.embed.side_effect = None
    worker = EmbeddingRecoveryWorker(db=db, embedding_provider=provider, qdrant_client=vectors, linker=linker, pace_per_min=0)
    assert await worker.drain_pending() == 2
    linker.auto_link.assert_awaited_once()
    assert linker.auto_link.await_args.args[0] == owner
    assert vectors.retrieve("knowledge_base", [peer])[0].payload["origin_class"] == "external_untrusted"


async def test_upgrade_schema_matches_fresh_and_runner_owns_transaction(harness):
    _, db, _, _, _, _ = harness
    fresh = await (await db.execute("PRAGMA table_info(memory_namespaces)")).fetchall()
    await db.execute("DROP TABLE memory_namespaces")
    await db.commit()
    migration = importlib.import_module("genesis.db.migrations.20261008044832_memory_namespaces")
    await db.execute("BEGIN IMMEDIATE")
    await migration.up(db)
    await migration.up(db)
    assert db.in_transaction
    upgraded = await (await db.execute("PRAGMA table_info(memory_namespaces)")).fetchall()
    assert [tuple(row) for row in fresh] == [tuple(row) for row in upgraded]
    await db.rollback()
    assert await scalar(db, "SELECT COUNT(*) FROM sqlite_master WHERE name='memory_namespaces'") == 0


def test_namespace_validation_and_uuid_determinism():
    ns = DedupNamespace("muse", "knowledge_base")
    assert ns.memory_id("exact") == DedupNamespace("muse", "knowledge_base").memory_id("exact")
    assert uuid.UUID(ns.memory_id("exact")).version == 8
    for peer, collection, origin in [("", "knowledge_base", "external_untrusted"), ("UPPER", "knowledge_base", "external_untrusted"), ("muse", "../bad", "external_untrusted"), ("muse", "knowledge_base", "owner")]:
        with pytest.raises(ValueError):
            DedupNamespace(peer, collection, origin)


async def test_eight_full_concurrent_writes_have_one_fts_row(harness):
    _, db, vectors, store, _, _ = harness
    rows = await asyncio.gather(*(write(store) for _ in range(8)))
    assert len({row[0] for row in rows}) == 1
    assert sum(row[1] for row in rows) == 1
    assert await scalar(db, "SELECT COUNT(*) FROM memory_fts") == 1
    assert vectors.count("knowledge_base").count == 1


async def test_namespaced_delete_refuses_open_writer_transaction(harness):
    _, db, _, store, _, _ = harness
    mid, _ = await write(store)
    await db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(ValueError, match="open writer"):
            await asyncio.wait_for(store.delete(mid), 1)
    finally:
        await db.rollback()
    assert await scalar(db, "SELECT status FROM memory_namespaces WHERE memory_id=?", (mid,)) == "complete"


async def test_delete_waits_for_paused_retry_then_blocks_all_reoffers(harness, monkeypatch):
    import genesis.memory.namespace as module

    _, db, vectors, store, provider, _ = harness
    real_complete = module.complete
    monkeypatch.setattr(module, "complete", AsyncMock(side_effect=RuntimeError("partial completion")))
    with pytest.raises(RuntimeError):
        await write(store)
    monkeypatch.setattr(module, "complete", real_complete)
    started, release = asyncio.Event(), asyncio.Event()
    async def paused_embed(*args):
        started.set()
        await release.wait()
        return [0.1] * 1024
    provider.embed.side_effect = paused_embed
    retry = asyncio.create_task(write(store))
    await asyncio.wait_for(started.wait(), 5)
    mid = DedupNamespace("muse", "knowledge_base").memory_id("Shared exact content")
    deletion = asyncio.create_task(store.delete(mid))
    await asyncio.sleep(0.05)
    assert not deletion.done()
    release.set()
    assert await retry == (mid, False)
    assert not (await deletion).get("deferred")
    assert await scalar(db, "SELECT status FROM memory_namespaces WHERE memory_id=?", (mid,)) == "deleted"
    assert await scalar(db, "SELECT COUNT(*) FROM memory_fts WHERE memory_id=?", (mid,)) == 0
    with pytest.raises(ValueError, match="owner restoration"):
        await write(store)
    assert vectors.count("knowledge_base").count == 0


async def test_canceled_vector_thread_drains_before_delete_gets_lock(harness, monkeypatch):
    import genesis.memory.store as module

    _, db, vectors, store, _, _ = harness
    started, release = threading.Event(), threading.Event()
    real = module.upsert_point
    def paused_upsert(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return real(*args, **kwargs)
    monkeypatch.setattr(module, "upsert_point", paused_upsert)
    writer = asyncio.create_task(write(store))
    assert await asyncio.to_thread(started.wait, 5)
    writer.cancel()
    mid = DedupNamespace("muse", "knowledge_base").memory_id("Shared exact content")
    deletion = asyncio.create_task(store.delete(mid))
    await asyncio.sleep(0.05)
    assert not writer.done() and not deletion.done()
    writer.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await writer
    assert not (await deletion).get("deferred")
    assert vectors.count("knowledge_base").count == 0
    assert await scalar(db, "SELECT status FROM memory_namespaces WHERE memory_id=?", (mid,)) == "deleted"
