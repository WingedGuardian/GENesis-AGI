"""Tests for genesis.memory.health — algorithmic memory health checks."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.memory import health as health_mod
from genesis.memory.health import (
    _scan_top_tags,
    distribution_stats,
    full_health_report,
    growth_stats,
    orphan_stats,
)


def _ts(days_ago: int = 0) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()


@pytest.fixture(autouse=True)
def _reset_top_tags_state():
    """Isolate the module-level SWR cache between tests."""
    health_mod._reset_top_tags_state()
    yield
    health_mod._reset_top_tags_state()


class _RecordingConn:
    """Proxy that records every SQL string executed on the wrapped connection."""

    def __init__(self, real):
        self._real = real
        self.executed: list[str] = []

    async def execute(self, sql, *args, **kwargs):
        self.executed.append(sql)
        return await self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


async def _make_file_db(path):
    """A file-backed SerializedConnection with the full schema (memory_fts incl.)."""
    from genesis.db.connection import SerializedConnection
    from genesis.db.schema import create_all_tables

    conn = await aiosqlite.connect(str(path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    await create_all_tables(conn)
    await conn.commit()
    return SerializedConnection(conn)


async def _seed_fts(db, rows: list[tuple[str, str]]) -> None:
    """rows = [(memory_id, tags), ...] — also seed memory_metadata for `total`."""
    for mem_id, tags in rows:
        await db.execute("INSERT INTO memory_fts (memory_id, tags) VALUES (?, ?)", (mem_id, tags))
        await db.execute(
            "INSERT INTO memory_metadata (memory_id, created_at) VALUES (?, ?)",
            (mem_id, _ts()),
        )
    await db.commit()


# --- A1: top_tags served stale-while-revalidate, never on the caller loop -----


@pytest.mark.asyncio()
async def test_distribution_stats_never_scans_fts_on_caller_connection(tmp_path, monkeypatch):
    """THE regression guard: the O(corpus) memory_fts scan must NEVER run on the
    caller's connection (the event loop). It is served stale, refreshed off-loop
    via a separate read-only connection."""
    db = await _make_file_db(tmp_path / "g.db")
    try:
        await _seed_fts(db, [("m1", "alpha beta"), ("m2", "alpha gamma"), ("m3", "alpha beta")])

        # Capture the scheduled rebuild coroutine instead of firing a real task.
        captured = []
        monkeypatch.setattr(health_mod, "tracked_task", lambda coro, **kw: captured.append(coro))

        proxy = _RecordingConn(db)
        first = await distribution_stats(proxy)

        # Caller connection never touched memory_fts (only cheap aggregates + pragma).
        assert not any("memory_fts" in s for s in proxy.executed), proxy.executed
        # Cold start: cache empty, warming flag set, a rebuild was scheduled.
        assert first["top_tags"] == []
        assert first["top_tags_warming"] is True
        assert first["total"] == 3
        assert len(captured) == 1

        # Run the background rebuild (off-loop path) and confirm it populates cache.
        await captured[0]
        assert dict(health_mod._top_tags_cache) == {"alpha": 3, "beta": 2, "gamma": 1}

        # A subsequent call serves the materialized tags, still without scanning fts.
        proxy2 = _RecordingConn(db)
        second = await distribution_stats(proxy2)
        assert not any("memory_fts" in s for s in proxy2.executed)
        assert second["top_tags"][0] == ("alpha", 3)
        assert second["top_tags_warming"] is False
    finally:
        await db.close()


@pytest.mark.asyncio()
async def test_distribution_stats_cold_start_in_memory_skips_rebuild(empty_db, monkeypatch):
    """On :memory: (tests) no out-of-band scan is possible — serve empty, schedule
    nothing, never block. The public shape stays intact (top_tags is a list)."""
    scheduled = []
    monkeypatch.setattr(health_mod, "tracked_task", lambda coro, **kw: scheduled.append(coro))

    result = await distribution_stats(empty_db)
    assert result["top_tags"] == []
    assert isinstance(result["top_tags"], list)
    assert result["top_tags_warming"] is True
    assert scheduled == []  # :memory: → _main_db_path None → no rebuild


def test_scan_top_tags_counts_and_is_readonly(tmp_path):
    """The sync scanner counts tags correctly, honors the limit, uses a RO
    connection (writes rejected), and fails soft on a bad path."""
    db_path = str(tmp_path / "s.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE VIRTUAL TABLE memory_fts USING fts5("
        "memory_id UNINDEXED, content, source_type, tags, collection UNINDEXED, "
        "tokenize='porter ascii')"
    )
    conn.executemany(
        "INSERT INTO memory_fts (memory_id, tags) VALUES (?, ?)",
        [("m1", "alpha beta"), ("m2", "alpha gamma"), ("m3", "alpha beta")],
    )
    conn.commit()
    conn.close()

    result = _scan_top_tags(db_path)
    assert result[0] == ("alpha", 3)
    assert dict(result) == {"alpha": 3, "beta": 2, "gamma": 1}
    assert _scan_top_tags(db_path, limit=1) == [("alpha", 3)]

    # mode=ro really is read-only.
    ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO memory_fts (memory_id, tags) VALUES ('x', 'y')")
    finally:
        ro.close()

    # Missing DB → graceful empty, no raise.
    assert _scan_top_tags(str(tmp_path / "nope.db")) == []


@pytest.mark.asyncio()
async def test_rebuild_top_tags_populates_cache_and_clears_flag(tmp_path):
    """The async rebuild materializes the cache, stamps built_at/corpus, and
    always releases the single-flight guard."""
    db_path = str(tmp_path / "r.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE VIRTUAL TABLE memory_fts USING fts5("
        "memory_id UNINDEXED, content, source_type, tags, collection UNINDEXED)"
    )
    conn.executemany(
        "INSERT INTO memory_fts (memory_id, tags) VALUES (?, ?)",
        [("m1", "x y"), ("m2", "x")],
    )
    conn.commit()
    conn.close()

    health_mod._top_tags_rebuild_in_flight = True
    await health_mod._rebuild_top_tags(db_path, total=2)

    assert dict(health_mod._top_tags_cache) == {"x": 2, "y": 1}
    assert health_mod._top_tags_built_at > 0.0
    assert health_mod._top_tags_corpus_count == 2
    assert health_mod._top_tags_rebuild_in_flight is False


@pytest.mark.asyncio()
async def test_maybe_refresh_single_flight_and_staleness(empty_db, monkeypatch, tmp_path):
    """Schedules once when stale; not while a rebuild is in flight; not when fresh;
    reschedules when the corpus drifts >10%."""
    db = await _make_file_db(tmp_path / "sf.db")
    try:
        await _seed_fts(db, [("m1", "a b"), ("m2", "a c")])

        # This test asserts scheduling COUNT, not the rebuild — close each
        # captured coro so it is not left unawaited (RuntimeWarning hygiene).
        scheduled = []

        def _capture_and_close(coro, **kw):
            scheduled.append(coro)
            coro.close()

        monkeypatch.setattr(health_mod, "tracked_task", _capture_and_close)

        # Cold + stale → schedules exactly one.
        await health_mod._maybe_refresh_top_tags(db, total=100)
        assert len(scheduled) == 1
        assert health_mod._top_tags_rebuild_in_flight is True

        # In-flight → no second schedule.
        await health_mod._maybe_refresh_top_tags(db, total=100)
        assert len(scheduled) == 1

        # Simulate the rebuild completing at corpus=100.
        health_mod._top_tags_rebuild_in_flight = False
        health_mod._top_tags_built_at = health_mod.time.monotonic()
        health_mod._top_tags_corpus_count = 100

        # Fresh (5% drift, within age) → no schedule.
        await health_mod._maybe_refresh_top_tags(db, total=105)
        assert len(scheduled) == 1

        # >10% drift → reschedule.
        await health_mod._maybe_refresh_top_tags(db, total=120)
        assert len(scheduled) == 2
    finally:
        await db.close()


@pytest.mark.asyncio()
async def test_top_tags_kill_switch_disables_rebuild(monkeypatch, tmp_path):
    """GENESIS_TOP_TAGS_DISABLED neutralizes the background rebuild entirely."""
    db = await _make_file_db(tmp_path / "k.db")
    try:
        await _seed_fts(db, [("m1", "a b")])
        monkeypatch.setenv("GENESIS_TOP_TAGS_DISABLED", "1")
        scheduled = []
        monkeypatch.setattr(health_mod, "tracked_task", lambda coro, **kw: scheduled.append(coro))

        result = await distribution_stats(db)
        assert scheduled == []
        assert result["top_tags"] == []
    finally:
        await db.close()


@pytest.mark.asyncio()
async def test_concurrent_refresh_schedules_single_rebuild(tmp_path, monkeypatch):
    """Single-flight under concurrency: two stale callers racing through the
    _main_db_path await must schedule EXACTLY ONE rebuild (not two)."""
    db = await _make_file_db(tmp_path / "cc.db")
    try:
        await _seed_fts(db, [("m1", "a b"), ("m2", "a c")])

        scheduled = []

        def _capture_and_close(coro, **kw):
            scheduled.append(coro)
            coro.close()

        monkeypatch.setattr(health_mod, "tracked_task", _capture_and_close)

        # Both see the same cold (stale) cache and race through the await in
        # _main_db_path; the flag must be claimed before that yield.
        await asyncio.gather(
            health_mod._maybe_refresh_top_tags(db, 100),
            health_mod._maybe_refresh_top_tags(db, 100),
        )
        assert len(scheduled) == 1
    finally:
        await db.close()


def test_stale_stabilizes_for_empty_corpus():
    """A genuinely empty corpus that has been built must NOT read as perpetually
    stale (else every health check reschedules a rebuild)."""
    # Simulate a completed build over an empty corpus.
    health_mod._top_tags_built_at = health_mod.time.monotonic()
    health_mod._top_tags_corpus_count = 0
    assert health_mod._top_tags_is_stale(0) is False  # built + still empty → fresh
    assert health_mod._top_tags_is_stale(100) is True  # corpus grew → stale


@pytest.mark.asyncio()
async def test_cancelled_during_resolve_releases_flag(empty_db, monkeypatch):
    """Cancellation while suspended in _main_db_path (the window the single-flight
    claim now spans) must release the in-flight flag AND re-raise — never wedge
    the flag True (which would silently disable all future refreshes)."""

    async def _cancel(_db):
        raise asyncio.CancelledError()

    monkeypatch.setattr(health_mod, "_main_db_path", _cancel)

    with pytest.raises(asyncio.CancelledError):
        await health_mod._maybe_refresh_top_tags(empty_db, 100)  # cold → stale → claims flag

    assert health_mod._top_tags_rebuild_in_flight is False  # not wedged


@pytest.mark.asyncio()
async def test_orphan_stats(empty_db):
    db = empty_db
    # Insert 3 memories: 2 old (orphan candidates), 1 recent
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at) VALUES (?, ?)",
        ("m1", _ts(days_ago=30)),
    )
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at) VALUES (?, ?)",
        ("m2", _ts(days_ago=14)),
    )
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at) VALUES (?, ?)",
        ("m3", _ts(days_ago=1)),
    )
    # m4: old, will be linked — should NOT be orphan
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at) VALUES (?, ?)",
        ("m4", _ts(days_ago=20)),
    )
    # Link m4 to m3 so m4 is NOT an orphan (m1 and m2 remain unlinked)
    await db.execute(
        "INSERT INTO memory_links (source_id, target_id, link_type, strength, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("m4", "m3", "related_to", 0.5, _ts()),
    )
    await db.commit()

    result = await orphan_stats(db, min_age_days=7)
    assert result["total_memories"] == 4
    # m1 (old, unlinked) and m2 (old, unlinked) => orphans; m3 too recent; m4 linked
    assert result["orphans"] == 2
    assert 0 < result["orphan_pct"] <= 100


@pytest.mark.asyncio()
async def test_distribution_stats(empty_db):
    db = empty_db
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at, collection) VALUES (?, ?, ?)",
        ("m1", _ts(), "episodic_memory"),
    )
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at, collection) VALUES (?, ?, ?)",
        ("m2", _ts(), "episodic_memory"),
    )
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at, collection) VALUES (?, ?, ?)",
        ("m3", _ts(), "knowledge"),
    )
    await db.commit()

    result = await distribution_stats(db)
    assert result["total"] == 3
    assert result["by_collection"]["episodic_memory"] == 2
    assert result["by_collection"]["knowledge"] == 1
    assert isinstance(result["top_tags"], list)


@pytest.mark.asyncio()
async def test_growth_stats(empty_db):
    db = empty_db
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at) VALUES (?, ?)",
        ("recent", _ts(days_ago=0)),
    )
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at) VALUES (?, ?)",
        ("week_old", _ts(days_ago=5)),
    )
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at) VALUES (?, ?)",
        ("month_old", _ts(days_ago=20)),
    )
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at) VALUES (?, ?)",
        ("ancient", _ts(days_ago=60)),
    )
    await db.commit()

    result = await growth_stats(db)
    assert result["last_24h"] == 1
    assert result["last_7d"] == 2
    assert result["last_30d"] == 3
    assert isinstance(result["avg_per_day_7d"], float)
    assert result["avg_per_day_7d"] == round(2 / 7, 2)


@pytest.mark.asyncio()
async def test_full_health_report_no_qdrant(empty_db):
    db = empty_db
    await db.execute(
        "INSERT INTO memory_metadata (memory_id, created_at) VALUES (?, ?)",
        ("m1", _ts()),
    )
    await db.commit()

    report = await full_health_report(db, qdrant_client=None)
    assert "orphans" in report
    assert "distribution" in report
    assert "growth" in report
    assert report["duplicates"] is None
    assert report["distribution"]["total"] == 1
