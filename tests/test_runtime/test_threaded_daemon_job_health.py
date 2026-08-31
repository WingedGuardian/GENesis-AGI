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

    thread = threading.Thread(target=_worker, name="tick-worker")
    thread.start()
    deadline = asyncio.get_running_loop().time() + 10
    while thread.is_alive():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("daemon tick did not complete within 10s")
        await asyncio.sleep(0.01)
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

    thread = threading.Thread(target=_worker, name="drive-worker")
    thread.start()
    deadline = asyncio.get_running_loop().time() + 15
    while thread.is_alive():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("worker thread did not finish within 15s")
        await asyncio.sleep(0.01)
    thread.join(timeout=5)
    return captured[0] if captured else None


async def _drive_wedged(fn) -> BaseException | None:
    """Run fn() on a worker thread while the event loop is genuinely BLOCKED.

    This is the difference between modelling a stall and merely running beside
    one. ``_drive`` keeps the loop turning (it polls with a 10 ms sleep), so a
    record submitted by the worker CAN complete mid-burst -- and coalescing is
    still satisfied there, because it is a CONCURRENCY bound; what does not hold
    in that case is a cumulative count of one. A test that asserts the count must
    create the "submitted and unable to run" state, not hope for it.

    Sequencing, and every step of it is load-bearing:
      * the worker waits on ``entered`` so it cannot submit before the wedge;
      * ``call_soon`` enqueues ``_blocker`` BEFORE this task's own resume handle
        and the ready queue is FIFO (it appends to the TAIL, so this is an
        ordering guarantee relative to the resume, never "runs first overall"),
        so the single ``sleep(0)`` below is enough for the loop to enter it;
      * while ``_blocker`` sits in ``released.wait()`` the loop runs NOTHING --
        ``run_coroutine_threadsafe`` still enqueues, but nothing drains it;
      * the worker sets ``released`` when it is done, and only then does the
        loop resume and run whatever was queued.
    Both waits are deadline-bounded so a regression fails the test instead of
    hanging the suite.
    """
    entered = threading.Event()
    released = threading.Event()
    captured: list[BaseException] = []
    # Guard the guard: records whether the wedge was still held when the worker
    # released it. Without this, an ordering regression (a sleep(0) that becomes
    # sleep(0.01), a call_soon moved after the await, a different loop policy)
    # degrades this test silently back into the ~50% flake it was written to
    # remove -- and the failures would read as a coalescing regression, sending
    # the next session to debug production code that is fine.
    wedge_held: list[bool] = []

    def _blocker() -> None:
        entered.set()
        wedge_held.append(released.wait(timeout=15))

    def _worker() -> None:
        try:
            # STRICTLY smaller than the join bound below, or this message can
            # never print: the loop only reaches the join before _blocker runs
            # or after it returns, so a 15s wait here would always lose the race
            # to a 5s join and report the wrong cause.
            if not entered.wait(timeout=2):
                raise AssertionError("event loop never entered the wedge")
            fn()
        except BaseException as exc:  # noqa: BLE001 - returned to the caller
            captured.append(exc)
        finally:
            released.set()

    thread = threading.Thread(target=_worker, name="wedge-worker", daemon=True)
    thread.start()
    asyncio.get_running_loop().call_soon(_blocker)
    await asyncio.sleep(0)  # yield: the loop enters _blocker and stays there
    thread.join(timeout=5)
    if thread.is_alive():
        raise AssertionError("worker thread did not finish within 5s")
    if not wedge_held or not wedge_held[0]:
        raise AssertionError(
            "the wedge broke before the worker finished -- this test's "
            "precondition did not hold, so its result proves nothing"
        )
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

    Unbounded submission was the previous shape. During a wedge every tick times
    out and queues another record, so a long stall lands them all at once on
    recovery -- spiking consecutive_failures, permanently inflating total_failures,
    and firing a retry per record where a retry registry is wired. "The daemon is
    failing" is ONE fact however long the stall lasts.

    The repeats are submitted while the event loop is genuinely WEDGED, which is
    what a stall looks like from the daemon's side: the first record is scheduled
    and CANNOT run, so the rest must be absorbed rather than queued.

    That wedge is the whole point, and an earlier version of this test only
    described it. It used ``_drive``, which keeps the loop turning, so the first
    record could complete mid-burst -- at which point ``_record_failure`` is
    SUPPOSED to submit another, and the assertion below failed. MEASURED at the
    time: ~50% locally (6 of 12 runs), on either parametrization, and twice on
    main's own CI. The mechanism guarantees a CONCURRENCY bound -- at most one
    record IN FLIGHT -- and the cumulative count asserted here is only equivalent
    to it while nothing can drain. ``_drive_wedged`` establishes that precondition
    instead of hoping for it.
    """
    calls: list[str] = []

    class _CountingRuntime:
        def record_job_failure(self, job_name, *args, **kwargs):
            calls.append(job_name)

    daemon = factory(interval_seconds=60)
    daemon._loop = asyncio.get_running_loop()
    rt = _CountingRuntime()

    def _five_failures() -> None:
        for n in range(5):
            daemon._record_failure(rt, RuntimeError(str(n)))

    assert await _drive_wedged(_five_failures) is None
    assert not calls, (
        f"the loop drained {len(calls)} record(s) DURING the supposed wedge -- "
        "the precondition broke, so the count below measures nothing"
    )
    for _ in range(200):
        if calls:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    await _drain_pending()

    assert len(calls) == 1, (
        f"expected the pending record to absorb the repeats, got {len(calls)} "
        "-- a wedge would queue one per tick and burst on recovery"
    )


@pytest.mark.parametrize(
    ("factory",),
    [(OutreachHeartbeat,), (DashboardHeartbeat,)],
    ids=["outreach", "dashboard"],
)
async def test_a_completed_record_re_arms_for_the_next_failure(factory):
    """Coalescing is a CONCURRENCY bound, NOT a one-record-per-process latch.

    The wedged test above pins ``pending.done()`` to False for its whole burst,
    which is what makes its count assertion meaningful -- and which also makes it
    structurally blind to the other half of the same predicate. MEASURED: with
    only that test, weakening the guard to ``if pending is not None: return``
    leaves the entire file green, while production stops recording after its
    FIRST failure for the lifetime of the process -- consecutive_failures frozen
    at 1, last_failure frozen at the first stall, and a wired retry registry
    never firing again.

    That is also the ORDINARY case, not the exotic one: ``_run`` calls
    ``_record_failure`` once per interval (60s default), so a healthy loop with a
    throwing emit completes each record long before the next tick and depends on
    the re-arm entirely.

    This test is deliberately NOT racy -- it drives the first record to
    COMPLETION before submitting the second, so it asserts a state rather than a
    timing window.
    """
    calls: list[str] = []

    class _CountingRuntime:
        def record_job_failure(self, job_name, *args, **kwargs):
            calls.append(job_name)

    daemon = factory(interval_seconds=60)
    daemon._loop = asyncio.get_running_loop()
    rt = _CountingRuntime()

    assert await _drive(lambda: daemon._record_failure(rt, RuntimeError("a"))) is None
    for _ in range(200):
        if calls:
            break
        await asyncio.sleep(0.01)
    await _drain_pending()
    # Guard the guard: if the first record never landed, the re-arm assertion
    # below would pass for the wrong reason.
    assert len(calls) == 1, "the first record never landed -- this test proves nothing"
    # The first record must not still be IN FLIGHT, or the second submission
    # would be absorbed and this would test coalescing again rather than re-arm.
    # Deliberately permissive about HOW: retaining the completed future and
    # clearing it in a done-callback are both valid re-arms, and the callback
    # form is arguably better since it stops holding the exception. Asserting
    # `is not None and .done()` would have failed a correct implementation --
    # over-constraining the mechanism instead of pinning the behaviour.
    pending = daemon._pending_failure
    assert pending is None or pending.done(), (
        "the first failure record is still in flight, so the second submission "
        "below would exercise coalescing rather than the re-arm branch"
    )

    assert await _drive(lambda: daemon._record_failure(rt, RuntimeError("b"))) is None
    for _ in range(200):
        if len(calls) == 2:
            break
        await asyncio.sleep(0.01)
    await _drain_pending()

    assert len(calls) == 2, (
        f"the COMPLETED pending record was never re-armed, got {len(calls)} -- a "
        "daemon that keeps failing would record only its first failure, freezing "
        "consecutive_failures and never re-firing the retry registry"
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
