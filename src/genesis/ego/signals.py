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

# Priority ordering (lower = higher priority). Also drives drain order.
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
    only compared field so sorting drains highest-priority signals first.
    """

    # Ordering — lower number = higher priority.
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
    """Priority queue for ego signals with dedup, expiry, and eviction.

    Internally a plain list — async blocking is provided by the
    ``_notify`` event (as it always was), so no asyncio queue is needed.
    All mutators run on the event loop with no await points, so no lock
    is required.

    Admission on a full queue: expired signals are pruned first; if still
    full, a strictly lower-priority signal is evicted to admit a
    higher-priority newcomer (oldest victim within the lowest priority
    class). A newcomer that outranks nothing is rejected.

    Parameters
    ----------
    maxsize:
        Maximum number of signals in the queue.
    dedup_hours:
        Default window for summary-based dedup. Same summary within the
        window is rejected. Escalation/reactive categories use shorter
        windows (``_DEDUP_HOURS_BY_CATEGORY``) — a re-firing CRITICAL is
        a fresh fact, not spam.
    """

    # Per-category dedup windows (hours). Categories absent here use the
    # constructor's dedup_hours. Escalations re-admit after 1h: suppressing
    # a recurring CRITICAL for 6h leaves the ego blind to a live incident.
    _DEDUP_HOURS_BY_CATEGORY: dict[str, float] = {
        "escalation": 1,
        "reactive": 2,
    }

    def __init__(
        self,
        *,
        maxsize: int = 20,
        dedup_hours: int = 6,
    ) -> None:
        self._items: list[EgoSignal] = []
        self._maxsize = maxsize
        self._dedup_hours = dedup_hours
        # summary → last_seen timestamp (same pattern as cadence.py:104)
        self._seen: dict[str, datetime] = {}
        # Blocking notification for consumer loop (set on push, cleared on drain)
        self._notify = asyncio.Event()

    def _dedup_window_hours(self, focus_category: str) -> float:
        return self._DEDUP_HOURS_BY_CATEGORY.get(
            focus_category,
            self._dedup_hours,
        )

    @staticmethod
    def _dedup_key(signal: EgoSignal) -> str:
        # Include focus_category to prevent collision on empty/short summaries.
        return f"{signal.focus_category}:{signal.summary[:100]}"

    def push(self, signal: EgoSignal) -> bool:
        """Add a signal to the queue.

        Returns True if the signal was accepted, False if it was
        rejected (dedup hit, expired, or queue full with nothing
        evictable).
        """
        # Reject expired signals
        if signal.is_expired:
            logger.debug("Signal rejected (expired): %s", signal.summary[:50])
            return False

        summary_key = self._dedup_key(signal)
        now = datetime.now(UTC)

        if summary_key in self._seen:
            age = (now - self._seen[summary_key]).total_seconds()
            if age < self._dedup_window_hours(signal.focus_category) * 3600:
                logger.debug(
                    "Signal deduped (%.0fm old): %s",
                    age / 60,
                    summary_key[:50],
                )
                return False

        # Prune old entries (keep dict bounded). Use the max window so a
        # still-active default-window stamp is never pruned early.
        max_window = max(
            [float(self._dedup_hours), *self._DEDUP_HOURS_BY_CATEGORY.values()],
        )
        cutoff = now - timedelta(hours=max_window)
        self._seen = {k: v for k, v in self._seen.items() if v > cutoff}

        if not self._admit(signal):
            return False

        # Stamp dedup ONLY after successful admission — a rejected push
        # must not suppress its own retry for the rest of the window.
        self._seen[summary_key] = now
        self._notify.set()
        logger.debug(
            "Signal queued [%s]: %s",
            signal.priority,
            summary_key[:50],
        )
        return True

    def requeue(self, signals: list[EgoSignal]) -> int:
        """Re-admit previously drained signals (e.g. after a gated cycle).

        Bypasses dedup — these signals were already admitted once and
        their dedup stamps still stand. Expired signals are dropped.
        Returns the number of signals re-admitted.
        """
        admitted = 0
        for signal in signals:
            if signal.is_expired:
                logger.debug(
                    "Requeue dropped (expired): %s",
                    signal.summary[:50],
                )
                continue
            if self._admit(signal):
                admitted += 1
            else:
                logger.warning(
                    "Requeue dropped (queue full): %s",
                    signal.summary[:50],
                )
        if admitted:
            self._notify.set()
        return admitted

    def _admit(self, signal: EgoSignal) -> bool:
        """Insert *signal*, pruning expired items and evicting a strictly
        lower-priority victim if the queue is full. Returns False when
        the queue is full of equal-or-higher-priority signals.
        """
        if len(self._items) >= self._maxsize:
            self._items = [s for s in self._items if not s.is_expired]

        if len(self._items) >= self._maxsize:
            lowest = max(s._priority_order for s in self._items)
            if signal._priority_order < lowest:
                # Victim: oldest signal in the lowest priority class —
                # staleness loses.
                victim = min(
                    (s for s in self._items if s._priority_order == lowest),
                    key=lambda s: s.created_at,
                )
                # Remove by identity — EgoSignal.__eq__ compares only
                # _priority_order (every other field is compare=False), so
                # list.remove() would delete the FIRST same-priority signal
                # rather than this specific oldest victim.
                self._items = [s for s in self._items if s is not victim]
                # Drop the victim's dedup stamp: it never reached a cycle, so
                # its content must be free to re-enter within the window —
                # otherwise eviction reintroduces the silent-loss case.
                self._seen.pop(self._dedup_key(victim), None)
                logger.warning(
                    "Signal queue full — evicted [%s] %r to admit [%s] %r",
                    victim.priority,
                    victim.summary[:50],
                    signal.priority,
                    signal.summary[:50],
                )
            else:
                logger.warning(
                    "Signal queue full — dropping: %s",
                    self._dedup_key(signal)[:50],
                )
                return False

        self._items.append(signal)
        return True

    def drain(self) -> list[EgoSignal]:
        """Drain all signals from the queue, dropping expired ones.

        Returns signals sorted by priority (highest first), FIFO within
        the same priority.

        Clears the notify event BEFORE draining so that a push()
        during drain re-sets it — the next wait() returns immediately.
        """
        self._notify.clear()
        items, self._items = self._items, []
        signals: list[EgoSignal] = []
        for sig in sorted(items, key=lambda s: (s._priority_order, s.created_at)):
            if not sig.is_expired:
                signals.append(sig)
            else:
                logger.debug(
                    "Signal dropped (expired on drain): %s",
                    sig.summary[:50],
                )
        return signals

    def __len__(self) -> int:
        """Number of signals currently in the queue."""
        return len(self._items)

    def empty(self) -> bool:
        """Whether the queue is empty."""
        return not self._items

    async def wait(self) -> None:
        """Block until at least one signal is pushed."""
        await self._notify.wait()

    def clear(self) -> None:
        """Drop all signals and reset dedup state."""
        self._notify.clear()
        self._items.clear()
        self._seen.clear()
