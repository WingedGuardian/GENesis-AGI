"""Off-loop event-loop stall diagnostics — capture the loop thread's stack while wedged.

The on-loop lag sampler (``hosting/standalone.py::_loop_lag_sampler``) can only
MEASURE a stall after it clears — it reads drift once its own ``asyncio.sleep``
returns, so by the time it logs, the synchronous frame that blocked the loop is
already gone. This sampler runs OFF the loop, in a daemon thread, and reads the
loop-health heartbeat the lag sampler publishes each ~0.5s: when that heartbeat
goes stale (the on-loop sampler stopped publishing = the loop is stuck right
now), it snapshots the loop thread's stack via :func:`sys._current_frames` and
logs it — catching the offending synchronous frame mid-stall. Diagnostic-only;
it never affects control flow.

Why a custom thread instead of ``faulthandler.dump_traceback_later``: this
targets the loop thread specifically (not every thread dumped to stderr), routes
to the logger, reuses the already-published ``loop_health`` sample (no second
timer to re-arm), and attaches executor pressure — at the cost of ~40 lines. One
dump per stall EPISODE (reset when the loop recovers) keeps a multi-second stall
from flooding the journal.

Depends on the loop-lag sampler (the heartbeat publisher); it no-ops while
``loop_health.read()`` is ``None``.
"""

from __future__ import annotations

import logging
import math
import sys
import threading
import traceback
from collections.abc import Callable

from genesis.util import loop_health

logger = logging.getLogger(__name__)

_DEFAULT_STALL_MS = 1000.0
_POLL_INTERVAL_S = 0.25


class LoopStallSampler:
    """Detects a wedged event loop (stale loop-health heartbeat) and dumps its stack.

    The injectable seams (``frames_source`` / ``health_read`` / ``age_fn`` /
    ``log``) make :meth:`poll_once` unit-testable without real threads or sleeps.
    """

    def __init__(
        self,
        *,
        loop_thread_id: int,
        stall_ms: float = _DEFAULT_STALL_MS,
        frames_source: Callable[[], dict] = sys._current_frames,
        health_read: Callable[[], object | None] = loop_health.read,
        age_fn: Callable[..., float] = loop_health.age_s,
        log: logging.Logger = logger,
    ) -> None:
        self._loop_thread_id = loop_thread_id
        # Reject NaN/inf/zero/negative: inf never dumps, and NaN/non-positive read
        # a healthy heartbeat as wedged and never reset the episode flag. These
        # parse as valid floats, so a ValueError guard at the env parser misses
        # them — clamp at the boundary instead.
        if not (math.isfinite(stall_ms) and stall_ms > 0):
            stall_ms = _DEFAULT_STALL_MS
        self._stall_ms = stall_ms
        self._frames_source = frames_source
        self._health_read = health_read
        self._age_fn = age_fn
        self._log = log
        self._dumped_this_episode = False

    def poll_once(self) -> bool:
        """One poll step. Returns ``True`` iff it dumped a stack this call."""
        sample = self._health_read()
        if sample is None:
            # Publisher hasn't run yet / disabled — nothing to judge.
            return False
        age_ms = self._age_fn(sample) * 1000.0
        if age_ms < self._stall_ms:
            # Loop is scheduling normally — reset so the next episode dumps once.
            self._dumped_this_episode = False
            return False
        if self._dumped_this_episode:
            # Already dumped for this stall episode — stay quiet until it clears.
            return False
        self._dumped_this_episode = True
        self._dump(sample, age_ms)
        return True

    def _dump(self, sample: object, age_ms: float) -> None:
        try:
            frame = self._frames_source().get(self._loop_thread_id)
            if frame is None:
                stack = "<loop thread frame unavailable (loop moved on)>"
            else:
                stack = "".join(traceback.format_stack(frame))
            self._log.warning(
                "event-loop WEDGED %.0fms (no loop-health publish) — executor=%s\n"
                "loop-thread stack (the synchronous frame starving the loop):\n%s",
                age_ms,
                getattr(sample, "executor", None),
                stack,
            )
        except Exception:
            # Diagnostic-only: a partial/racing frame snapshot must never raise
            # into the sampler thread.
            self._log.warning("loop-stall stack dump failed", exc_info=True)


def run_loop_stall_sampler(
    *,
    loop_thread_id: int,
    stop_event: threading.Event,
    stall_ms: float = _DEFAULT_STALL_MS,
    poll_interval_s: float = _POLL_INTERVAL_S,
) -> None:
    """Poll for a wedged loop until ``stop_event`` is set (runs in a daemon thread).

    ``stop_event.wait`` doubles as the inter-poll sleep and the shutdown signal,
    so the thread exits promptly on shutdown instead of only at interpreter
    teardown.
    """
    sampler = LoopStallSampler(loop_thread_id=loop_thread_id, stall_ms=stall_ms)
    while not stop_event.wait(poll_interval_s):
        try:
            sampler.poll_once()
        except Exception:
            logger.debug("loop-stall sampler poll failed", exc_info=True)
