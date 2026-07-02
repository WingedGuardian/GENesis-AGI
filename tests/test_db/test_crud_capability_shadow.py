"""crud.capability_shadow: best-effort record with a table-existence guard + read helpers."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import capability_shadow as crud
from genesis.db.schema._tables import INDEXES, TABLES


@pytest.fixture(autouse=True)
def _reset_table_cache():
    # The per-process existence cache would otherwise leak across tests (a test that
    # creates the table would mark it verified for a later "no table" test).
    crud._table_verified = False
    yield
    crud._table_verified = False


async def _db(path, *, with_table=True):
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    if with_table:
        await conn.execute(TABLES["capability_shadow_events"])
        for idx in INDEXES:
            if "capability_shadow_events" in idx:
                await conn.execute(idx)
        await conn.commit()
    return conn


def _kw(n, *, path="deliver", verb="send", risk="bulk", state=None, would_hold=True):
    return {
        "id": f"sh-{n}",
        "observed_at": f"2026-07-01T00:00:0{n}+00:00",
        "path": path,
        "channel": "discord",
        "cell_domain": "discord",
        "cell_verb": verb,
        "cell_risk_class": risk,
        "cell_state": state,
        "would_hold": would_hold,
        "target": "announcements",
        "content_preview": "hello world",
        "content_hash": f"h{n}",
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
    # Best-effort: the subprocess pre-migration window — skip, do NOT raise, do NOT create.
    assert await crud.record(db, **_kw(1)) is False
    # Table was NOT created as a side effect.
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='capability_shadow_events'"
    )
    assert await cur.fetchone() is None
    await db.close()


@pytest.mark.asyncio
async def test_record_self_heals_after_table_created(tmp_path):
    db = await _db(tmp_path / "g.db", with_table=False)
    assert await crud.record(db, **_kw(1)) is False  # missing -> skip (cache stays False)
    # Server migration lands mid-life:
    await db.execute(TABLES["capability_shadow_events"])
    await db.commit()
    assert await crud.record(db, **_kw(2)) is True  # now writes without a restart
    assert await crud.count(db) == 1
    await db.close()


@pytest.mark.asyncio
async def test_would_hold_stored_as_int(tmp_path):
    db = await _db(tmp_path / "g.db")
    await crud.record(db, **_kw(1, would_hold=True))
    await crud.record(db, **_kw(2, would_hold=False))
    rows = {r["id"]: r["would_hold"] for r in await crud.list_recent(db)}
    assert rows == {"sh-1": 1, "sh-2": 0}
    await db.close()


@pytest.mark.asyncio
async def test_list_recent_newest_first(tmp_path):
    db = await _db(tmp_path / "g.db")
    await crud.record(db, **_kw(1))
    await crud.record(db, **_kw(3))
    await crud.record(db, **_kw(2))
    assert [r["id"] for r in await crud.list_recent(db)] == ["sh-3", "sh-2", "sh-1"]
    await db.close()


@pytest.mark.asyncio
async def test_summary_groups_by_door_and_verdict(tmp_path):
    db = await _db(tmp_path / "g.db")
    await crud.record(db, **_kw(1, path="deliver", verb="send", risk="bulk", would_hold=True))
    await crud.record(db, **_kw(2, path="deliver", verb="send", risk="bulk", would_hold=True))
    await crud.record(db, **_kw(3, path="reply", verb="reply", risk="standard", would_hold=False))
    rows = await crud.summary(db)
    top = rows[0]
    assert top["path"] == "deliver" and top["would_hold"] == 1 and top["n"] == 2
    assert any(r["path"] == "reply" and r["would_hold"] == 0 and r["n"] == 1 for r in rows)
    await db.close()
