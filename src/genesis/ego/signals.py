"""Ego signal system — structured events for the unified cognitive loop.

Signals represent things the ego might want to pay attention to.
The SignalQueue provides priority ordering, dedup, and expiry —
generalizing the reactive dedup pattern from cadence.py.

Part of PR 1: Signal System + Focus Selector (unified cognitive loop).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

# Priority ordering for asyncio.PriorityQueue (lower = higher priority).
PRIORITY_ORDER: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


@dataclass(order=True)
class EgoSignal:
    """A structured event that the ego might want to pay attention to.

    Uses ``@dataclass(order=True)`` with ``_priority_order`` as the
    only compared field so that ``asyncio.PriorityQueue`` drains
    highest-priority signals first.
    """

    # PriorityQueue ordering — lower number = higher priority.
    _priority_order: int = field(compare=True, repr=False, default=2)

    # --- Actual fields (not compared for ordering) ---
    id: str = field(
        default_factory=lambda: uuid.uuid4().hex[:12],
        compare=False,
    )
    signal_type: str = field(default="timer", compare=False)
    focus_category: str = field(default="proactive", compare=False)
    summary: str = field(default="", compare=False)
    priority: str = field(default="medium", compare=False)
    focus_id: str | None = field(default=None, compare=False)
    metadata: dict = field(default_factory=dict, compare=False)
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
        compare=False,
    )
    expires_at: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        """Set _priority_order from the human-readable priority string."""
        self._priority_order = PRIORITY_ORDER.get(self.priority, 2)

    @property
    def is_expired(self) -> bool:
        """Check if the signal has passed its expiry time."""
        if self.expires_at is None:
            return False
        try:
            return datetime.now(UTC) > datetime.fromisoformat(self.expires_at)
        except (ValueError, TypeError):
            return False


class SignalQueue:
    """Priority queue for ego signals with dedup and expiry.

    Generalizes the reactive dedup pattern from
    ``EgoCadenceManager`` (cadence.py:101-105, 209-223).

    Parameters
    ----------
    maxsize:
        Maximum number of signals in the queue. Overflow drops
        on push (lowest priority first via replacement is not
        implemented — we simply reject when full, matching the
        existing ``QueueFull`` pattern from cadence.py:225-229).
    dedup_hours:
        Window for summary-based dedup. Same summary within this
        window is rejected. Default 6h matches cadence.py.
    """

    def __init__(
        self,
        *,
        maxsize: int = 20,
        dedup_hours: int = 6,
    ) -> None:
        self._queue: asyncio.PriorityQueue[EgoSignal] = (
            asyncio.PriorityQueue(maxsize=maxsize)
        )
        self._maxsize = maxsize
        self._dedup_hours = dedup_hours
        # summary → last_seen timestamp (same pattern as cadence.py:104)
        self._seen: dict[str, datetime] = {}

    def push(self, signal: EgoSignal) -> bool:
        """Add a signal to the queue.

        Returns True if the signal was accepted, False if it was
        rejected (dedup hit, expired, or queue full).
        """
        # Reject expired signals
        if signal.is_expired:
            logger.debug("Signal rejected (expired): %s", signal.summary[:50])
            return False

        # Content dedup: skip if same summary seen recently.
        # Include focus_category to prevent collision on empty/short summaries.
        summary_key = f"{signal.focus_category}:{signal.summary[:100]}"
        now = datetime.now(UTC)

        if summary_key in self._seen:
            age = (now - self._seen[summary_key]).total_seconds()
            if age < self._dedup_hours * 3600:
                logger.debug(
                    "Signal deduped (%.0fm old): %s",
                    age / 60,
                    summary_key[:50],
                )
                return False

        # Prune old entries (keep dict bounded)
        cutoff = now - timedelta(hours=self._dedup_hours)
        self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        self._seen[summary_key] = now

        # Try to enqueue
        try:
            self._queue.put_nowait(signal)
            logger.debug(
                "Signal queued [%s]: %s",
                signal.priority,
                summary_key[:50],
            )
            return True
        except asyncio.QueueFull:
            logger.warning("Signal queue full — dropping: %s", summary_key[:50])
            return False

    def drain(self) -> list[EgoSignal]:
        """Drain all signals from the queue, dropping expired ones.

        Returns signals sorted by priority (highest first — they
        come out in priority order from the PriorityQueue).
        """
        signals: list[EgoSignal] = []
        while not self._queue.empty():
            try:
                sig = self._queue.get_nowait()
                if not sig.is_expired:
                    signals.append(sig)
                else:
                    logger.debug(
                        "Signal dropped (expired on drain): %s",
                        sig.summary[:50],
                    )
            except asyncio.QueueEmpty:
                break
        return signals

    def __len__(self) -> int:
        """Number of signals currently in the queue."""
        return self._queue.qsize()

    def empty(self) -> bool:
        """Whether the queue is empty."""
        return self._queue.empty()

    def clear(self) -> None:
        """Drop all signals and reset dedup state."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._seen.clear()
