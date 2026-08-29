"""Dashboard heartbeat — emits periodic heartbeats to the event bus.

The web UI runs in the same process as all Genesis subsystems. If the Flask
app degrades (routes broken, event loop stalled) this heartbeat will stop,
allowing subsystem_heartbeats() to detect the issue.  If the entire process
dies, external monitoring (status.json file age, systemd watchdog) covers
the gap — this heartbeat handles the *degraded-but-alive* case.
"""

from __future__ import annotations

import asyncio
import logging
import threading

logger = logging.getLogger("genesis.dashboard.heartbeat")


class DashboardHeartbeat:
    """Background daemon thread that emits heartbeat events for the web UI."""

    # Bounded so a wedged main loop cannot pin the worker thread or let ticks
    # overlap; must stay BELOW the pulse interval (60s) for that to hold.
    _TICK_TIMEOUT_S = 30

    def __init__(self, interval_seconds: int = 60) -> None:
        self._interval = interval_seconds
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # The runtime's main event loop, captured in start(). See _tick: job
        # health can only be persisted from a RUNNING loop that outlives the
        # call, which a per-tick throwaway loop is not. None when start() runs
        # outside a loop (tests) — _tick then falls back to the old behaviour.
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        """Start the heartbeat background thread."""
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="dashboard-heartbeat",
        )
        self._thread.start()
        logger.info("Dashboard heartbeat started (interval=%ds)", self._interval)

    def stop(self) -> None:
        """Signal the thread to stop and wait for it."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)



    def _submit(self, coro_factory) -> bool:
        """Run a coroutine on the captured main loop. False if none is live.

        Cancels the submission whenever the wait does not complete normally. A
        timed-out ``Future.result()`` does NOT cancel the underlying task, so
        without this a stalled loop would leave each tick pending while the next
        one is submitted -- accumulating tasks that all fire at once on recovery,
        emitting stale pulses and a burst of successes.
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            return False
        future = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
        try:
            future.result(timeout=self._TICK_TIMEOUT_S)
        except BaseException:
            future.cancel()
            raise
        return True

    def _record_failure(self, rt: object, exc: BaseException) -> None:
        """Persist a failed tick, from the live loop when there is one.

        ``record_job_failure`` persists through the SAME loop-dependent path as
        ``record_job_success``: called from this worker thread it updates memory
        and never the table. Recording success on the loop but failure off it
        would leave exactly half the defect in place -- the failure direction,
        which is the half an operator most needs to see.
        """

        async def _record() -> None:
            rt.record_job_failure(
                "dashboard_heartbeat", "heartbeat emission failed", exc=exc
            )

        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                # Submitted WITHOUT _submit's bounded wait, deliberately. A tick is
                # periodic and a stale one is actively misleading, so it is cancelled
                # on timeout; a failure record is a one-off that stays true however
                # late it lands. Bounding it would drop the record in precisely the
                # scenario it describes — a loop wedged past the timeout is exactly
                # when the wait would expire — leaving the persisted row showing the
                # previous SUCCESS across the whole stall. Pending records are bounded
                # by one per interval and settle when the loop recovers.
                asyncio.run_coroutine_threadsafe(_record(), loop)
                return
            except RuntimeError:
                # The loop stopped between the check and the submit; fall through.
                pass
        rt.record_job_failure("dashboard_heartbeat", "heartbeat emission failed", exc=exc)

    def _tick(self, rt: object, Subsystem: object, Severity: object) -> None:
        """Emit one pulse and record job health, on the runtime's main loop.

        Blocks on the scheduled coroutine so a failure still raises into _run's
        handler (which records the job FAILURE) rather than being swallowed by a
        discarded future.
        """

        async def _emit_and_record() -> None:
            await rt.event_bus.emit(
                Subsystem.DASHBOARD,
                Severity.DEBUG,
                "heartbeat",
                "Dashboard web UI alive",
            )
            # Runs INSIDE the live main loop, so persist_job_health can schedule
            # its DB write onto a loop that keeps running afterwards.
            rt.record_job_success("dashboard_heartbeat")

        if self._submit(_emit_and_record):
            return

        # No live main loop (start() ran outside one). Emit on a throwaway loop;
        # job health stays in-memory only, exactly as before this fix.
        fallback = asyncio.new_event_loop()
        try:
            fallback.run_until_complete(
                rt.event_bus.emit(
                    Subsystem.DASHBOARD,
                    Severity.DEBUG,
                    "heartbeat",
                    "Dashboard web UI alive",
                )
            )
        finally:
            fallback.close()
        rt.record_job_success("dashboard_heartbeat")

    def _run(self) -> None:
        from genesis.observability.types import Severity, Subsystem

        while not self._stop_event.is_set():
            try:
                from genesis.runtime import GenesisRuntime

                rt = GenesisRuntime.instance()
                if rt.is_bootstrapped and rt.event_bus:
                    self._tick(rt, Subsystem, Severity)
            except Exception as exc:
                logger.error("Dashboard heartbeat failed", exc_info=True)
                try:
                    from genesis.runtime import GenesisRuntime

                    rt = GenesisRuntime.instance()
                    self._record_failure(rt, exc)
                except Exception:
                    pass
            self._stop_event.wait(self._interval)
