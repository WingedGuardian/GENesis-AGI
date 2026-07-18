"""expires_at TTL comparison must be format/timezone-robust (Codex audit idx 52).

The 2026-04-18 migration backfilled expires_at with SQLite ``datetime()`` (a
space separator: ``2026-07-18 10:00:00``), while every resolve path
string-compared it against Python ``datetime.isoformat()`` (a 'T' separator).
Since ``' ' (0x20) < 'T' (0x54)``, any space-format expiry sorted as
always-earlier than the 'T'-format "now" cutoff, so unresolved observations
resolved up to ~24h early. The fix ``datetime()``-wraps both sides of every
expires_at comparison (and emits ISO in the backfill). These lock the runtime
resolve path against both stored formats.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from genesis.db.crud.observations import resolve_expired

pytestmark = pytest.mark.asyncio


async def _obs(db, oid: int, expires_at: str) -> None:
    await db.execute(
        "INSERT INTO observations "
        "(id, source, type, content, priority, created_at, expires_at, resolved) "
        "VALUES (?,?,?,?,?,?,?,0)",
        (oid, "s", "generic", "c", "medium", "2026-01-01T00:00:00+00:00", expires_at),
    )
    await db.commit()


async def test_space_format_future_expiry_not_resolved_early(empty_db):
    # Legacy space-format expiry 6h in the FUTURE. Under the old raw string
    # compare it sorted before the 'T'-format now and resolved early; the
    # datetime()-wrap must treat it correctly -> NOT resolved.
    future = datetime.now(UTC) + timedelta(hours=6)
    await _obs(empty_db, 1, future.strftime("%Y-%m-%d %H:%M:%S"))
    assert await resolve_expired(empty_db) == 0


async def test_iso_future_expiry_not_resolved(empty_db):
    future = datetime.now(UTC) + timedelta(hours=6)
    await _obs(empty_db, 2, future.isoformat())
    assert await resolve_expired(empty_db) == 0


async def test_genuinely_expired_resolved_both_formats(empty_db):
    now = datetime.now(UTC)
    await _obs(empty_db, 3, (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"))  # space, past
    await _obs(empty_db, 4, (now - timedelta(hours=2)).isoformat())  # 'T', past
    assert await resolve_expired(empty_db) == 2
