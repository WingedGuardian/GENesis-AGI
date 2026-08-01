"""Delete-intent tombstones: module lifecycle, store wiring, atomic requeue guard.

The cross-process half of the delete-vs-repair story (PR-2): a deferred delete
records a DB-backed tombstone; requeue/re-embed refuse tombstoned ids; a later
successful delete closes the intent. Uses the real in-memory ``db`` fixture
(full schema, SerializedConnection + Row factory — same as production).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.db.crud import deferred_work as deferred_crud
from genesis.db.crud import memory as memory_crud
from genesis.db.crud import pending_embeddings as pending_crud
from genesis.memory import delete_tombstones as dt
from genesis.memory.store import MemoryStore


def _store(db):
    ep = MagicMock()
    ep.embed = AsyncMock(return_value=[0.1] * 1024)
    ep.enrich = MagicMock(return_value="episodic: x")
    return MemoryStore(
        embedding_provider=ep,
        qdrant_client=MagicMock(),
        db=db,
        linker=None,
    )


class TestTombstoneModule:
    @pytest.mark.asyncio
    async def test_enqueue_dedup_and_close_lifecycle(self, db):
        assert await dt.enqueue_tombstone(db, memory_id="mem-t1", reason="test") is True
        assert await dt.has_open_tombstone(db, memory_id="mem-t1") is True

        # Second enqueue for the same id dedups to the open row — still True:
        # the intent IS durably recorded, just not by a new row.
        assert await dt.enqueue_tombstone(db, memory_id="mem-t1", reason="test") is True
        rows = await deferred_crud.query_pending(db, work_type=dt.WORK_TYPE)
        assert len(rows) == 1

        # Different id is independent.
        assert await dt.enqueue_tombstone(db, memory_id="mem-t2", reason="test") is True

        closed = await dt.complete_open_tombstones(db, memory_id="mem-t1")
        assert closed == 1
        assert await dt.has_open_tombstone(db, memory_id="mem-t1") is False
        assert await dt.has_open_tombstone(db, memory_id="mem-t2") is True

    @pytest.mark.asyncio
    async def test_enqueue_never_raises(self, db, monkeypatch):
        async def _boom(*a, **k):
            raise RuntimeError("db exploded")

        monkeypatch.setattr(deferred_crud, "count_open_by_identity", _boom)
        assert await dt.enqueue_tombstone(db, memory_id="mem-x", reason="test") is False

    def test_memory_id_from_row(self):
        assert dt.memory_id_from_row({"payload_json": '{"memory_id": "m1"}'}) == "m1"
        assert dt.memory_id_from_row({"payload_json": '{"topic": "m2"}'}) == "m2"
        assert dt.memory_id_from_row({"payload_json": "not json"}) is None
        assert dt.memory_id_from_row({"payload_json": None}) is None


class TestStoreDeleteTombstones:
    @pytest.mark.asyncio
    async def test_deferred_delete_records_tombstone(self, db):
        store = _store(db)
        store._qdrant.retrieve = MagicMock(side_effect=RuntimeError("qdrant down"))
        result = await store.delete("mem-def")
        assert result["deferred"] is True
        assert result["tombstoned"] is True
        assert await dt.has_open_tombstone(db, memory_id="mem-def") is True

    @pytest.mark.asyncio
    async def test_successful_delete_closes_open_tombstone(self, db):
        await dt.enqueue_tombstone(db, memory_id="mem-ok", reason="earlier defer")
        store = _store(db)
        store._qdrant.retrieve = MagicMock(return_value=[])  # point already absent
        result = await store.delete("mem-ok")
        assert not result.get("deferred")
        assert await dt.has_open_tombstone(db, memory_id="mem-ok") is False


class TestAtomicRequeueGuard:
    """requeue_for_reembed carries the metadata-exists + no-open-tombstone guard
    INSIDE each write statement — the cross-process close of the Codex R4 race."""

    async def _seed(self, db, mid: str):
        await memory_crud.create(db, memory_id=mid, content=f"content {mid}")
        await memory_crud.create_metadata(db, memory_id=mid, created_at="2026-03-11T12:00:00")
        await db.execute(
            "UPDATE memory_metadata SET embedding_status = 'embedded' WHERE memory_id = ?",
            (mid,),
        )
        await db.commit()

    async def _requeue(self, db, mid: str) -> bool:
        return await pending_crud.requeue_for_reembed(
            db,
            memory_id=mid,
            content="c",
            tags=None,
            memory_type="episodic",
            collection="episodic_memory",
            created_at="2026-03-11T12:00:00",
            confidence=0.5,
        )

    @pytest.mark.asyncio
    async def test_requeue_normal_path_true(self, db):
        await self._seed(db, "mem-rq1")
        assert await self._requeue(db, "mem-rq1") is True
        rows = await db.execute_fetchall(
            "SELECT status FROM pending_embeddings WHERE memory_id = 'mem-rq1'"
        )
        assert [r[0] for r in rows] == ["pending"]
        meta = await db.execute_fetchall(
            "SELECT embedding_status FROM memory_metadata WHERE memory_id = 'mem-rq1'"
        )
        assert meta[0][0] == "pending"

    @pytest.mark.asyncio
    async def test_requeue_refused_when_metadata_gone(self, db):
        # Simulates the cross-process delete winning after the caller's re-read.
        assert await self._requeue(db, "mem-gone") is False
        rows = await db.execute_fetchall(
            "SELECT 1 FROM pending_embeddings WHERE memory_id = 'mem-gone'"
        )
        assert rows == []

    @pytest.mark.asyncio
    async def test_requeue_refused_when_tombstoned(self, db):
        await self._seed(db, "mem-ts")
        await dt.enqueue_tombstone(db, memory_id="mem-ts", reason="deferred delete")
        assert await self._requeue(db, "mem-ts") is False
        rows = await db.execute_fetchall(
            "SELECT 1 FROM pending_embeddings WHERE memory_id = 'mem-ts'"
        )
        assert rows == []
        # Mirror must NOT have been flipped for a refused requeue.
        meta = await db.execute_fetchall(
            "SELECT embedding_status FROM memory_metadata WHERE memory_id = 'mem-ts'"
        )
        assert meta[0][0] == "embedded"

    @pytest.mark.asyncio
    async def test_requeue_existing_row_not_reset_when_tombstoned(self, db):
        await self._seed(db, "mem-row")
        await pending_crud.create(
            db,
            id="pe-row",
            memory_id="mem-row",
            content="old",
            memory_type="episodic",
            collection="episodic_memory",
            created_at="2026-03-11T12:00:00",
            status="failed",
        )
        await dt.enqueue_tombstone(db, memory_id="mem-row", reason="deferred delete")
        assert await self._requeue(db, "mem-row") is False
        rows = await db.execute_fetchall(
            "SELECT status, content FROM pending_embeddings WHERE memory_id = 'mem-row'"
        )
        assert [(r[0], r[1]) for r in rows] == [("failed", "old")]


class TestMetadataMissingIds:
    """metadata_missing_ids is the recall ghost detector — real-DB unit test."""

    @pytest.mark.asyncio
    async def test_partitions_present_and_missing(self, db):
        from genesis.memory.retrieval import metadata_missing_ids

        await memory_crud.create_metadata(
            db, memory_id="mm-present", created_at="2026-03-11T12:00:00"
        )
        missing = await metadata_missing_ids(db, {"mm-present", "mm-ghost-a", "mm-ghost-b"})
        assert missing == {"mm-ghost-a", "mm-ghost-b"}
        assert await metadata_missing_ids(db, set()) == set()
