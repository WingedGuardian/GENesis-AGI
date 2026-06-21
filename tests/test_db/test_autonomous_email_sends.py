"""Tests for the WS-8 PR-D autonomous-send ledger CRUD."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import autonomous_email_sends as aes
from genesis.db.schema import create_all_tables

_CELL = {"cell_domain": "email", "cell_verb": "send", "cell_risk_class": "standard"}


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await conn.commit()
        yield conn


async def _mk(db, *, id, sent_at, recipient="a@b.c", **over):
    await aes.create(
        db, id=id, recipient=recipient, sent_at=sent_at,
        subject="hi", thread_id="t1", outreach_id="o1",
        **{**_CELL, **over},
    )


@pytest.mark.asyncio
async def test_create_and_get(db):
    await _mk(db, id="s1", sent_at="2026-06-21T10:00:00")
    row = await aes.get_by_id(db, "s1")
    assert row["recipient"] == "a@b.c"
    assert row["cell_risk_class"] == "standard"
    assert row["flagged_at"] is None


@pytest.mark.asyncio
async def test_count_for_cell_since_windows(db):
    await _mk(db, id="old", sent_at="2026-06-21T08:00:00")
    await _mk(db, id="new1", sent_at="2026-06-21T10:30:00")
    await _mk(db, id="new2", sent_at="2026-06-21T10:45:00")
    # a send for a DIFFERENT cell must not count
    await _mk(db, id="bulk", sent_at="2026-06-21T10:50:00", cell_risk_class="bulk")
    n = await aes.count_for_cell_since(db, since="2026-06-21T10:00:00", **_CELL)
    assert n == 2


@pytest.mark.asyncio
async def test_list_recent_orders_desc(db):
    await _mk(db, id="a", sent_at="2026-06-21T09:00:00")
    await _mk(db, id="b", sent_at="2026-06-21T11:00:00")
    await _mk(db, id="c", sent_at="2026-06-21T10:00:00")
    rows = await aes.list_recent(db, limit=2)
    assert [r["id"] for r in rows] == ["b", "c"]


@pytest.mark.asyncio
async def test_mark_flagged_is_idempotent(db):
    await _mk(db, id="s1", sent_at="2026-06-21T10:00:00")
    assert await aes.mark_flagged(db, "s1", flagged_at="2026-06-21T12:00:00") is True
    # second flag is a no-op (already flagged) → caller must not re-demote
    assert await aes.mark_flagged(db, "s1", flagged_at="2026-06-21T13:00:00") is False
    row = await aes.get_by_id(db, "s1")
    assert row["flagged_at"] == "2026-06-21T12:00:00"  # first flag preserved
