"""Actual store + owned SQLite coverage for normalized duplicate identity."""
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from genesis.db.crud import memory
from genesis.memory import entity_resolution
from genesis.memory.store import MemoryStore
from genesis.perception.confidence import ConfidenceGatesConfig


@pytest.fixture
def owned_store(db, monkeypatch):
    monkeypatch.setattr(entity_resolution, "load_aliases", lambda: {"CC": "Claude Code", "équipe": "Team"})
    normalize = Mock(wraps=entity_resolution.normalize_content)
    monkeypatch.setattr(entity_resolution, "normalize_content", normalize)
    monkeypatch.setattr("genesis.perception.confidence.load_config", ConfidenceGatesConfig)
    embeddings = MagicMock()
    embeddings.embed = AsyncMock(side_effect=AssertionError("unexpected embedding"))
    return MemoryStore(embedding_provider=embeddings, qdrant_client=MagicMock(), db=db), normalize


@pytest.mark.parametrize("text,canonical", [("CC helps", "Claude Code helps"), ("cc helps", "Claude Code helps"), ("ÉQUIPE 東京", "Team 東京"), ("unchanged", "unchanged")])
async def test_actual_store_normalizes_once_before_repeated_lookup(db, owned_store, text, canonical):
    store, normalize = owned_store
    first, created = await store.store_reporting_creation(text, "synthetic", force_fts5_only=True)
    assert created is True
    second, created = await store.store_reporting_creation(text, "synthetic", force_fts5_only=True)
    assert (second, created) == (first, False)
    assert normalize.call_count == 2
    rows = await db.execute_fetchall("SELECT content FROM memory_fts")
    assert [row[0] for row in rows] == [canonical]
    assert await store.store(text, "synthetic", force_fts5_only=True) == first
    store._embeddings.embed.assert_not_awaited()


async def test_normalized_match_precedes_legacy_raw_fallback(db, owned_store):
    store, normalize = owned_store
    await memory.create(db, memory_id="raw", content="CC helps")
    assert await store.store_reporting_creation("CC helps", "synthetic") == ("raw", False)
    await memory.create(db, memory_id="canonical", content="Claude Code helps")
    assert await store.store_reporting_creation("CC helps", "synthetic") == ("canonical", False)
    assert normalize.call_count == 2


async def test_failed_normalization_preserves_raw_dedup(db, owned_store):
    store, normalize = owned_store
    normalize.side_effect = RuntimeError("owned alias failure")
    await memory.create(db, memory_id="raw", content="CC helps")
    assert await store.store_reporting_creation("CC helps", "synthetic") == ("raw", False)
    normalize.assert_called_once_with("CC helps")


@pytest.mark.parametrize("scope", [None, "worker", ""])
async def test_raw_fallback_keeps_the_same_visibility_scope(db, owned_store, scope):
    store, _ = owned_store
    for mid, subsystem in [("other", "unrelated"), ("own", scope)]:
        await memory.create(db, memory_id=mid, content="CC helps")
        await memory.create_metadata(
            db, memory_id=mid, created_at="2020-01-01T00:00:00+00:00",
            source_subsystem=subsystem,
        )
    assert await store.store_reporting_creation(
        "CC helps", "synthetic", source_subsystem=scope,
    ) == ("own", False)


async def test_unchanged_text_looks_up_once(db, owned_store, monkeypatch):
    store, _ = owned_store
    lookup = AsyncMock(wraps=memory.find_exact_duplicate)
    monkeypatch.setattr(memory, "find_exact_duplicate", lookup)
    await store.store("unchanged", "synthetic", force_fts5_only=True)
    lookup.assert_awaited_once_with(db, content="unchanged", source_subsystem=None)


async def test_actual_store_dedup_keeps_subsystem_and_user_identities_separate(db, owned_store):
    store, _ = owned_store
    ids = []
    for subsystem in [None, "worker", "other", ""]:
        mid, created = await store.store_reporting_creation("CC helps", "synthetic", source_subsystem=subsystem, force_fts5_only=True)
        assert created
        ids.append(mid)
        assert await store.store_reporting_creation("CC helps", "synthetic", source_subsystem=subsystem) == (mid, False)
    assert len(set(ids)) == 4
