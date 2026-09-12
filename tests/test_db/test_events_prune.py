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
from genesis.mcp.health.manifest import compute_heartbeat_staleness

pytestmark = pytest.mark.asyncio


async def _db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_all_tables(db)  # canonical events schema — drift-proof
    return db


def _iso(days_ago: float) -> str:
    # Negative days_ago → a FUTURE timestamp (used to simulate clock-skew rows).
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


async def _insert_hb(db, *, subsystem: str, days_ago: float) -> str:
    """Insert a durable ``heartbeat`` event for a subsystem at a now-relative age."""
    return await events.insert(
        db,
        subsystem=subsystem,
        severity="debug",
        event_type="heartbeat",
        message=f"{subsystem} pulse {days_ago}d ago",
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


# ── keep_latest_per_subsystem: future-row (clock-skew) hardening ──────────────
# A corrupt/clock-skewed FUTURE heartbeat row is the lexical MAX(timestamp), so
# the keep-latest anchor pins ON IT — genuine pulses age past the window and are
# deleted, leaving only the future row → compute_heartbeat_staleness degrades to
# a permanent ``unknown``. The GC must anchor on the newest VALID row and delete
# materially-future rows outright.


async def test_keep_latest_deletes_future_row_and_retains_newest_valid():
    """The bug: a 'future' heartbeat must not survive as the keep-latest anchor.

    Seed a far-future row + two genuine pulses both older than the 7d cutoff.
    After GC: the future row is gone and the NEWEST VALID pulse survives (so the
    liveness read can still say 'dead since X' rather than 'unknown').
    """
    db = await _db()
    try:
        future = await _insert_hb(db, subsystem="ego", days_ago=-1000)  # ~2099
        valid_old = await _insert_hb(db, subsystem="ego", days_ago=100)
        valid_newest = await _insert_hb(db, subsystem="ego", days_ago=90)
        cutoff = _iso(7)

        await events.prune(
            db, older_than=cutoff, event_type="heartbeat", keep_latest_per_subsystem=True
        )

        ids = {r["id"] for r in await events.query(db)}
        assert future not in ids, "materially-future row must be deleted"
        assert valid_newest in ids, "newest VALID pulse must be retained as the anchor"
        assert valid_old not in ids, "older valid pulse below the anchor is pruned"
    finally:
        await db.close()


async def test_keep_latest_only_far_future_rows_all_deleted():
    """A subsystem whose ONLY rows are IMPLAUSIBLY-far future → all deleted.

    Far-future (> the corrupt horizon) rows are corrupt beyond recovery; with no
    valid pulse to preserve, keeping nothing (→ no_heartbeat) is the truthful
    outcome, not a retained corrupt row.
    """
    db = await _db()
    try:
        await _insert_hb(db, subsystem="inbox", days_ago=-500)  # ~2027+, > 1d horizon
        await _insert_hb(db, subsystem="inbox", days_ago=-400)
        cutoff = _iso(7)

        await events.prune(
            db, older_than=cutoff, event_type="heartbeat", keep_latest_per_subsystem=True
        )

        rows = [r for r in await events.query(db) if r["subsystem"] == "inbox"]
        assert rows == [], "only-future rows must all be deleted (no valid pulse to keep)"
    finally:
        await db.close()


async def test_keep_latest_normal_case_retains_newest_valid_below_cutoff():
    """No future rows: keep_latest still retains the newest pulse even when it is
    older than the cutoff (the whole point of keep_latest_per_subsystem)."""
    db = await _db()
    try:
        oldest = await _insert_hb(db, subsystem="dashboard", days_ago=100)
        newest = await _insert_hb(db, subsystem="dashboard", days_ago=30)
        cutoff = _iso(7)

        await events.prune(
            db, older_than=cutoff, event_type="heartbeat", keep_latest_per_subsystem=True
        )

        ids = {r["id"] for r in await events.query(db)}
        assert newest in ids and oldest not in ids
    finally:
        await db.close()


async def test_keep_latest_future_row_no_longer_poisons_staleness_read():
    """Integration: after GC, compute_heartbeat_staleness reads the truthful
    verdict (a stale-but-real pulse → 'overdue') instead of the future-row
    induced permanent 'unknown'."""
    db = await _db()
    try:
        await _insert_hb(db, subsystem="ego", days_ago=-1000)  # skewed-future
        await _insert_hb(db, subsystem="ego", days_ago=30)  # real, long stale
        cutoff = _iso(7)

        await events.prune(
            db, older_than=cutoff, event_type="heartbeat", keep_latest_per_subsystem=True
        )

        verdict = await compute_heartbeat_staleness("ego", db=db, paused=False)
        assert verdict["status"] == "overdue", verdict
    finally:
        await db.close()


async def test_keep_latest_modestly_future_retained_only_far_future_deleted():
    """A modestly-future (recoverable) row must NOT be destroyed — it ages into
    validity. Only implausibly-far-future (corrupt) rows are deleted outright.

    Guards the split-bound design: the destructive horizon (>1d) is deliberately
    far wider than the read-side display tolerance (5min), so a row a few minutes
    ahead of a skewed clock survives instead of reverting the subsystem to a false
    no_heartbeat.
    """
    db = await _db()
    try:
        far_future = await _insert_hb(db, subsystem="ego", days_ago=-1000)  # ~2099
        mod_future = await _insert_hb(db, subsystem="ego", days_ago=-(30 / 1440))  # +30min
        valid = await _insert_hb(db, subsystem="ego", days_ago=30)
        cutoff = _iso(7)

        await events.prune(
            db, older_than=cutoff, event_type="heartbeat", keep_latest_per_subsystem=True
        )

        ids = {r["id"] for r in await events.query(db)}
        assert far_future not in ids, "implausibly-far-future row is corrupt → deleted"
        assert mod_future in ids, "modestly-future row is recoverable → retained"
        assert valid in ids, "genuine valid pulse retained (anchor within display tolerance)"
    finally:
        await db.close()


async def test_keep_latest_ties_at_valid_anchor_both_retained():
    """Two rows sharing the newest-valid timestamp are BOTH kept (strict ``<``);
    an older valid row below them is pruned."""
    db = await _db()
    try:
        tie_ts = _iso(30)
        a = await events.insert(
            db,
            subsystem="ego",
            severity="debug",
            event_type="heartbeat",
            message="tie a",
            timestamp=tie_ts,
        )
        b = await events.insert(
            db,
            subsystem="ego",
            severity="debug",
            event_type="heartbeat",
            message="tie b",
            timestamp=tie_ts,
        )
        older = await _insert_hb(db, subsystem="ego", days_ago=100)
        cutoff = _iso(7)

        await events.prune(
            db, older_than=cutoff, event_type="heartbeat", keep_latest_per_subsystem=True
        )

        ids = {r["id"] for r in await events.query(db)}
        assert a in ids and b in ids, "tied newest-valid rows are both kept (strict <)"
        assert older not in ids, "older valid row below the anchor is pruned"
    finally:
        await db.close()
