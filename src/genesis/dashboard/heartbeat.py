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

        loop = self._loop
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(_emit_and_record(), loop).result(
                timeout=self._TICK_TIMEOUT_S
            )
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
                    rt.record_job_failure("dashboard_heartbeat", "heartbeat emission failed", exc=exc)
                except Exception:
                    pass
            self._stop_event.wait(self._interval)
