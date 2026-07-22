"""Write-lock hardening for the data-migration framework.

Covers the two mechanisms that keep a bulk migration from starving the live
server's single WAL writer (regression: d0006 held the write lock ~13s and the
server + its own ledger write failed with "database is locked", #1179):

1. ``_util.commit_in_batches`` — commits per batch so the write lock is released
   between batches (proven by a separate reader observing committed batches
   mid-run).
2. ``runner._ledger_write`` — retries the ledger bookkeeping write on a transient
   lock, and is best-effort (never raises) so a lost bookkeeping write only costs
   an idempotent no-op re-run.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3

import pytest

from genesis.db.data_migrations import runner
from genesis.db.data_migrations._util import DEFAULT_BATCH_SIZE, commit_in_batches


def _wal_db(tmp_path, n: int) -> tuple[str, sqlite3.Connection]:
    path = str(tmp_path / "b.db")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")  # match prod; WAL readers don't block the writer
    conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY)")
    conn.executemany("INSERT INTO t(id) VALUES (?)", [(i,) for i in range(n)])
    conn.commit()
    return path, conn


# --- commit_in_batches ------------------------------------------------------


def test_commits_incrementally(tmp_path):
    # A SEPARATE reader connection must observe committed batches WHILE the
    # migration is still running — proof the write lock is released per batch,
    # not held for the whole loop.
    path, conn = _wal_db(tmp_path, 250)
    peek = sqlite3.connect(path)
    seen: list[int] = []

    def _apply(c: sqlite3.Connection, i: int) -> None:
        c.execute("DELETE FROM t WHERE id = ?", (i,))
        seen.append(peek.execute("SELECT COUNT(*) FROM t").fetchone()[0])

    applied = commit_in_batches(conn, list(range(250)), _apply, batch_size=100)
    conn.close()
    peek.close()

    assert applied == 250
    # After batch 1 commits, the reader sees 150 remaining; after batch 2, 50.
    assert 150 in seen
    assert 50 in seen


def test_applies_all_including_final_partial_batch(tmp_path):
    path, conn = _wal_db(tmp_path, 5)
    applied = commit_in_batches(
        conn,
        list(range(5)),
        lambda c, i: c.execute("DELETE FROM t WHERE id = ?", (i,)),
        batch_size=2,
    )
    remaining = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    conn.close()
    assert applied == 5
    assert remaining == 0  # the trailing partial batch (1 leftover) is flushed


def test_batch_size_is_clamped_to_at_least_one(tmp_path):
    path, conn = _wal_db(tmp_path, 3)
    applied = commit_in_batches(
        conn,
        [0, 1, 2],
        lambda c, i: c.execute("DELETE FROM t WHERE id = ?", (i,)),
        batch_size=0,
    )
    remaining = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    conn.close()
    assert applied == 3
    assert remaining == 0


def test_empty_items_is_a_noop(tmp_path):
    path, conn = _wal_db(tmp_path, 0)
    applied = commit_in_batches(conn, [], lambda c, i: pytest.fail("apply must not be called"))
    conn.close()
    assert applied == 0


def test_apply_may_skip_writes_but_still_paces(tmp_path):
    # An item whose apply does no write still counts toward the total (and the
    # batch cadence) — mirrors d0006 skipping a unit on a Qdrant failure.
    path, conn = _wal_db(tmp_path, 4)

    def _apply(c: sqlite3.Connection, i: int) -> None:
        if i % 2 == 0:
            c.execute("DELETE FROM t WHERE id = ?", (i,))

    applied = commit_in_batches(conn, list(range(4)), _apply, batch_size=2)
    remaining = {r[0] for r in conn.execute("SELECT id FROM t").fetchall()}
    conn.close()
    assert applied == 4
    assert remaining == {1, 3}  # odd ids skipped, even ids deleted


def test_default_batch_size_is_reasonable():
    assert DEFAULT_BATCH_SIZE == 100


# --- runner._ledger_write ---------------------------------------------------


def test_ledger_write_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(runner, "_LEDGER_RETRY_DELAY_S", 0)
    calls = {"n": 0}

    async def _flaky() -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")

    assert asyncio.run(runner._ledger_write(_flaky, what="mark_completed")) is True
    assert calls["n"] == 3  # failed twice on lock, succeeded on the third attempt


def test_ledger_write_gives_up_on_persistent_lock_without_raising(monkeypatch, caplog):
    monkeypatch.setattr(runner, "_LEDGER_RETRY_DELAY_S", 0)
    calls = {"n": 0}

    async def _always_locked() -> None:
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    with caplog.at_level(logging.ERROR, logger=runner.logger.name):
        # must NOT raise, and reports it did not record
        assert asyncio.run(runner._ledger_write(_always_locked, what="mark_failed")) is False

    assert calls["n"] == runner._LEDGER_LOCK_RETRIES  # exhausted the retry budget
    assert "ledger mark_failed write failed" in caplog.text


def test_ledger_write_does_not_retry_non_lock_errors(monkeypatch, caplog):
    monkeypatch.setattr(runner, "_LEDGER_RETRY_DELAY_S", 0)
    calls = {"n": 0}

    async def _other_error() -> None:
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: data_migrations")

    with caplog.at_level(logging.ERROR, logger=runner.logger.name):
        # must NOT raise, reports failure, and does not retry a non-lock error
        assert asyncio.run(runner._ledger_write(_other_error, what="mark_completed")) is False

    assert calls["n"] == 1  # a non-lock error is logged and NOT retried


def test_run_one_reports_failure_when_completed_ledger_write_fails(monkeypatch, caplog):
    # Codex P2 (#1190): when migrate()/verify() succeed but the 'completed' ledger
    # write can't land (persistent lock), _run_one must NOT report success — the
    # row stays 'running' and replays, so a false success would hide the failure.
    from pathlib import Path

    from genesis.db.data_migrations import d0006_purge_surplus_ops_telemetry as d6

    monkeypatch.setattr(d6, "migrate", lambda: {"purged": 0})
    monkeypatch.setattr(d6, "verify", lambda: True)
    monkeypatch.setattr(runner, "_LEDGER_RETRY_DELAY_S", 0)

    async def _locked(*_a, **_k) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(runner.crud, "mark_completed", _locked)

    r = runner.DataMigrationRunner(db=object())  # _db unused (mark_completed stubbed)
    with caplog.at_level(logging.ERROR, logger=runner.logger.name):
        outcome = asyncio.run(
            r._run_one("d0006", "d0006_purge_surplus_ops_telemetry", Path(d6.__file__))
        )

    assert outcome["success"] is False
    assert "ledger write failed" in outcome["error"]
    assert "will replay next boot" in caplog.text
