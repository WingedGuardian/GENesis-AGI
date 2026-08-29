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

    # Bounded so a wedged main loop cannot pin the worker thread or let ticks
    # overlap; must stay BELOW the pulse interval (60s) for that to hold.
    _TICK_TIMEOUT_S = 30

    def __init__(self, interval_seconds: int = 60) -> None:
        self._interval = interval_seconds
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # The runtime's main event loop, captured in start(). Each tick runs ON
        # it rather than on a per-tick throwaway loop, because recording job
        # health needs a RUNNING loop that OUTLIVES the call: persist_job_health
        # schedules the DB write as a task and returns early when no loop is
        # running, so a throwaway loop that is closed straight after the emit
        # loses the write either way. None when start() is called outside a loop
        # (tests, sync embedders) — the worker then falls back to the previous
        # throwaway-loop behaviour.
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        """Start the heartbeat background thread."""
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            # Started outside a running loop; _tick falls back (see _tick).
            self._loop = None
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
                "outreach_heartbeat", "heartbeat emission failed", exc=exc
            )

        try:
            if self._submit(_record):
                return
        except BaseException:
            # Already handling a failure; fall through to the degraded path
            # rather than masking the original exception with this one.
            pass
        rt.record_job_failure("outreach_heartbeat", "heartbeat emission failed", exc=exc)

    def _tick(self, rt: object, Subsystem: object, Severity: object) -> None:
        """Emit one pulse and record job health, on the runtime's main loop.

        Blocks on the scheduled coroutine so a failure still raises into _run's
        handler (which records the job FAILURE) instead of being swallowed by a
        discarded future. The wait is bounded below the pulse interval so a
        wedged loop cannot stack overlapping ticks or pin this thread forever.
        """

        async def _emit_and_record() -> None:
            await rt.event_bus.emit(
                Subsystem.OUTREACH,
                Severity.DEBUG,
                "heartbeat",
                "Outreach scheduler alive",
            )
            # Runs INSIDE the live main loop, so persist_job_health can schedule
            # its DB write onto a loop that keeps running afterwards.
            rt.record_job_success("outreach_heartbeat")

        if self._submit(_emit_and_record):
            return

        # No live main loop (start() ran outside one). Emit on a throwaway loop;
        # job health stays in-memory only, exactly as before this fix.
        fallback = asyncio.new_event_loop()
        try:
            fallback.run_until_complete(
                rt.event_bus.emit(
                    Subsystem.OUTREACH,
                    Severity.DEBUG,
                    "heartbeat",
                    "Outreach scheduler alive",
                )
            )
        finally:
            fallback.close()
        rt.record_job_success("outreach_heartbeat")

    def _run(self) -> None:
        from genesis.observability.types import Severity, Subsystem

        while not self._stop_event.is_set():
            try:
                from genesis.runtime import GenesisRuntime

                rt = GenesisRuntime.instance()
                if self._should_emit(rt):
                    self._tick(rt, Subsystem, Severity)
            except Exception as exc:
                logger.error("Outreach heartbeat failed", exc_info=True)
                try:
                    from genesis.runtime import GenesisRuntime

                    rt = GenesisRuntime.instance()
                    self._record_failure(rt, exc)
                except Exception:
                    pass
            self._stop_event.wait(self._interval)
