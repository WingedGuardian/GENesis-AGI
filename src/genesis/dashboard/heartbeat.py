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

    def __init__(self, interval_seconds: int = 60) -> None:
        self._interval = interval_seconds
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the heartbeat background thread."""
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

    def _run(self) -> None:
        from genesis.observability.types import Severity, Subsystem

        while not self._stop_event.is_set():
            try:
                from genesis.runtime import GenesisRuntime

                rt = GenesisRuntime.instance()
                if rt.is_bootstrapped and rt.event_bus:
                    loop = asyncio.new_event_loop()
                    try:
                        loop.run_until_complete(
                            rt.event_bus.emit(
                                Subsystem.DASHBOARD,
                                Severity.DEBUG,
                                "heartbeat",
                                "Dashboard web UI alive",
                            )
                        )
                    finally:
                        loop.close()
                    rt.record_job_success("dashboard_heartbeat")
            except Exception:
                logger.error("Dashboard heartbeat failed", exc_info=True)
                try:
                    from genesis.runtime import GenesisRuntime

                    rt = GenesisRuntime.instance()
                    rt.record_job_failure("dashboard_heartbeat", "heartbeat emission failed")
                except Exception:
                    pass
            self._stop_event.wait(self._interval)
