"""cognitive_state TTL comparison must be format-robust (PR-K collateral fix).

get_recent_active compared the isoformat-written ('T'-separator) expires_at
against SQLite datetime('now') (space separator). Since 'T' > ' ', a same-day
expired row sorted as always-later-than-now and never registered as expired
until the calendar date rolled over — so the morning report's cognitive-state
section showed stale entries as active. The fix datetime()-wraps both the
expires_at and created_at comparisons (mirror of the observations idx-52 fix).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from genesis.db.crud import cognitive_state as cs

pytestmark = pytest.mark.asyncio


async def _insert(db, cid: str, content: str, expires_at: str, created_at: str) -> None:
    await db.execute(
        "INSERT INTO cognitive_state "
        "(id, content, section, generated_by, created_at, expires_at) "
        "VALUES (?,?,?,?,?,?)",
        (cid, content, "resilience_degradation", "test", created_at, expires_at),
    )
    await db.commit()


async def test_past_iso_expiry_excluded_from_recent_active(empty_db):
    now = datetime.now(UTC)
    created = (now - timedelta(minutes=1)).isoformat()
    expired = (now - timedelta(minutes=5)).isoformat()  # 'T', genuinely past
    await _insert(empty_db, "past", "PAST-ROW", expired, created)
    rows = await cs.get_recent_active(empty_db, hours=24)
    assert not any(r["content"] == "PAST-ROW" for r in rows)


async def test_future_iso_expiry_included(empty_db):
    now = datetime.now(UTC)
    created = (now - timedelta(minutes=1)).isoformat()
    future = (now + timedelta(hours=6)).isoformat()
    await _insert(empty_db, "future", "FUTURE-ROW", future, created)
    rows = await cs.get_recent_active(empty_db, hours=24)
    assert any(r["content"] == "FUTURE-ROW" for r in rows)
