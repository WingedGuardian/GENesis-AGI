"""Cross-store integrity: delete completeness + FTS↔metadata drift tripwire.

Uses the real in-memory ``db`` fixture (full schema) so the cross-store
cascades and COUNT queries run against actual tables.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import genesis.memory.store as store_mod
from genesis.db.crud import entities as entities_crud
from genesis.db.crud import memory as memory_crud
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


class TestDeleteCascadesEntityMentions:
    """F4: delete() must cascade entity_mentions or leave dangling rows."""

    @pytest.mark.asyncio
    async def test_delete_removes_mentions(self, db):
        eid = await entities_crud.create_entity(
            db,
            name="Qdrant",
            norm_name="qdrant",
            entity_type="product",
        )
        await entities_crud.upsert_mention(
            db,
            memory_id="mem-del",
            entity_id=eid,
            provenance="EXTRACTED",
            confidence=0.9,
        )
        before = await db.execute_fetchall(
            "SELECT COUNT(*) FROM entity_mentions WHERE memory_id = ?",
            ("mem-del",),
        )
        assert before[0][0] == 1

        results = await _store(db).delete("mem-del")

        assert results["mentions_deleted"] == 1
        after = await db.execute_fetchall(
            "SELECT COUNT(*) FROM entity_mentions WHERE memory_id = ?",
            ("mem-del",),
        )
        assert after[0][0] == 0


class TestFtsMetadataDrift:
    """F6: the GC tripwire counts BOTH orphan directions."""

    @pytest.mark.asyncio
    async def test_counts_both_orphan_directions(self, db):
        g0, i0 = await memory_crud.count_fts_metadata_drift(db)

        # ghost: an FTS row with no metadata row
        await memory_crud.create(db, memory_id="ghost-1", content="ghost content")
        # invisible: a metadata row with no FTS row
        await memory_crud.create_metadata(
            db,
            memory_id="invisible-1",
            created_at="2026-03-11T12:00:00",
        )
        # healthy: both stores agree — contributes to neither count
        await memory_crud.create(db, memory_id="ok-1", content="ok content")
        await memory_crud.create_metadata(
            db,
            memory_id="ok-1",
            created_at="2026-03-11T12:00:00",
        )

        ghosts, invisible = await memory_crud.count_fts_metadata_drift(db)
        assert ghosts == g0 + 1
        assert invisible == i0 + 1


class TestHotPathOffloadedToThread:
    """D6(b): the hot-path sync Qdrant calls run via asyncio.to_thread so a
    slow round-trip doesn't stall the event loop."""

    @pytest.mark.asyncio
    async def test_delete_offloads_qdrant_delete(self, db):
        seen = []
        orig = asyncio.to_thread

        async def spy(func, *args, **kwargs):
            seen.append(func)
            return await orig(func, *args, **kwargs)

        with (
            patch.object(store_mod, "delete_point") as mock_del,
            patch.object(store_mod.asyncio, "to_thread", spy),
        ):
            await _store(db).delete("mem-none")

        # delete_point dispatched through to_thread (both collections)
        assert mock_del in seen
