"""SerializedConnection bounded lock-retry (WS-1 PR-1, follow-up 2d88740d).

Covers the retry schedule (delays + jitter bounds via a recording sleep stub),
idempotency-driven routing (execute/executemany/commit/rollback retried;
executescript deliberately not), the generator-materialization guard, the
widened lock predicate, and one REAL cross-connection contention run driven by
events — no wall-clock-dependent assertions anywhere (repo rule).
"""

import asyncio
import logging
import sqlite3

import aiosqlite
import pytest

from genesis.db import connection as connection_mod
from genesis.db.connection import (
    _JITTER_HIGH,
    _JITTER_LOW,
    _WRITE_RETRY_DELAYS,
    SerializedConnection,
    _is_lock_error,
)

ATTEMPTS_PER_EPISODE = len(_WRITE_RETRY_DELAYS) + 1


@pytest.fixture
def recorded_sleeps(monkeypatch):
    """Replace the retry backoff sleep with an instant recorder."""
    slept: list[float] = []

    async def _record(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(connection_mod, "_async_sleep", _record)
    return slept


@pytest.fixture
async def sconn(tmp_path):
    """SerializedConnection over a real file DB (no reconnect_fn — retry tests
    assert pre-reconnect behavior; recovery tests own that seam)."""
    db_path = tmp_path / "retry.db"
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("CREATE TABLE t(x)")
    await conn.commit()
    sc = SerializedConnection(conn)
    yield sc
    import contextlib

    with contextlib.suppress(Exception):
        await sc.close()


# ── predicate ────────────────────────────────────────────────────────────────


def test_lock_predicate_matches_both_variants_case_insensitive():
    assert _is_lock_error(sqlite3.OperationalError("database is locked"))
    assert _is_lock_error(sqlite3.OperationalError("database table is locked"))
    assert _is_lock_error(sqlite3.OperationalError("Database Is LOCKED"))
    assert not _is_lock_error(sqlite3.OperationalError("no such table: t"))
    assert not _is_lock_error(ValueError("database is locked"))  # wrong type


# ── schedule ─────────────────────────────────────────────────────────────────


async def test_transient_lock_retries_with_jittered_backoff(sconn, recorded_sleeps):
    """Fail twice then succeed: caller sees success; the two recorded delays sit
    inside their configured jitter bands."""
    original = sconn._conn.execute
    calls = 0

    async def flaky(sql, params=None):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise sqlite3.OperationalError("database is locked")
        return await original(sql, params)

    sconn._conn.execute = flaky

    await sconn.execute("INSERT INTO t VALUES (1)")
    assert calls == 3
    assert len(recorded_sleeps) == 2
    for delay, base in zip(recorded_sleeps, _WRITE_RETRY_DELAYS, strict=False):
        assert base * _JITTER_LOW <= delay <= base * _JITTER_HIGH


async def test_exhaustion_raises_after_exact_attempts_and_warns(sconn, recorded_sleeps, caplog):
    calls = 0

    async def always_fail(sql, params=None):
        nonlocal calls
        calls += 1
        raise sqlite3.OperationalError("database is locked")

    sconn._conn.execute = always_fail

    with (
        caplog.at_level(logging.WARNING, logger="genesis.db.connection"),
        pytest.raises(sqlite3.OperationalError, match="locked"),
    ):
        await sconn.execute("INSERT INTO t VALUES (1)")

    assert calls == ATTEMPTS_PER_EPISODE
    assert len(recorded_sleeps) == len(_WRITE_RETRY_DELAYS)
    assert any("DB lock persisted" in r.getMessage() for r in caplog.records)


async def test_non_lock_operational_error_not_retried(sconn, recorded_sleeps):
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        await sconn.execute("INSERT INTO missing VALUES (1)")
    assert recorded_sleeps == []
    assert sconn._consecutive_errors == 0


async def test_table_locked_variant_is_retried(sconn, recorded_sleeps):
    """SQLITE_LOCKED ("database table is locked") now retries too — the
    predicate widening over the old case-sensitive 'locked in str(e)' check."""
    original = sconn._conn.execute
    calls = 0

    async def flaky(sql, params=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database table is locked")
        return await original(sql, params)

    sconn._conn.execute = flaky
    await sconn.execute("INSERT INTO t VALUES (1)")
    assert calls == 2
    assert len(recorded_sleeps) == 1


# ── per-method routing ───────────────────────────────────────────────────────


async def test_commit_retried(sconn, recorded_sleeps):
    original = sconn._conn.commit
    calls = 0

    async def flaky_commit():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return await original()

    sconn._conn.commit = flaky_commit
    await sconn.execute("INSERT INTO t VALUES (1)")
    await sconn.commit()
    assert calls == 2
    assert len(recorded_sleeps) == 1


async def test_rollback_retried(sconn, recorded_sleeps):
    """A locked ROLLBACK left in_transaction=True forever pre-PR-1 (no lock
    handling at all on this path)."""
    original = sconn._conn.rollback
    calls = 0

    async def flaky_rollback():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return await original()

    sconn._conn.rollback = flaky_rollback
    await sconn.execute("INSERT INTO t VALUES (1)")
    await sconn.rollback()
    assert calls == 2
    assert len(recorded_sleeps) == 1


async def test_executemany_generator_arg_survives_retry(sconn, recorded_sleeps):
    """A generator parameters arg must be materialized BEFORE attempt 1 — a
    consumed generator would make the retry 'succeed' with zero rows."""
    original = sconn._conn.executemany
    calls = 0

    async def flaky(sql, params):
        nonlocal calls
        calls += 1
        if calls == 1:
            # Consume like the real driver would before failing.
            list(params)
            raise sqlite3.OperationalError("database is locked")
        return await original(sql, params)

    sconn._conn.executemany = flaky

    rows = ((i,) for i in range(5))  # deliberately a generator
    await sconn.executemany("INSERT INTO t VALUES (?)", rows)
    await sconn.commit()

    cur = await sconn.execute("SELECT count(*) AS c FROM t")
    assert (await cur.fetchone())["c"] == 5
    assert calls == 2


async def test_executescript_not_retried(sconn, recorded_sleeps):
    """Scripts can partially apply before the lock error — retry is not
    idempotent, so executescript keeps count-and-reraise on the FIRST error."""
    calls = 0

    async def fail_script(sql):
        nonlocal calls
        calls += 1
        raise sqlite3.OperationalError("database is locked")

    sconn._conn.executescript = fail_script

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        await sconn.executescript("INSERT INTO t VALUES (1); INSERT INTO t VALUES (2);")

    assert calls == 1  # no retry
    assert recorded_sleeps == []
    assert sconn._consecutive_errors == 1  # still counted


async def test_cancellation_during_backoff_propagates(sconn, monkeypatch):
    """CancelledError raised while sleeping between retries must propagate
    (the route-timeout path) — never swallowed or converted into a retry."""

    async def cancelled_sleep(_delay: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(connection_mod, "_async_sleep", cancelled_sleep)

    async def always_locked(sql, params=None):
        raise sqlite3.OperationalError("database is locked")

    sconn._conn.execute = always_locked

    with pytest.raises(asyncio.CancelledError):
        await sconn.execute("INSERT INTO t VALUES (1)")


# ── env knob ─────────────────────────────────────────────────────────────────


class TestDbBusyTimeoutMs:
    def test_default_without_env(self, monkeypatch):
        from genesis.db.connection import BUSY_TIMEOUT_MS
        from genesis.env import db_busy_timeout_ms

        monkeypatch.delenv("GENESIS_DB_BUSY_TIMEOUT_MS", raising=False)
        assert db_busy_timeout_ms() == BUSY_TIMEOUT_MS

    def test_override(self, monkeypatch):
        from genesis.env import db_busy_timeout_ms

        monkeypatch.setenv("GENESIS_DB_BUSY_TIMEOUT_MS", "15000")
        assert db_busy_timeout_ms() == 15000

    def test_floor_prevents_instant_failure_typo(self, monkeypatch):
        from genesis.env import db_busy_timeout_ms

        monkeypatch.setenv("GENESIS_DB_BUSY_TIMEOUT_MS", "5")
        assert db_busy_timeout_ms() == 100

    def test_garbage_falls_back_to_default(self, monkeypatch):
        from genesis.db.connection import BUSY_TIMEOUT_MS
        from genesis.env import db_busy_timeout_ms

        monkeypatch.setenv("GENESIS_DB_BUSY_TIMEOUT_MS", "fifteen-seconds")
        assert db_busy_timeout_ms() == BUSY_TIMEOUT_MS

    async def test_get_db_applies_env_override(self, tmp_path, monkeypatch):
        """The knob reaches the actual PRAGMA on a get_db connection."""
        from genesis.db.connection import get_db

        monkeypatch.setenv("GENESIS_DB_BUSY_TIMEOUT_MS", "7500")
        db = await get_db(tmp_path / "knob.db")
        try:
            cur = await db.execute("PRAGMA busy_timeout")
            assert (await cur.fetchone())[0] == 7500
        finally:
            await db.close()


# ── real contention (event-driven, no wall-clock asserts) ────────────────────


async def test_real_contention_write_succeeds_after_holder_releases(tmp_path, monkeypatch):
    """A concurrent BEGIN IMMEDIATE holder makes the first attempt lose for
    real (busy_timeout floored low so SQLite gives up fast); the holder commits
    when the retry loop reaches its first backoff sleep, so the retry wins.
    Event-driven choreography — the test never sleeps a fixed wall-clock time."""
    db_path = tmp_path / "contended.db"
    setup = await aiosqlite.connect(str(db_path))
    await setup.execute("PRAGMA journal_mode=WAL")
    await setup.execute("CREATE TABLE t(x)")
    await setup.commit()
    await setup.close()

    holder = await aiosqlite.connect(str(db_path))
    writer_raw = await aiosqlite.connect(str(db_path))
    # Tiny busy_timeout so the LOSING attempt is fast; the retry provides the
    # patience instead (the exact division of labor PR-1 ships).
    await writer_raw.execute("PRAGMA busy_timeout=50")
    writer = SerializedConnection(writer_raw)

    released = asyncio.Event()
    first_backoff = asyncio.Event()

    async def signalling_sleep(_delay: float) -> None:
        first_backoff.set()
        await released.wait()  # resume only after the holder is gone

    monkeypatch.setattr(connection_mod, "_async_sleep", signalling_sleep)

    async def release_on_first_backoff():
        await first_backoff.wait()
        await holder.commit()  # releases the write lock
        released.set()

    try:
        await holder.execute("BEGIN IMMEDIATE")
        await holder.execute("INSERT INTO t VALUES (99)")

        releaser = asyncio.ensure_future(release_on_first_backoff())
        # First attempt loses (holder owns the writer slot) → backoff signals
        # the releaser → holder commits → retry succeeds.
        await writer.execute("INSERT INTO t VALUES (1)")
        await writer.commit()
        await releaser

        cur = await writer.execute("SELECT count(*) FROM t")
        assert (await cur.fetchone())[0] == 2  # holder's row + writer's row
        assert writer._consecutive_errors == 0
    finally:
        import contextlib

        with contextlib.suppress(Exception):
            await holder.close()
        with contextlib.suppress(Exception):
            await writer.close()
