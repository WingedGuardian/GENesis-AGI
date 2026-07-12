"""WS-3 B1 end-to-end: real migration chain + real config + real emit + prune.

Unlike the unit tests (which monkeypatch ``gate_mode``), this drives the ACTUAL
``config/ws3_immunity.yaml`` through ``security.immunity.gate_mode`` and the
real ``MigrationRunner``, proving the shadow injection gate flows data
end-to-end with no mocks.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import immunity_shadow as crud
from genesis.db.migrations.runner import MigrationRunner
from genesis.security import immunity, immunity_shadow


@pytest.fixture(autouse=True)
def _reset_caches():
    crud._table_verified = False
    crud._table_verified_sync = False
    yield
    crud._table_verified = False
    crud._table_verified_sync = False


async def _migrated(path) -> aiosqlite.Connection:
    db = await aiosqlite.connect(str(path))
    db.row_factory = aiosqlite.Row
    await MigrationRunner(db).run_pending()
    return db


@pytest.mark.asyncio
async def test_full_migration_chain_creates_table(tmp_path):
    """If the system migrates from scratch now, the 0055 chain lands the table
    (+ indexes) — proving 0055 composes with every prior migration."""
    db = await _migrated(tmp_path / "g.db")
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='immunity_shadow_events'"
    )
    assert await cur.fetchone() is not None
    idx = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='immunity_shadow_events'"
    )
    names = {r[0] for r in await idx.fetchall()}
    assert {
        "idx_immunity_shadow_events_observed_at",
        "idx_immunity_shadow_events_gate",
    } <= names
    await db.close()


@pytest.mark.asyncio
async def test_real_config_drives_shadow_emit(tmp_path):
    """Real config → emit integration, no mocks. Whatever the live merged
    ws3_immunity config says for the injection gate, the emit honors it:
    off → no row; shadow/enforce → an external row lands, first_party never."""
    mode = immunity.gate_mode("injection")  # reads config/ws3_immunity.yaml live
    assert mode in ("off", "shadow", "enforce")

    db = await _migrated(tmp_path / "g.db")
    wrote_ext = await immunity_shadow.record_would_block(
        gate="injection",
        source_kind="recall_inject",
        source_ref="mcp/memory/core.py::memory_recall",
        process="server",
        blockable_count=2,
        origin_class="external_untrusted",
        db=db,
    )
    # The never-block invariant holds under the REAL config too.
    wrote_fp = await immunity_shadow.record_would_block(
        gate="injection",
        source_kind="recall_inject",
        source_ref="x",
        process="server",
        blockable_count=2,
        origin_class="first_party",
        db=db,
    )
    assert wrote_fp is False

    if mode == "off":
        assert wrote_ext is False
        assert await crud.count(db) == 0
    else:
        assert wrote_ext is True
        rows = await crud.list_recent(db)
        assert len(rows) == 1
        assert rows[0]["origin_class"] == "external_untrusted"
        assert rows[0]["mode"] == mode
        summary = await immunity_shadow.recent_summary(db=db)
        assert summary and summary[0]["n"] == 1
    await db.close()


@pytest.mark.asyncio
async def test_item_classifier_matches_store_derivation(tmp_path):
    """The recall-time blockability classifier agrees with store-time
    derivation for the common cases (external KB blockable; first-party
    pipelines in KB not)."""
    assert (
        immunity_shadow.item_is_blockable(
            collection="knowledge_base", source_pipeline="knowledge_ingest"
        )
        is True
    )
    assert (
        immunity_shadow.item_is_blockable(collection="knowledge_base", source_pipeline="surplus")
        is False
    )
    assert (
        immunity_shadow.item_is_blockable(collection="episodic_memory", source_pipeline=None)
        is False
    )


@pytest.mark.asyncio
async def test_memory_recall_compact_branch_emits(tmp_path, monkeypatch):
    """Regression (Fable GAP): the compact=True branch early-returns previews
    BEFORE the full-path wrap/emit — it must still record the gate. Drives the
    real memory_recall(compact=True) with a stubbed retriever + migrated DB."""
    from unittest.mock import AsyncMock, MagicMock

    import genesis.mcp.memory_mcp as mod
    from genesis.memory.types import RetrievalResult

    db = await _migrated(tmp_path / "g.db")
    kb_hit = RetrievalResult(
        memory_id="m1",
        content="external doc body",
        source="doc",
        memory_type="fact",
        score=0.9,
        vector_rank=1,
        fts_rank=None,
        activation_score=0.5,
        payload={"wing": "", "room": ""},
        source_pipeline="knowledge_ingest",
        memory_class="fact",
        collection="knowledge_base",
    )
    retriever = AsyncMock()
    retriever.recall = AsyncMock(return_value=[kb_hit])

    old = (mod._store, mod._db, mod._retriever, mod._qdrant)
    try:
        mod._store, mod._db, mod._retriever, mod._qdrant = (
            MagicMock(),
            db,
            retriever,
            MagicMock(),
        )
        from genesis.mcp.memory.core import memory_recall

        out = await memory_recall.fn(
            "q",
            source="knowledge",
            compact=True,
            mode="standard",
            corrective=False,
            include_graph=False,
            rerank=False,
            expand_query_terms=False,
        )
        assert out and out[0]["memory_id"] == "m1"  # compact preview returned
        rows = await crud.list_recent(db)
        assert len(rows) == 1  # ...AND the gate recorded it
        assert rows[0]["source_ref"] == "mcp/memory/core.py::memory_recall"
        assert '"path":"compact"' in rows[0]["detail"]
    finally:
        mod._store, mod._db, mod._retriever, mod._qdrant = old
        await db.close()


@pytest.mark.asyncio
async def test_prune_bounds_the_store(tmp_path):
    db = await _migrated(tmp_path / "g.db")
    common = dict(
        gate="injection",
        mode="shadow",
        origin_class="external_untrusted",
        would_block=True,
        source_kind="recall_inject",
        source_ref="x",
        detail=None,
        process="server",
    )
    await crud.record(db, id="old", observed_at="2026-01-01T00:00:00+00:00", **common)
    await crud.record(db, id="new", observed_at="2026-07-11T00:00:00+00:00", **common)
    deleted = await crud.prune_immunity_shadow_events(
        db, older_than_days=45, now="2026-07-11T12:00:00+00:00"
    )
    assert deleted == 1
    assert await crud.count(db) == 1
    await db.close()
