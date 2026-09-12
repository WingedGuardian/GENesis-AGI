"""WS-2 M9 — job_run_events debounce, duration honesty, and write isolation.

Per-run job history is written from record_job_success/failure with a debounce
anchored on the persisted job_health columns (last_success / last_failure /
consecutive_failures). These tests drive elapsed time by seeding those in-memory
columns directly (the debounce reads them BEFORE overwriting), so no global-clock
patching is needed. Real in-memory aiosqlite DB + tracked_task drain, matching
test_job_health.py.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.db.schema import create_all_tables
from genesis.runtime import GenesisRuntime


async def _drain_pending() -> None:
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _make_runtime(db: aiosqlite.Connection | None) -> GenesisRuntime:
    rt = GenesisRuntime.__new__(GenesisRuntime)
    rt._job_health = {}
    rt._db = db
    rt._job_retry_registry = None
    return rt


async def _events(db: aiosqlite.Connection, job_name: str) -> list[tuple]:
    async with db.execute(
        "SELECT status, run_started_at, duration_ms, error FROM job_run_events "
        "WHERE job_name = ? ORDER BY recorded_at",
        (job_name,),
    ) as cur:
        return list(await cur.fetchall())


async def _setup() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    await create_all_tables(db)
    await db.commit()
    return db


async def test_first_success_writes_run_event():
    db = await _setup()
    try:
        rt = _make_runtime(db)
        rt.record_job_success("job_a")
        await _drain_pending()
        rows = await _events(db, "job_a")
        assert len(rows) == 1
        assert rows[0][0] == "success"
    finally:
        await db.close()


async def test_second_success_within_hour_is_debounced():
    db = await _setup()
    try:
        rt = _make_runtime(db)
        rt.record_job_success("job_a")  # first → writes
        await _drain_pending()
        rt.record_job_success("job_a")  # immediately again → last_success ~now → suppressed
        await _drain_pending()
        rows = await _events(db, "job_a")
        assert len(rows) == 1, "sub-hourly repeat success must not write a second event"
    finally:
        await db.close()


async def test_success_after_an_hour_writes_again():
    db = await _setup()
    try:
        rt = _make_runtime(db)
        rt.record_job_success("job_a")  # first
        await _drain_pending()
        # Simulate >1h elapsed by backdating the in-memory last_success anchor.
        rt._job_health["job_a"]["last_success"] = (
            datetime.now(UTC) - timedelta(hours=2)
        ).isoformat()
        rt.record_job_success("job_a")  # now > 1h since anchor → writes
        await _drain_pending()
        rows = await _events(db, "job_a")
        assert len(rows) == 2
    finally:
        await db.close()


async def test_failure_onset_writes_event_with_error():
    """Injected-failure E2E: a job failure lands a status='failed' run-event with the error."""
    db = await _setup()
    try:
        rt = _make_runtime(db)
        rt.record_job_failure("job_b", "boom")
        await _drain_pending()
        rows = await _events(db, "job_b")
        assert len(rows) == 1
        assert rows[0][0] == "failed"
        assert rows[0][3] == "boom"
    finally:
        await db.close()


async def test_repeated_failure_within_hour_is_debounced():
    db = await _setup()
    try:
        rt = _make_runtime(db)
        rt.record_job_failure("job_b", "boom")  # onset → writes
        await _drain_pending()
        rt.record_job_failure("job_b", "boom again")  # consec=1, within 1h → suppressed
        await _drain_pending()
        rows = await _events(db, "job_b")
        assert len(rows) == 1, "sustained sub-hourly failures debounce to onset only"
    finally:
        await db.close()


async def test_failure_heartbeat_after_an_hour():
    db = await _setup()
    try:
        rt = _make_runtime(db)
        rt.record_job_failure("job_b", "boom")  # onset
        await _drain_pending()
        # Simulate a sustained outage: streak continues, last failure >1h ago.
        rt._job_health["job_b"]["last_failure"] = (
            datetime.now(UTC) - timedelta(hours=2)
        ).isoformat()
        rt.record_job_failure("job_b", "still broken")  # heartbeat → writes
        await _drain_pending()
        rows = await _events(db, "job_b")
        assert len(rows) == 2, "a sustained outage emits an hourly heartbeat row"
    finally:
        await db.close()


async def test_duration_is_none_without_start_marker():
    db = await _setup()
    try:
        rt = _make_runtime(db)
        rt.record_job_success("job_c")  # no record_job_start → no honest duration
        await _drain_pending()
        rows = await _events(db, "job_c")
        assert rows[0][2] is None, "duration must be NULL, never derived from last_run"
    finally:
        await db.close()


async def test_duration_populated_from_start_marker():
    db = await _setup()
    try:
        rt = _make_runtime(db)
        rt.record_job_start("job_c")
        # Backdate the in-memory start marker so the computed duration is positive.
        rt._job_health["job_c"]["run_started_at"] = (
            datetime.now(UTC) - timedelta(milliseconds=500)
        ).isoformat()
        rt.record_job_success("job_c")
        await _drain_pending()
        rows = await _events(db, "job_c")
        assert rows[0][2] is not None and rows[0][2] >= 0, "duration should be real when started"
    finally:
        await db.close()


async def test_run_event_write_failure_never_throws():
    """A missing table (write path broken) must not propagate into the ~62 callers."""
    db = await aiosqlite.connect(":memory:")
    try:
        # job_health exists (full canonical schema), job_run_events does NOT —
        # the write path is deliberately broken to prove failures are swallowed.
        await create_all_tables(db)
        await db.execute("DROP TABLE job_run_events")
        await db.commit()
        rt = _make_runtime(db)
        # Must not raise despite the missing job_run_events table.
        rt.record_job_success("job_d")
        rt.record_job_failure("job_d", "boom")
        await _drain_pending()
    finally:
        await db.close()
