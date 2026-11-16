"""Dedup keeps existing content ownership while applying requested corrections."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.db.crud import memory
from genesis.memory import entity_resolution
from genesis.memory.delete_tombstones import enqueue_tombstone
from genesis.memory.store import MemoryStore, SupersedeUnresolved

OLD = "abcd1234-0000-4000-8000-000000000001"
NEW = "efab5678-0000-4000-8000-000000000002"


@pytest.fixture
async def correction(db, monkeypatch):
    monkeypatch.setattr(entity_resolution, "load_aliases", lambda: {})
    for mid, content in [(OLD, "prior fact"), (NEW, "corrected fact")]:
        await memory.create(db, memory_id=mid, content=content)
        await memory.create_metadata(db, memory_id=mid, created_at="2020-01-01T00:00:00+00:00", embedding_status="fts5_only")
    ep = MagicMock()
    ep.embed = AsyncMock(side_effect=AssertionError("dedup must not embed"))
    return MemoryStore(embedding_provider=ep, qdrant_client=MagicMock(), db=db)


async def test_dedup_correction_uses_full_resolved_pair_without_creating(db, correction):
    assert await correction.store_reporting_creation("corrected fact", "synthetic", supersedes=f"id:{OLD[:8]}") == (NEW, False)
    row = await memory.get_metadata(db, OLD)
    assert (row["deprecated"], row["superseded_by"]) == (1, NEW)
    links = await db.execute_fetchall("SELECT source_id, target_id FROM memory_links WHERE link_type='succeeded_by'")
    assert [tuple(row) for row in links] == [(OLD, NEW)]
    assert len(await db.execute_fetchall("SELECT memory_id FROM memory_fts")) == 2
    correction._embeddings.embed.assert_not_awaited()


async def test_bad_handle_is_rejected_before_even_duplicate_lookup(db, correction, monkeypatch):
    lookup = AsyncMock(return_value=NEW)
    monkeypatch.setattr(memory, "find_exact_duplicate", lookup)
    with pytest.raises(SupersedeUnresolved, match="not_found"):
        await correction.store("corrected fact", "synthetic", supersedes="absent-memory")
    lookup.assert_not_awaited()
    assert not (await memory.get_metadata(db, OLD))["deprecated"]


@pytest.mark.parametrize("damage,reason", [("self", "self_supersede"), ("deprecated", "successor_deprecated"), ("expired", "successor_expired"), ("deleting", "successor_deleting"), ("vanished", "not_found")])
async def test_successor_changed_after_lookup_rejects_without_store(db, correction, monkeypatch, damage, reason):
    async def lookup(*args, **kwargs):
        if damage == "deprecated":
            await db.execute("UPDATE memory_metadata SET deprecated=1 WHERE memory_id=?", (NEW,))
        elif damage == "expired":
            await db.execute("UPDATE memory_metadata SET invalid_at='2000-01-01' WHERE memory_id=?", (NEW,))
        elif damage == "deleting":
            assert await enqueue_tombstone(db, memory_id=NEW, reason="owned test")
        elif damage == "vanished":
            await db.execute("DELETE FROM memory_metadata WHERE memory_id=?", (NEW,))
        return OLD if damage == "self" else NEW

    monkeypatch.setattr(memory, "find_exact_duplicate", lookup)
    mark = AsyncMock(wraps=correction._mark_superseded)
    monkeypatch.setattr(correction, "_mark_superseded", mark)
    with pytest.raises(SupersedeUnresolved) as caught:
        await correction.store("corrected fact", "synthetic", supersedes=OLD)
    assert caught.value.reason == reason
    mark.assert_not_awaited()
    assert not (await memory.get_metadata(db, OLD))["deprecated"]
    assert len(await db.execute_fetchall("SELECT memory_id FROM memory_fts")) == 2


async def test_failed_apply_keeps_existing_id_and_creation_flag(db, correction, monkeypatch):
    monkeypatch.setattr(correction, "_mark_superseded", AsyncMock(side_effect=RuntimeError("owned write failure")))
    assert await correction.store_reporting_creation("corrected fact", "synthetic", supersedes=OLD) == (NEW, False)
    assert await memory.get_metadata(db, NEW) is not None
    assert len(await db.execute_fetchall("SELECT memory_id FROM memory_fts")) == 2


async def test_whitespace_is_not_a_supersession_request(correction, monkeypatch):
    resolve = AsyncMock(side_effect=AssertionError("whitespace is not a handle"))
    monkeypatch.setattr(correction, "_resolve_supersede_target", resolve)
    assert await correction.store("corrected fact", "synthetic", supersedes=" \n ") == NEW
    resolve.assert_not_awaited()
