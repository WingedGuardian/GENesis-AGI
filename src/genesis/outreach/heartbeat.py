"""Outreach heartbeat — a dedicated, channel-independent liveness pulse.

Outreach's heartbeat used to be *emergent*: it only fired as a side-effect of an
outreach job succeeding, and the outreach scheduler only ``.start()``s once a
messaging channel (Telegram) registers. So a Telegram-less (dashboard-only) install
never emitted an outreach pulse, and adding outreach to the cessation-alert set naively
would fire a permanent unresolvable alert on exactly those installs — the false-alarm
trap that kept outreach OUT of the alert set.

This daemon breaks that coupling: it runs independently of channel registration and
pulses ONLY while the outreach scheduler is actually running (``is_running``). Paired
with the ``_subsystem_enabled('outreach')`` enable-gate (Telegram configured?), that
gives honest coverage:
  - Telegram-less install → not enabled → benign (no alert), and this daemon emits no
    pulse anyway (scheduler never started).
  - Telegram install, scheduler running → steady pulse → ``alive``.
  - Telegram install, scheduler stopped/never-restarted → pulse ceases → the shared
    ``compute_heartbeat_staleness`` goes ``overdue`` → ``subsystem_stale:outreach``.

Boundary (documented, out of scope): a WEDGED-but-alive event loop keeps
``is_running`` True and this thread pulsing — detecting a wedged (vs stopped) scheduler
is job_health's domain, the same bar the dashboard/ego heartbeats hold.
"""

from __future__ import annotations

import asyncio
import logging
import threading

logger = logging.getLogger("genesis.outreach.heartbeat")


class OutreachHeartbeat:
    """Background daemon thread that emits heartbeat events for outreach."""

    def __init__(self, interval_seconds: int = 60) -> None:
        self._interval = interval_seconds
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the heartbeat background thread."""
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="outreach-heartbeat",
        )
        self._thread.start()
        logger.info("Outreach heartbeat started (interval=%ds)", self._interval)

    def stop(self) -> None:
        """Signal the thread to stop and wait for it."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    @staticmethod
    def _should_emit(rt: object) -> bool:
        """Emit a pulse only when outreach is genuinely running.

        Gated on the scheduler's ``is_running`` (accurate across the start/stop
        lifecycle: False before start and after ``stop()`` nulls it) so a
        never-started / cleanly-stopped scheduler goes stale rather than reading a
        false ``alive`` — while a Telegram-less install (scheduler never started)
        simply never pulses.
        """
        scheduler = getattr(rt, "_outreach_scheduler", None)
        return bool(
            getattr(rt, "is_bootstrapped", False)
            and getattr(rt, "event_bus", None)
            and scheduler is not None
            and getattr(scheduler, "is_running", False)
        )

    def _run(self) -> None:
        from genesis.observability.types import Severity, Subsystem

        while not self._stop_event.is_set():
            try:
                from genesis.runtime import GenesisRuntime

                rt = GenesisRuntime.instance()
                if self._should_emit(rt):
                    loop = asyncio.new_event_loop()
                    try:
                        loop.run_until_complete(
                            rt.event_bus.emit(
                                Subsystem.OUTREACH,
                                Severity.DEBUG,
                                "heartbeat",
                                "Outreach scheduler alive",
                            )
                        )
                    finally:
                        loop.close()
                    rt.record_job_success("outreach_heartbeat")
            except Exception as exc:
                logger.error("Outreach heartbeat failed", exc_info=True)
                try:
                    from genesis.runtime import GenesisRuntime

                    rt = GenesisRuntime.instance()
                    rt.record_job_failure(
                        "outreach_heartbeat", "heartbeat emission failed", exc=exc
                    )
                except Exception:
                    pass
            self._stop_event.wait(self._interval)
