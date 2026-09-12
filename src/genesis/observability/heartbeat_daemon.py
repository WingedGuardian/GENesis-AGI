"""Shared mechanism for the threaded subsystem heartbeat daemons.

Subclasses supply IDENTITY — which subsystem, which job name, which message,
which thread name — and, where they need one, an emit GATE. The mechanism lives
here exactly once: capture the runtime's main event loop, run each tick ON it,
cancel a tick that outlives its budget, and record BOTH success and failure from
that loop.

WHY THIS MODULE EXISTS, since a new module needs a reason. It did not exist, and
the cost was measured: the outreach daemon was copied from the dashboard one and
inherited, verbatim, a silent job-health persistence bug (both recorded health
AFTER closing the throwaway loop they emitted on, so ``persist_job_health``
returned early and the row was never written — neither subsystem appeared among
89 persisted job rows while both pulsed normally). Three successive review rounds
then had to apply the same fix twice, and each round's fix created the next
round's defect. Duplication was the generator, so removing it is the fix rather
than a tidy-up.

WHY THE TICK RUNS ON THE RUNTIME'S LOOP. Recording job health needs a RUNNING
loop that OUTLIVES the call: ``persist_job_health`` schedules the DB write as a
task and returns early when no loop is running, so a per-tick throwaway loop
loses the write either way — it is closed before the scheduled write can run.

WHY SUCCESS AND FAILURE ARE BOUNDED DIFFERENTLY. A tick is PERIODIC and a stale
pulse is actively misleading, so an overdue tick is cancelled. A failure record
is a ONE-OFF that stays true however late it lands, and the condition it reports
— a wedged loop — is exactly what makes a bounded wait expire; bounding it would
drop the record across the whole stall, leaving the persisted row showing the
previous SUCCESS. Do not "make these consistent": the asymmetry is the point, and
collapsing it is the defect a review round already caught here once.
"""

from __future__ import annotations

import asyncio
import logging
import threading

logger = logging.getLogger("genesis.observability.heartbeat_daemon")


class HeartbeatDaemon:
    """Background daemon thread emitting a subsystem's liveness pulse."""

    # Bounded so a wedged main loop cannot pin the worker thread or let ticks
    # overlap; must stay BELOW the pulse interval for that to hold.
    _TICK_TIMEOUT_S = 30

    # --- subclass identity -------------------------------------------------
    #: Attribute name on ``observability.types.Subsystem`` (resolved lazily so
    #: this module stays import-cheap for a foundational path).
    subsystem_name: str = ""
    #: job_health key recorded for each tick.
    job_name: str = ""
    #: Human-facing event message.
    message: str = ""
    #: OS thread name, for ps/py-spy.
    thread_name: str = ""

    def __init__(self, interval_seconds: int = 60) -> None:
        self._interval = interval_seconds
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # The runtime's main event loop, captured in start(). None when start()
        # runs outside a loop (tests, sync embedders) — _tick then falls back to
        # a throwaway loop and pulses without recording health, which is the
        # pre-existing behaviour rather than a new failure.
        self._loop: asyncio.AbstractEventLoop | None = None
        # The in-flight failure record, if any. See _record_failure: at most one.
        self._pending_failure = None

    # --- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Start the heartbeat thread, capturing the loop it was started from.

        MUST be called from the runtime's loop thread. That is the fix's entire
        activation condition, and it used to be silent: an embedder calling this
        from a sync context gets ``_loop = None``, every tick takes the fallback,
        and job health is never persisted — the exact defect this class exists to
        remove. So the miss is now LOUD rather than inferred from missing rows.
        """
        if self._thread is not None and self._thread.is_alive():
            logger.warning(
                "%s heartbeat already running — ignoring duplicate start()",
                self.job_name,
            )
            return
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
            logger.warning(
                "%s heartbeat started with NO running event loop: it will pulse, but "
                "job health will not be persisted. Start it from the runtime's loop "
                "thread to restore persistence.",
                self.job_name,
            )
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=self.thread_name or "heartbeat",
        )
        self._thread.start()
        logger.info(
            "%s heartbeat started (interval=%ds)", self.job_name, self._interval
        )

    def stop(self) -> None:
        """Signal the thread to stop and wait briefly for it."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    # --- overridable gate --------------------------------------------------
    def _should_emit(self, rt: object) -> bool:
        """Whether to pulse this cycle. Default: the runtime is usable."""
        return bool(getattr(rt, "is_bootstrapped", False) and getattr(rt, "event_bus", None))

    # --- mechanism ---------------------------------------------------------
    def _submit(self, coro_factory) -> bool:
        """Run a coroutine on the captured main loop. False if none is live.

        Cancels the submission whenever the wait does not complete normally. A
        timed-out ``Future.result()`` does NOT cancel the underlying task, so
        without this a stalled loop would leave each tick pending while the next
        is submitted — accumulating tasks that all fire at once on recovery,
        emitting stale pulses and a burst of successes.
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            return False
        coro = coro_factory()
        try:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            coro.close()  # never scheduled -> close it rather than leak a warning
            raise
        try:
            future.result(timeout=self._TICK_TIMEOUT_S)
        except BaseException:
            future.cancel()
            raise
        return True

    def _tick(self, rt: object, Subsystem: object, Severity: object) -> None:
        """Emit one pulse and record success, on the runtime's main loop.

        Blocks on the scheduled coroutine so a failure still raises into _run's
        handler (which records the job FAILURE) instead of being swallowed by a
        discarded future.
        """
        subsystem = getattr(Subsystem, self.subsystem_name)

        async def _emit_and_record() -> None:
            await rt.event_bus.emit(subsystem, Severity.DEBUG, "heartbeat", self.message)
            # Runs INSIDE the live main loop, so persist_job_health can schedule
            # its DB write onto a loop that keeps running afterwards.
            rt.record_job_success(self.job_name)

        if self._submit(_emit_and_record):
            return

        # No live main loop. Emit on a throwaway loop; job health stays in-memory
        # only, exactly as before this mechanism existed.
        fallback = asyncio.new_event_loop()
        try:
            fallback.run_until_complete(
                rt.event_bus.emit(subsystem, Severity.DEBUG, "heartbeat", self.message)
            )
        finally:
            fallback.close()
        rt.record_job_success(self.job_name)

    def _record_failure(self, rt: object, exc: BaseException) -> None:
        """Persist a failed tick, from the live loop when there is one.

        Submitted WITHOUT _submit's bounded wait — see the module docstring for
        why the two directions are bounded differently.
        """

        async def _record() -> None:
            rt.record_job_failure(self.job_name, "heartbeat emission failed", exc=exc)

        loop = self._loop
        if loop is not None and loop.is_running():
            # COALESCE: at most ONE failure record may be in flight. Unbounded
            # submission was the previous shape and it is wrong for the case it was
            # written for — during a wedge every tick times out and queues another
            # record, so a three-hour stall lands ~120 of them at once on recovery,
            # driving consecutive_failures to ~120, permanently inflating
            # total_failures, and firing a retry per record where a retry registry
            # is wired. "The daemon is failing" is ONE fact however long the stall
            # lasts; the pending record already carries it.
            pending = self._pending_failure
            if pending is not None and not pending.done():
                return
            coro = _record()
            try:
                self._pending_failure = asyncio.run_coroutine_threadsafe(coro, loop)
            except RuntimeError:
                # The loop stopped between the check and the submit. Close the
                # coroutine explicitly: it was created as an argument and never
                # scheduled, so without this it warns "never awaited" every tick
                # through the shutdown window.
                coro.close()
            else:
                # A concurrent.futures.Future does not warn on an unretrieved
                # exception the way an asyncio task does, so an error inside the
                # record would vanish silently.
                self._pending_failure.add_done_callback(self._log_record_error)
                return
        rt.record_job_failure(self.job_name, "heartbeat emission failed", exc=exc)

    def _log_record_error(self, future) -> None:
        """Surface an exception raised by a submitted failure record."""
        try:
            future.result()
        except Exception:
            logger.error("%s heartbeat failure-record failed", self.job_name, exc_info=True)

    def _run(self) -> None:
        from genesis.observability.types import Severity, Subsystem

        while not self._stop_event.is_set():
            try:
                from genesis.runtime import GenesisRuntime

                rt = GenesisRuntime.instance()
                if self._should_emit(rt):
                    self._tick(rt, Subsystem, Severity)
            except Exception as exc:
                logger.error("%s heartbeat failed", self.job_name, exc_info=True)
                try:
                    from genesis.runtime import GenesisRuntime

                    rt = GenesisRuntime.instance()
                    self._record_failure(rt, exc)
                except Exception:
                    pass
            self._stop_event.wait(self._interval)
