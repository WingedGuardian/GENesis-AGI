"""Polling stall watchdog for Telegram getUpdates.

Detects when polling goes silent (no updates processed by PTB) for longer
than STALL_THRESHOLD_S and triggers a restart callback.

Activity is recorded by calling record_activity() — this should be hooked
into PTB's update processing (not just user message handlers) so idle
periods don't trigger false positives.

Consecutive stalls use exponential backoff on the threshold to avoid
spam-restarting when the chat is simply idle (no users talking).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from genesis.util.tasks import tracked_task

logger = logging.getLogger(__name__)

STALL_THRESHOLD_S = 300.0  # 5 min — low-traffic bot; 90s was triggering on normal idle
CHECK_INTERVAL_S = 60.0
_MAX_STALL_THRESHOLD_S = 900.0  # 15 minutes — cap for backoff


class PollingWatchdog:
    """Monitors polling liveness and triggers restart on stall."""

    def __init__(
        self,
        on_stall=None,
        stall_threshold_s: float = STALL_THRESHOLD_S,
        check_interval_s: float = CHECK_INTERVAL_S,
    ) -> None:
        self._on_stall = on_stall
        self._base_threshold = stall_threshold_s
        self._stall_threshold = stall_threshold_s
        self._check_interval = check_interval_s
        self._last_activity_at = time.monotonic()
        self._task: asyncio.Task | None = None
        self._running = False
        self._handling_stall = False
        self._consecutive_stalls = 0

    @property
    def is_running(self) -> bool:
        """Whether the watchdog is actively monitoring polling."""
        return self._running

    def record_activity(self) -> None:
        """Call this whenever a polling update is processed.

        Should be wired to PTB's update processing, not just user handlers,
        so idle periods (no user messages) don't trigger false alarms.

        Resets the consecutive-stall backoff since real activity proves
        polling is working.
        """
        self._last_activity_at = time.monotonic()
        if self._consecutive_stalls > 0:
            self._consecutive_stalls = 0
            self._stall_threshold = self._base_threshold

    def start(self) -> None:
        """Start the watchdog background task."""
        if self._running:
            return
        self._running = True
        self._last_activity_at = time.monotonic()
        self._task = tracked_task(self._run(), name="polling-watchdog")

    async def stop(self) -> None:
        """Stop the watchdog."""
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while self._running:
            await asyncio.sleep(self._check_interval)
            elapsed = time.monotonic() - self._last_activity_at
            if elapsed > self._stall_threshold and not self._handling_stall:
                self._consecutive_stalls += 1
                logger.warning(
                    "Polling stall detected (%.0fs since last activity, "
                    "threshold=%.0fs, consecutive=%d)",
                    elapsed, self._stall_threshold, self._consecutive_stalls,
                )
                if self._on_stall:
                    self._handling_stall = True
                    try:
                        await self._on_stall()
                    except Exception:
                        logger.exception("Stall handler failed")
                    finally:
                        self._handling_stall = False
                # Exponential backoff on threshold for consecutive stalls —
                # prevents spam-restarting when no users are talking.
                self._stall_threshold = min(
                    self._base_threshold * (2 ** self._consecutive_stalls),
                    _MAX_STALL_THRESHOLD_S,
                )
                logger.info(
                    "Next stall threshold: %.0fs", self._stall_threshold,
                )
                # Reset activity timer so we wait the full new threshold
                self._last_activity_at = time.monotonic()
