"""Saved content and completed supersession are separate outcomes."""
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from genesis.db.crud import memory
from genesis.memory import entity_resolution
from genesis.memory import store as store_mod
from genesis.memory.store import MemoryStore
from genesis.perception.confidence import ConfidenceGatesConfig

OLD = "abcd1234-0000-4000-8000-000000000001"
EXISTING = "efab5678-0000-4000-8000-000000000002"


@pytest.mark.parametrize("duplicate", [False, True])
@pytest.mark.parametrize("failure", [None, "qdrant", "link"])
async def test_actual_store_reports_partial_supersession_and_repairs(db, monkeypatch, duplicate, failure):
    monkeypatch.setattr(entity_resolution, "load_aliases", lambda: {})
    monkeypatch.setattr("genesis.perception.confidence.load_config", ConfidenceGatesConfig)
    for mid, text in [(OLD, "prior fact"), *([(EXISTING, "correction")] if duplicate else [])]:
        await memory.create(db, memory_id=mid, content=text)
        await memory.create_metadata(db, memory_id=mid, created_at="2020-01-01T00:00:00+00:00", embedding_status="embedded" if mid == OLD else "fts5_only")
    qdrant = MagicMock()
    qdrant.retrieve.side_effect = lambda **kw: [object()] if kw["collection_name"] == "episodic_memory" else []
    store = MemoryStore(embedding_provider=MagicMock(), qdrant_client=qdrant, db=db)
    update = Mock(side_effect=RuntimeError("owned mirror failure") if failure == "qdrant" else None)
    monkeypatch.setattr(store_mod, "update_payload", update)
    real_link = store_mod.memory_links_crud.create
    if failure == "link":
        monkeypatch.setattr(store_mod.memory_links_crud, "create", AsyncMock(side_effect=RuntimeError("owned link failure")))
    outcome = {"superseded": True, "old_id": "stale", "new_id": "stale"}
    mid, created = await store.store_reporting_creation(
        "correction", "synthetic", force_fts5_only=True,
        supersedes=f"id:{OLD[:8]}", supersede_outcome=outcome,
    )
    assert created is not duplicate
    assert not duplicate or mid == EXISTING
    assert outcome == {"superseded": failure is None, "old_id": OLD, "new_id": mid}
    assert (await memory.get_metadata(db, OLD))["superseded_by"] == mid
    assert len(await db.execute_fetchall("SELECT memory_id FROM memory_fts")) == 2
    if failure:
        assert not await db.execute_fetchall("SELECT * FROM memory_links WHERE link_type='succeeded_by'")
        update.side_effect = None
        monkeypatch.setattr(store_mod.memory_links_crud, "create", real_link)
        await store.supersede(outcome["old_id"], outcome["new_id"])
    links = await db.execute_fetchall("SELECT source_id, target_id FROM memory_links WHERE link_type='succeeded_by'")
    assert [tuple(row) for row in links] == [(OLD, mid)]
    assert len(await db.execute_fetchall("SELECT memory_id FROM memory_fts")) == 2


async def test_rejected_handle_cannot_reuse_confirmed_outcome(db, monkeypatch):
    store = MemoryStore(embedding_provider=MagicMock(), qdrant_client=MagicMock(), db=db)
    outcome = {"superseded": True}
    with pytest.raises(store_mod.SupersedeUnresolved):
        await store.store("correction", "synthetic", supersedes="absent", supersede_outcome=outcome)
    assert outcome == {"superseded": False}
