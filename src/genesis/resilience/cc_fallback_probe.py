"""Safety-net probe for total-idle CC fallback recovery.

While CC is in account-wide fallback (Claude rate-limited, running on a roster
peer), recovery is normally detected by the next successful HOME-model call —
a foreground turn or any background DirectSession (see
``genesis.cc.fallback_recovery``). But a *totally* idle system makes no home
calls at all, so the dashboard banner + CLI notice could assert a stale
fallback after Claude has actually recovered.

This worker closes that gap: on a slow cadence, and ONLY while in fallback, it
makes one minimal native-Claude call. Success ⇒ Claude is back ⇒ clear the flag
(via the shared ``note_home_recovery``). This is the *only* way to detect
Anthropic-side recovery without traffic — the resilience state machine and CC
budget tracker track Genesis's self-imposed throttle, not Anthropic availability.

The cadence is deliberately slow (30 min): real turns/background sessions clear
fallback far sooner in an active system, so this is purely the idle backstop. A
probe during an outage just fails fast with a rate-limit error (cheap); it only
costs a real (minimal) Claude call on the cycle where recovery is detected.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from genesis.util.proc_kill import ProcessCleanupUncertain

logger = logging.getLogger(__name__)

# Slow backstop: foreground/background success clears fallback much faster in an
# active system; this only matters when nothing else is talking to the home model.
_PROBE_INTERVAL_S = 1800  # 30 minutes


class CCFallbackProbeWorker:
    """Periodically probes the home model while in fallback; clears on recovery."""

    def __init__(self, *, invoker, interval_s: int = _PROBE_INTERVAL_S) -> None:
        self._invoker = invoker
        self._interval_s = interval_s
        self._task: asyncio.Task | None = None
        self._cleanup_uncertain = False

    def start(self) -> None:
        """Start the probe loop (idempotent — no-op if a task is already running)."""
        if self._cleanup_uncertain:
            return  # Runtime recreation is required after an uncertain child.
        if self._task is None or self._task.done():
            from genesis.util.tasks import tracked_task

            # tracked_task (not bare create_task): only stop() ever awaits this
            # loop, so an unexpected loop death would otherwise be silent.
            self._task = tracked_task(self._loop(), name="cc-fallback-probe")
            logger.info(
                "CC fallback probe worker started (interval=%ds)",
                self._interval_s,
            )

    async def stop(self) -> None:
        """Cancel the probe loop and wait for it to unwind (idempotent)."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
            logger.info("CC fallback probe worker stopped")

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._interval_s)
                await self._probe_once()
            except asyncio.CancelledError:
                raise
            except ProcessCleanupUncertain:
                self._cleanup_uncertain = True
                logger.warning("CC recovery probes suspended: process cleanup unconfirmed")
                return
            except Exception:
                logger.warning("CC fallback probe iteration failed; provider details withheld")

    async def _probe_once(self) -> None:
        """One probe cycle: no-op unless in fallback; minimal HOME-model call; clear
        on OK. The home model is whatever was rate-limited (``state.original``) —
        which may be a roster PEER when the configured default is non-Claude, NOT
        necessarily native Claude. Probing the wrong model would falsely clear a
        peer outage (Claude is ~always up)."""
        from genesis.cc import fallback_state, roster

        state = fallback_state.read()
        if not state.is_fallback:
            return  # only probe while degraded — zero cost in the normal state

        from genesis.cc.exceptions import CCQuotaExhaustedError, CCRateLimitError
        from genesis.cc.provider_readiness import probe_provider
        from genesis.cc.types import CCInvocation

        home = state.original or roster.CLAUDE
        try:
            # {} for native Claude; the peer's endpoint+token otherwise. Pre-stamped
            # so the invoker routes to the HOME model; roster_eligible=False keeps the
            # chokepoint from re-selecting the global default over it.
            overrides = roster.overrides_for(home)
        except roster.RosterError:
            logger.warning("CC fallback probe: cannot resolve home model %r", home)
            return

        # The shared probe suppresses customizations/tools while retaining OAuth.
        inv = CCInvocation(
            prompt="",
            **overrides,
        )
        try:
            # Keep the worker's existing 300s bound and 30-minute cadence.
            await probe_provider(self._invoker, inv, timeout_s=300)
        except (CCRateLimitError, CCQuotaExhaustedError):
            logger.info("CC fallback probe: home model %r still rate-limited", home)
            return

        from genesis.cc.fallback_recovery import note_home_recovery

        # A validated HOME answer, not an arbitrary peer success, may clear the
        # shared status. All automatic callbacks were isolated during the probe.
        observer = self._invoker if home == roster.active_model() else None
        if await note_home_recovery(invoker=observer):
            logger.info("CC fallback probe: home model %r recovered — fallback cleared", home)
