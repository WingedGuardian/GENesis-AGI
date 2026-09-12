"""Job health persistence — failure-state clearing on recovery (WS-3b).

Regression coverage for the bug where the ``job_health`` UPSERT used
``COALESCE(excluded.last_failure, last_failure)``, so ``record_job_success``
(which carries NULL failure columns) never overwrote a prior failure — leaving
the row showing a permanent failure long after the job recovered.

These tests exercise the REAL SQL against an in-memory aiosqlite DB (existing
runtime tests mock ``_db``, so the UPSERT itself was never covered).
"""

from __future__ import annotations

import asyncio

import aiosqlite

from genesis.db.schema import create_all_tables
from genesis.runtime import GenesisRuntime
from genesis.runtime._job_health import clear_stale_job_failures


async def _drain_pending() -> None:
    """Await scheduled tracked_task background writes so the DB is settled."""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _make_runtime(db: aiosqlite.Connection) -> GenesisRuntime:
    rt = GenesisRuntime.__new__(GenesisRuntime)
    rt._job_health = {}
    rt._db = db
    rt._job_retry_registry = None
    return rt


async def _fetch(db: aiosqlite.Connection, job_name: str):
    async with db.execute(
        "SELECT last_success, last_failure, last_error, consecutive_failures, "
        "total_runs, total_successes, total_failures "
        "FROM job_health WHERE job_name = ?",
        (job_name,),
    ) as cur:
        return await cur.fetchone()


async def test_success_clears_persisted_failure():
    """A job that fails then succeeds must not retain a stale last_failure/last_error."""
    async with aiosqlite.connect(":memory:") as db:
        await create_all_tables(db)
        await db.commit()
        rt = _make_runtime(db)

        # 1. Job fails — failure columns populated.
        rt.record_job_failure("job_x", "boom")
        await _drain_pending()
        row = await _fetch(db, "job_x")
        assert row is not None, "failure write did not persist"
        last_success, last_failure, last_error, consec = row[0], row[1], row[2], row[3]
        assert last_failure is not None
        assert last_error == "boom"
        assert consec == 1

        # 2. Job recovers — failure columns MUST clear, success recorded.
        rt.record_job_success("job_x")
        await _drain_pending()
        row = await _fetch(db, "job_x")
        last_success, last_failure, last_error, consec = row[0], row[1], row[2], row[3]
        assert last_failure is None, "last_failure not cleared on recovery (WS-3b)"
        assert last_error is None, "last_error not cleared on recovery (WS-3b)"
        assert last_success is not None
        assert consec == 0


async def test_failure_path_unbroken_and_success_preserved():
    """The fix must not break the failure path, and last_success must survive a failure.

    Guards that dropping COALESCE on last_failure/last_error does not NULL them on a
    real failure, while COALESCE on last_success still preserves a prior success.
    """
    async with aiosqlite.connect(":memory:") as db:
        await create_all_tables(db)
        await db.commit()
        rt = _make_runtime(db)

        rt.record_job_success("job_y")  # prior success
        await _drain_pending()
        rt.record_job_failure("job_y", "kaboom")  # then a failure
        await _drain_pending()

        row = await _fetch(db, "job_y")
        last_success, last_failure, last_error, consec = row[0], row[1], row[2], row[3]
        assert last_failure is not None, "failure must persist"
        assert last_error == "kaboom"
        assert last_success is not None, "prior success must be preserved across a failure"
        assert consec == 1


async def test_clear_stale_failures_clears_persisted_failure():
    """The bootstrap stale-failure sweep must also NULL the persisted failure columns.

    clear_stale_job_failures() is the third caller that pops last_failure/last_error to
    signal recovery; it persists via the same UPSERT and must not retain stale failures.
    """
    async with aiosqlite.connect(":memory:") as db:
        await create_all_tables(db)
        await db.commit()
        rt = _make_runtime(db)

        rt.record_job_failure("job_z", "stale boom")
        await _drain_pending()
        row = await _fetch(db, "job_z")
        assert row[1] is not None and row[3] == 1  # failure persisted, consec==1

        # Simulate the post-restart stale-failure sweep.
        cleared = clear_stale_job_failures(rt)
        await _drain_pending()
        assert cleared == 1

        row = await _fetch(db, "job_z")
        last_failure, last_error, consec = row[1], row[2], row[3]
        assert last_failure is None, "clear_stale must NULL persisted last_failure (WS-3b)"
        assert last_error is None
        assert consec == 0
