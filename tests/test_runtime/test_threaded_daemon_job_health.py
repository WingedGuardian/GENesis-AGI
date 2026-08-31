"""Threaded heartbeat daemons must PERSIST job health, not only record it in memory.

Regression coverage for a silent data-loss path found live on 2026-08-29: both
heartbeat daemons called ``rt.record_job_success()`` AFTER closing the throwaway
event loop they had created for the emit. ``persist_job_health`` returns early
when no loop is running, so the ``job_health`` ROW was never written and the
``job_health`` tool -- which reads sqlite -- reported the job as if it had never
run. Measured: neither ``outreach_heartbeat`` nor ``dashboard_heartbeat``
appeared among 89 persisted job rows while both daemons were pulsing normally.

The assertion is deliberately on the ROW, never on the call. ``record_job_success``
was ALREADY being called before the fix, so a test asserting the call passes just
as happily on the broken code -- the vacuous version of this test.

Both daemons are covered because the population is exactly two: they were the only
files in ``src/genesis/`` that both call ``record_job_success`` and use
``threading.Thread``, and the outreach daemon was modelled on the dashboard one,
inheriting the defect verbatim.
"""

from __future__ import annotations

import asyncio
import threading
import time

import aiosqlite
import pytest

from genesis.dashboard.heartbeat import DashboardHeartbeat
from genesis.db.schema import create_all_tables
from genesis.observability.types import Severity, Subsystem
from genesis.outreach.heartbeat import OutreachHeartbeat
from genesis.runtime import GenesisRuntime


class _FakeBus:
    """Minimal event bus: records emits, and is awaitable like the real one."""

    def __init__(self) -> None:
        self.emitted: list[tuple] = []

    async def emit(self, subsystem, severity, event_type, message, **kwargs):
        self.emitted.append((subsystem, event_type, message))


def _make_runtime(db: aiosqlite.Connection, bus: _FakeBus) -> GenesisRuntime:
    rt = GenesisRuntime.__new__(GenesisRuntime)
    rt._job_health = {}
    rt._db = db
    rt._job_retry_registry = None
    rt._event_bus = bus  # event_bus is a read-only property over this
    return rt


async def _drain_pending() -> None:
    """Await the tracked_task background writes so the DB is settled."""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def _run_tick_from_worker_thread(daemon, rt) -> None:
    """Drive one tick from a WORKER THREAD, as in production.

    The main loop must keep running while the worker schedules onto it, so this
    yields until the thread finishes -- bounded, so a regression that never
    completes fails the test instead of hanging the suite forever.
    """
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            daemon._tick(rt, Subsystem, Severity)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    # The deadline is DERIVED, not picked: this drives _tick, which waits on
    # HeartbeatDaemon._TICK_TIMEOUT_S (30s). An outer bound below the inner one
    # fails a legitimately-slow-but-working tick and blames the wrong thing, so it
    # must exceed 30s. daemon=True + join-in-finally for the same reason as _drive.
    thread = threading.Thread(target=_worker, name="tick-worker", daemon=True)
    thread.start()
    try:
        deadline = asyncio.get_running_loop().time() + 45
        while thread.is_alive():
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("daemon tick did not complete within 45s")
            await asyncio.sleep(0.01)
    finally:
        thread.join(timeout=5)
    if errors:
        raise errors[0]


@pytest.mark.parametrize(
    ("factory", "job_name", "subsystem"),
    [
        (OutreachHeartbeat, "outreach_heartbeat", Subsystem.OUTREACH),
        (DashboardHeartbeat, "dashboard_heartbeat", Subsystem.DASHBOARD),
    ],
    ids=["outreach", "dashboard"],
)
async def test_tick_persists_a_job_health_row(factory, job_name, subsystem):
    """One tick on the runtime's main loop must leave a job_health ROW behind."""
    async with aiosqlite.connect(":memory:") as db:
        await create_all_tables(db)
        await db.commit()
        bus = _FakeBus()
        rt = _make_runtime(db, bus)

        daemon = factory(interval_seconds=60)
        daemon._loop = asyncio.get_running_loop()  # what start() captures

        await _run_tick_from_worker_thread(daemon, rt)
        await _drain_pending()

        # Guard the guard: the tick must actually have emitted, or a missing row
        # below would prove nothing about persistence.
        assert bus.emitted, "daemon did not emit -- test proves nothing"
        assert bus.emitted[0][0] is subsystem

        async with db.execute(
            "SELECT last_success FROM job_health WHERE job_name = ?", (job_name,)
        ) as cur:
            row = await cur.fetchone()

        assert row is not None, (
            f"{job_name} left NO job_health row -- record_job_success ran without a "
            "live event loop, so persist_job_health dropped the write silently"
        )
        assert row[0] is not None, f"{job_name} row has no last_success timestamp"


@pytest.mark.parametrize(
    ("factory", "job_name"),
    [(OutreachHeartbeat, "outreach_heartbeat"), (DashboardHeartbeat, "dashboard_heartbeat")],
    ids=["outreach", "dashboard"],
)
async def test_tick_without_a_captured_loop_still_emits(factory, job_name):
    """The no-live-loop fallback must still pulse rather than raise.

    Liveness detection reads heartbeat EVENTS, so the fallback losing only the
    job_health row is a documented degradation -- but it must never cost the
    pulse itself, which is the daemon's whole purpose.
    """
    async with aiosqlite.connect(":memory:") as db:
        await create_all_tables(db)
        await db.commit()
        bus = _FakeBus()
        rt = _make_runtime(db, bus)

        daemon = factory(interval_seconds=60)
        daemon._loop = None  # start() ran outside a loop

        await _run_tick_from_worker_thread(daemon, rt)

        assert bus.emitted, f"{job_name} fallback path lost the pulse"


async def _drive(fn) -> BaseException | None:
    """Run fn() on a worker thread while this loop keeps turning.

    Returns the exception fn raised, or None. Deadline-bounded so a regression
    fails the test instead of hanging the suite.
    """
    captured: list[BaseException] = []

    def _worker() -> None:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - returned to the caller
            captured.append(exc)

    # daemon=True so a worker still stuck at the deadline below can never hold up
    # interpreter shutdown; the join is in a `finally` so the deadline path still
    # reaps it rather than walking away from a live thread.
    thread = threading.Thread(target=_worker, name="drive-worker", daemon=True)
    thread.start()
    try:
        deadline = asyncio.get_running_loop().time() + 15
        while thread.is_alive():
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("worker thread did not finish within 15s")
            await asyncio.sleep(0.01)
    finally:
        thread.join(timeout=5)
    return captured[0] if captured else None


@pytest.mark.parametrize(
    ("factory", "job_name"),
    [(OutreachHeartbeat, "outreach_heartbeat"), (DashboardHeartbeat, "dashboard_heartbeat")],
    ids=["outreach", "dashboard"],
)
async def test_failed_tick_persists_a_failure_row(factory, job_name):
    """The FAILURE direction must persist too, not only the success direction.

    record_job_failure goes through the same loop-dependent persistence path as
    record_job_success. Fixing only the success path would leave half the defect
    in place -- and the failure half is the one an operator most needs to see.
    """
    async with aiosqlite.connect(":memory:") as db:
        await create_all_tables(db)
        await db.commit()
        rt = _make_runtime(db, _FakeBus())

        daemon = factory(interval_seconds=60)
        daemon._loop = asyncio.get_running_loop()

        raised = await _drive(lambda: daemon._record_failure(rt, RuntimeError("emit exploded")))
        assert raised is None, f"recording the failure itself raised: {raised!r}"

        # The record is submitted to the loop and NOT waited on (deliberately -- see
        # test_failure_record_is_not_bounded_by_the_tick_timeout), so poll for the row
        # rather than assuming it has landed. Bounded: if it never lands the assertion
        # below still fires, so this cannot mask the defect it guards.
        row = None
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            await _drain_pending()
            async with db.execute(
                "SELECT last_failure, last_error FROM job_health WHERE job_name = ?",
                (job_name,),
            ) as cur:
                row = await cur.fetchone()
            if row is not None:
                break
            await asyncio.sleep(0.02)

        assert row is not None, (
            f"{job_name} left NO job_health row for a FAILED tick -- "
            "record_job_failure ran without a live event loop"
        )
        assert row[0] is not None, f"{job_name} row has no last_failure timestamp"


@pytest.mark.parametrize(
    ("factory",),
    [(OutreachHeartbeat,), (DashboardHeartbeat,)],
    ids=["outreach", "dashboard"],
)
async def test_timed_out_tick_cancels_its_submission(factory):
    """A tick that outlives its timeout must be CANCELLED, not left pending.

    Future.result(timeout=...) does not cancel the underlying task. Without an
    explicit cancel, a stalled loop accumulates one pending tick per interval
    and they all complete in a burst on recovery -- emitting stale pulses.
    """
    cancelled = asyncio.Event()

    class _StallingBus:
        emitted: list = []

        async def emit(self, *args, **kwargs):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise

    async with aiosqlite.connect(":memory:") as db:
        await create_all_tables(db)
        await db.commit()
        rt = _make_runtime(db, _StallingBus())

        daemon = factory(interval_seconds=60)
        daemon._loop = asyncio.get_running_loop()
        daemon._TICK_TIMEOUT_S = 0.2  # instance attr shadows the class default

        raised = await _drive(lambda: daemon._tick(rt, Subsystem, Severity))
        assert isinstance(raised, TimeoutError), (
            f"expected the tick to time out, got {raised!r}"
        )

        # The cancellation is delivered on this loop; give it a bounded chance.
        await asyncio.wait_for(cancelled.wait(), timeout=5)
        assert cancelled.is_set(), "the timed-out submission was never cancelled"


@pytest.mark.parametrize(
    ("factory",),
    [(OutreachHeartbeat,), (DashboardHeartbeat,)],
    ids=["outreach", "dashboard"],
)
async def test_failure_record_is_not_bounded_by_the_tick_timeout(factory):
    """A slow failure record must NOT be abandoned and re-done off the loop.

    A tick is cancelled when it outlives its timeout, because a stale pulse is
    actively misleading. A failure record is the opposite: it stays true however
    late it lands, and the situation it reports -- a wedged loop -- is exactly
    when a bounded wait would expire. Bounding it would drop the record during
    the whole stall, leaving the persisted row showing the previous SUCCESS.

    Counting calls makes this timing-tolerant rather than racy: with a bounded
    wait the on-loop attempt runs AND the degraded fallback runs too (two calls),
    because the wait expires while the first is still in flight. Unbounded, the
    record is made exactly once.
    """
    calls: list[str] = []

    class _SlowRuntime:
        """Records on the loop, slowly enough to outlive a tiny timeout."""

        def record_job_failure(self, job_name, *args, **kwargs):
            calls.append(job_name)
            time.sleep(0.25)  # blocks the loop; a cancel cannot interrupt it

    daemon = factory(interval_seconds=60)
    daemon._loop = asyncio.get_running_loop()
    daemon._TICK_TIMEOUT_S = 0.05  # far below the work, so a bounded wait WOULD expire

    rt = _SlowRuntime()
    raised = await _drive(lambda: daemon._record_failure(rt, RuntimeError("boom")))
    assert raised is None, f"_record_failure raised: {raised!r}"

    # Let the submitted coroutine finish on this loop.
    for _ in range(200):
        if calls:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.35)

    assert len(calls) == 1, (
        f"expected exactly ONE failure record, got {len(calls)} -- a second call "
        "means the submission was abandoned on a timeout and re-done off the loop, "
        "where it does not persist"
    )


@pytest.mark.parametrize(
    ("factory",),
    [(OutreachHeartbeat,), (DashboardHeartbeat,)],
    ids=["outreach", "dashboard"],
)
async def test_start_captures_the_running_loop(factory):
    """start() must capture the loop, and every test until now assigned _loop by hand.

    That left the fix's ENTIRE activation condition uncovered: deleting the
    get_running_loop() capture would have left the whole suite green while
    production silently lost every job-health row again.
    """
    daemon = factory(interval_seconds=3600)
    daemon._stop_event.set()  # the worker exits immediately; we only inspect capture
    daemon.start()
    try:
        assert daemon._loop is asyncio.get_running_loop()
    finally:
        daemon.stop()


@pytest.mark.parametrize(
    ("factory",),
    [(OutreachHeartbeat,), (DashboardHeartbeat,)],
    ids=["outreach", "dashboard"],
)
async def test_repeated_failures_coalesce_into_one_pending_record(factory):
    """A stalled loop must not queue one failure record per tick.

    Unbounded submission was the previous shape. During a wedge every tick times out
    and queues another record, so a long stall lands them all at once on recovery --
    spiking consecutive_failures, permanently inflating total_failures, and firing a
    retry per record where a retry registry is wired. "The daemon is failing" is ONE
    fact however long the stall lasts.

    NO THREADS, deliberately. This assertion previously drove five failures from a
    worker thread and wedged the loop with a blocking wait to hold the first record
    open. That shape was FLAKY IN CI -- measured on the same main SHA, passing at
    04:32 and failing with "got 2" at 12:15 -- because it depended on the loop
    losing a race to the worker. Its bound, its thread-leak path and its potential
    for a worker/loop deadlock were all scaffolding, not coverage.

    The mechanism here is the event loop's own single-threadedness. ``_record_failure``
    submits via ``run_coroutine_threadsafe`` and returns WITHOUT waiting, so calling
    it five times from this coroutine with no ``await`` between the calls leaves the
    loop no opportunity to run any submitted record: this coroutine is what the loop
    is currently running. ``self._pending_failure`` therefore cannot become done
    between the calls, and the coalescing branch is reached BY CONSTRUCTION rather
    than by winning a race. It cannot flake.

    Nothing is lost by dropping the threads. Production calls ``_record_failure``
    from exactly ONE place (``_run``'s exception handler) on exactly ONE thread per
    daemon, so there is no concurrent-caller race for a threaded test to catch; and
    cross-thread invocation is already covered by
    ``test_failed_tick_persists_a_failure_row`` and
    ``test_failure_record_is_not_bounded_by_the_tick_timeout``, which both drive
    ``_record_failure`` through ``_drive`` from a worker thread.

    The pre-drain assertion is not decoration -- it proves the premise actually held.
    If a record had run early, the count below could pass for the wrong reason.
    """
    calls: list[str] = []

    class _CountingRuntime:
        def record_job_failure(self, job_name, *args, **kwargs):
            calls.append(job_name)

    daemon = factory(interval_seconds=60)
    daemon._loop = asyncio.get_running_loop()
    rt = _CountingRuntime()

    for n in range(5):
        daemon._record_failure(rt, RuntimeError(str(n)))

    assert not calls, (
        f"a record ran before the submissions finished, got {len(calls)} -- the "
        "loop was not held by this coroutine, so this test's premise is broken and "
        "the coalescing count below would prove nothing"
    )

    deadline = asyncio.get_running_loop().time() + 10
    while not calls and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    await _drain_pending()

    assert len(calls) == 1, (
        f"expected the pending record to absorb the repeats, got {len(calls)} "
        "-- unbounded submission queues one per tick and bursts on recovery"
    )


@pytest.mark.parametrize(
    ("factory",),
    [(OutreachHeartbeat,), (DashboardHeartbeat,)],
    ids=["outreach", "dashboard"],
)
async def test_double_start_is_refused_while_running(factory):
    """A second start() must not spawn a second pulsing thread.

    The guard keys on the existing thread being ALIVE, so this stands in a live
    one rather than starting a real worker: setting _stop_event first would let the
    worker exit immediately, and restarting a DEAD daemon is correct behaviour, not
    the case under test.
    """
    daemon = factory(interval_seconds=3600)
    release = threading.Event()
    sentinel = threading.Thread(target=lambda: release.wait(timeout=10), daemon=True)
    sentinel.start()
    daemon._thread = sentinel
    try:
        daemon.start()
        assert daemon._thread is sentinel, "double start() replaced a live thread"
    finally:
        release.set()
        sentinel.join(timeout=5)
