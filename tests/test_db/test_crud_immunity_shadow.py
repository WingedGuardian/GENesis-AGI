"""crud.immunity_shadow: WS-3 B1 immunity SHADOW store.

Best-effort record with a table-existence guard (async + sync writer paths),
plus read/aggregation/retention helpers. Modeled on
``crud.capability_shadow`` — the immunity gate is observe-only: rows are gate
DECISIONS + provenance refs, never recalled content, never a block.
"""

from __future__ import annotations

import sqlite3

import aiosqlite
import pytest

from genesis.db.crud import immunity_shadow as crud
from genesis.db.schema._tables import INDEXES, TABLES


@pytest.fixture(autouse=True)
def _reset_table_cache():
    # Per-process existence caches (async + sync) would leak across tests
    # (a test that creates the table would mark it verified for a later
    # "no table" test).
    crud._table_verified = False
    crud._table_verified_sync = False
    yield
    crud._table_verified = False
    crud._table_verified_sync = False


async def _db(path, *, with_table=True):
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    if with_table:
        await conn.execute(TABLES["immunity_shadow_events"])
        for idx in INDEXES:
            if "immunity_shadow_events" in idx:
                await conn.execute(idx)
        await conn.commit()
    return conn


def _sync_db(path, *, with_table=True):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if with_table:
        conn.execute(TABLES["immunity_shadow_events"])
        for idx in INDEXES:
            if "immunity_shadow_events" in idx:
                conn.execute(idx)
        conn.commit()
    return conn


def _kw(
    n,
    *,
    gate="injection",
    mode="shadow",
    origin_class="external_untrusted",
    would_block=True,
    source_ref="mcp/memory/core.py::memory_recall",
    source_kind="recall_inject",
    process="server",
    observed_at=None,
):
    return {
        "id": f"im-{n}",
        "observed_at": observed_at or f"2026-07-11T00:00:0{n}+00:00",
        "gate": gate,
        "mode": mode,
        "origin_class": origin_class,
        "would_block": would_block,
        "source_kind": source_kind,
        "source_ref": source_ref,
        "detail": '{"blockable": 3}',
        "process": process,
    }


@pytest.mark.asyncio
async def test_record_inserts_and_count(tmp_path):
    db = await _db(tmp_path / "g.db")
    assert await crud.record(db, **_kw(1)) is True
    assert await crud.count(db) == 1
    await db.close()


@pytest.mark.asyncio
async def test_record_noops_when_table_absent(tmp_path):
    db = await _db(tmp_path / "g.db", with_table=False)
    # Best-effort: subprocess pre-migration window — skip, do NOT raise, do NOT create.
    assert await crud.record(db, **_kw(1)) is False
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='immunity_shadow_events'"
    )
    assert await cur.fetchone() is None
    await db.close()


@pytest.mark.asyncio
async def test_record_self_heals_after_table_created(tmp_path):
    db = await _db(tmp_path / "g.db", with_table=False)
    assert await crud.record(db, **_kw(1)) is False  # missing -> skip (cache stays False)
    await db.execute(TABLES["immunity_shadow_events"])
    await db.commit()
    assert await crud.record(db, **_kw(2)) is True  # writes without a restart
    assert await crud.count(db) == 1
    await db.close()


@pytest.mark.asyncio
async def test_would_block_stored_as_int(tmp_path):
    db = await _db(tmp_path / "g.db")
    await crud.record(db, **_kw(1, would_block=True))
    await crud.record(db, **_kw(2, would_block=False))
    rows = {r["id"]: r["would_block"] for r in await crud.list_recent(db)}
    assert rows == {"im-1": 1, "im-2": 0}
    await db.close()


def test_record_sync_inserts(tmp_path):
    conn = _sync_db(tmp_path / "g.db")
    assert crud.record_sync(conn, **_kw(1, process="proactive_hook")) is True
    cur = conn.execute("SELECT COUNT(*) FROM immunity_shadow_events")
    assert cur.fetchone()[0] == 1
    conn.close()


def test_record_sync_noops_when_table_absent(tmp_path):
    conn = _sync_db(tmp_path / "g.db", with_table=False)
    assert crud.record_sync(conn, **_kw(1)) is False
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='immunity_shadow_events'"
    )
    assert cur.fetchone() is None
    conn.close()


@pytest.mark.asyncio
async def test_count_would_block_filters_gate_and_since(tmp_path):
    db = await _db(tmp_path / "g.db")
    await crud.record(
        db, **_kw(1, gate="injection", would_block=True, observed_at="2026-07-11T10:00:00+00:00")
    )
    await crud.record(
        db, **_kw(2, gate="injection", would_block=True, observed_at="2026-07-11T09:00:00+00:00")
    )
    # A would_block=0 row must NOT be counted.
    await crud.record(
        db, **_kw(3, gate="injection", would_block=False, observed_at="2026-07-11T10:00:00+00:00")
    )
    # A different gate must NOT be counted.
    await crud.record(
        db, **_kw(4, gate="procedure", would_block=True, observed_at="2026-07-11T10:00:00+00:00")
    )
    n = await crud.count_would_block(db, gate="injection", since="2026-07-11T09:30:00+00:00")
    assert n == 1  # only im-1 (injection, would_block, after the cutoff)
    await db.close()


@pytest.mark.asyncio
async def test_summary_groups_by_gate_and_source(tmp_path):
    db = await _db(tmp_path / "g.db")
    await crud.record(db, **_kw(1, source_ref="A", gate="injection"))
    await crud.record(db, **_kw(2, source_ref="A", gate="injection"))
    await crud.record(db, **_kw(3, source_ref="B", gate="injection"))
    rows = await crud.summary(db)
    top = rows[0]
    assert top["source_ref"] == "A" and top["gate"] == "injection" and top["n"] == 2
    assert any(r["source_ref"] == "B" and r["n"] == 1 for r in rows)
    await db.close()


@pytest.mark.asyncio
async def test_prune_deletes_only_old_rows(tmp_path):
    db = await _db(tmp_path / "g.db")
    await crud.record(db, **_kw(1, observed_at="2026-01-01T00:00:00+00:00"))  # old
    await crud.record(db, **_kw(2, observed_at="2026-07-11T00:00:00+00:00"))  # fresh
    deleted = await crud.prune_immunity_shadow_events(
        db, older_than_days=45, now="2026-07-11T12:00:00+00:00"
    )
    assert deleted == 1
    assert await crud.count(db) == 1
    remaining = await crud.list_recent(db)
    assert remaining[0]["id"] == "im-2"
    await db.close()
