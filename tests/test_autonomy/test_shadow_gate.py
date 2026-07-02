"""autonomy.shadow_gate.observe_discord_send — observe-only Discord capability shadow.

Verifies: would_hold reflects cell state (missing/ask => hold, granted => allow); a row
is recorded; it is READ-ONLY (never creates/mutates the capability cell); and it is
best-effort (None db / broken db never raise, never write)."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.autonomy import shadow_gate
from genesis.db.crud import capability_grants as cg
from genesis.db.crud import capability_shadow
from genesis.db.schema._tables import INDEXES, TABLES


@pytest.fixture(autouse=True)
def _reset_table_cache():
    capability_shadow._table_verified = False
    yield
    capability_shadow._table_verified = False


async def _db(path, *, grant=None):
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    for table in ("capability_shadow_events", "capability_grants"):
        await conn.execute(TABLES[table])
    for idx in INDEXES:
        if "capability_shadow_events" in idx:
            await conn.execute(idx)
    if grant is not None:  # (domain, verb, risk_class, state)
        d, v, r, s = grant
        await conn.execute(
            "INSERT INTO capability_grants (id, domain, verb, risk_class, state) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"{d}:{v}:{r}", d, v, r, s),
        )
    await conn.commit()
    return conn


@pytest.mark.asyncio
async def test_missing_cell_would_hold(tmp_path):
    db = await _db(tmp_path / "g.db")  # no discord cell exists
    ok = await shadow_gate.observe_discord_send(
        db, path="deliver", verb="send", risk_class="bulk",
        target="announcements", content="hi community",
    )
    assert ok is True
    rows = await capability_shadow.list_recent(db)
    assert len(rows) == 1
    r = rows[0]
    assert r["would_hold"] == 1 and r["cell_state"] is None
    assert r["cell_domain"] == "discord" and r["cell_verb"] == "send"
    assert r["cell_risk_class"] == "bulk" and r["path"] == "deliver"
    await db.close()


@pytest.mark.asyncio
async def test_ask_cell_would_hold(tmp_path):
    db = await _db(tmp_path / "g.db", grant=("discord", "send", "bulk", "ask"))
    await shadow_gate.observe_discord_send(
        db, path="deliver", verb="send", risk_class="bulk", target="x", content="y",
    )
    r = (await capability_shadow.list_recent(db))[0]
    assert r["would_hold"] == 1 and r["cell_state"] == "ask"
    await db.close()


@pytest.mark.asyncio
async def test_granted_cell_would_allow(tmp_path):
    db = await _db(tmp_path / "g.db", grant=("discord", "reply", "standard", "granted"))
    await shadow_gate.observe_discord_send(
        db, path="reply", verb="reply", risk_class="standard",
        target="123", content="thanks!",
    )
    r = (await capability_shadow.list_recent(db))[0]
    assert r["would_hold"] == 0 and r["cell_state"] == "granted"
    await db.close()


@pytest.mark.asyncio
async def test_readonly_never_creates_or_mutates_cell(tmp_path):
    db = await _db(tmp_path / "g.db")  # no discord cell
    await shadow_gate.observe_discord_send(
        db, path="poll", verb="poll", risk_class="bulk", target="a", content="b",
    )
    # The observation must NOT have created the capability cell (read-only shadow).
    assert await cg.get_cell(db, "discord", "poll", "bulk") is None
    await db.close()


@pytest.mark.asyncio
async def test_content_preview_bounded_hash_over_full_content(tmp_path):
    import hashlib

    db = await _db(tmp_path / "g.db")
    long = "z" * 300 + "TAIL"  # content that differs BEYOND the 200-char preview span
    await shadow_gate.observe_discord_send(
        db, path="deliver", verb="send", risk_class="bulk", target="t", content=long,
    )
    r = (await capability_shadow.list_recent(db))[0]
    assert len(r["content_preview"]) == 200                                  # excerpt bounded
    assert r["content_hash"] == hashlib.sha256(long.encode()).hexdigest()    # FULL content
    # Genuinely full — NOT the first-200-chars span (which would collide with the preview).
    assert r["content_hash"] != hashlib.sha256(long[:200].encode()).hexdigest()
    await db.close()


@pytest.mark.asyncio
async def test_none_db_is_noop(tmp_path):
    # Never raises, never writes when the process has no DB connection.
    assert await shadow_gate.observe_discord_send(
        None, path="reply", verb="reply", risk_class="standard", target="t", content="c",
    ) is False


@pytest.mark.asyncio
async def test_best_effort_swallows_db_error(tmp_path):
    # A db with NO tables at all: get_cell raises OperationalError -> swallowed, no raise.
    conn = await aiosqlite.connect(tmp_path / "empty.db")
    conn.row_factory = aiosqlite.Row
    assert await shadow_gate.observe_discord_send(
        conn, path="deliver", verb="send", risk_class="bulk", target="t", content="c",
    ) is False
    await conn.close()
