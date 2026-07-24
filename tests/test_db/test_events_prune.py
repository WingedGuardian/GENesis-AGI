"""Retention for the observability ``events`` table (crud.events.prune).

The event bus is the one high-volume table with no retention until the
``events_prune`` drip job (runtime/init/learning.py) was wired. These lock the
crud it calls: an ISO ``older_than`` cutoff deletes strictly-older rows and
leaves recent ones, and (with ``event_type``) can scope to one type.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.db.crud import events
from genesis.db.schema import create_all_tables

pytestmark = pytest.mark.asyncio


async def _db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_all_tables(db)  # canonical events schema — drift-proof
    return db


def _iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


async def _insert(db, *, days_ago: float, event_type: str = "test.event") -> str:
    return await events.insert(
        db,
        subsystem="health",
        severity="error",
        event_type=event_type,
        message=f"event {days_ago}d ago",
        timestamp=_iso(days_ago),
    )


async def test_prune_removes_only_older_than_cutoff():
    db = await _db()
    try:
        old = await _insert(db, days_ago=100)
        recent = await _insert(db, days_ago=10)
        cutoff = _iso(90)

        removed = await events.prune(db, older_than=cutoff)

        assert removed == 1
        rows = await events.query(db)
        ids = {r["id"] for r in rows}
        assert old not in ids and recent in ids
    finally:
        await db.close()


async def test_prune_empty_table_is_noop():
    db = await _db()
    try:
        assert await events.prune(db, older_than=_iso(90)) == 0
    finally:
        await db.close()


async def test_prune_boundary_is_strict_less_than():
    """A row exactly at the cutoff is NOT pruned (DELETE uses `timestamp < ?`)."""
    db = await _db()
    try:
        cutoff = _iso(90)
        await events.insert(
            db,
            subsystem="health",
            severity="error",
            event_type="boundary",
            message="at cutoff",
            timestamp=cutoff,
        )
        assert await events.prune(db, older_than=cutoff) == 0
    finally:
        await db.close()


async def test_prune_scoped_to_event_type():
    db = await _db()
    try:
        await _insert(db, days_ago=100, event_type="keep.me")
        await _insert(db, days_ago=100, event_type="prune.me")
        cutoff = _iso(90)

        removed = await events.prune(db, older_than=cutoff, event_type="prune.me")

        assert removed == 1
        remaining = {r["event_type"] for r in await events.query(db)}
        assert remaining == {"keep.me"}
    finally:
        await db.close()
