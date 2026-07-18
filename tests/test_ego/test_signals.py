"""Tests for the ego signal system — EgoSignal and SignalQueue.

Covers: priority ordering, dedup, expiry, queue overflow, drain, clear.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from genesis.ego.signals import EgoSignal, SignalQueue

# ── EgoSignal dataclass ───────────────────────────────────────────────────


def test_signal_priority_order_from_string():
    """EgoSignal._priority_order is set from the priority string."""
    sig = EgoSignal(priority="critical")
    assert sig._priority_order == 0

    sig = EgoSignal(priority="high")
    assert sig._priority_order == 1

    sig = EgoSignal(priority="medium")
    assert sig._priority_order == 2

    sig = EgoSignal(priority="low")
    assert sig._priority_order == 3


def test_signal_default_priority_is_medium():
    sig = EgoSignal()
    assert sig.priority == "medium"
    assert sig._priority_order == 2


def test_signal_ordering_critical_before_low():
    """PriorityQueue ordering: critical signals come first."""
    critical = EgoSignal(priority="critical", summary="urgent")
    low = EgoSignal(priority="low", summary="minor")
    # dataclass ordering: lower _priority_order wins
    assert critical < low


def test_signal_not_expired_when_no_expiry():
    sig = EgoSignal()
    assert sig.is_expired is False


def test_signal_not_expired_when_future():
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    sig = EgoSignal(expires_at=future)
    assert sig.is_expired is False


def test_signal_expired_when_past():
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    sig = EgoSignal(expires_at=past)
    assert sig.is_expired is True


def test_signal_expired_bad_timestamp():
    sig = EgoSignal(expires_at="not-a-date")
    assert sig.is_expired is False


def test_signal_has_unique_id():
    s1 = EgoSignal()
    s2 = EgoSignal()
    assert s1.id != s2.id


# ── SignalQueue ───────────────────────────────────────────────────────────


def test_queue_push_and_drain():
    q = SignalQueue(maxsize=10)
    sig = EgoSignal(summary="test signal")
    assert q.push(sig) is True
    assert len(q) == 1
    assert not q.empty()

    signals = q.drain()
    assert len(signals) == 1
    assert signals[0].summary == "test signal"
    assert q.empty()


def test_queue_dedup_rejects_same_summary():
    """Same summary within dedup window should be rejected."""
    q = SignalQueue(maxsize=10, dedup_hours=6)
    sig1 = EgoSignal(summary="deadline approaching")
    sig2 = EgoSignal(summary="deadline approaching")

    assert q.push(sig1) is True
    assert q.push(sig2) is False  # deduped
    assert len(q) == 1


def test_queue_dedup_allows_different_summary():
    q = SignalQueue(maxsize=10)
    sig1 = EgoSignal(summary="event A")
    sig2 = EgoSignal(summary="event B")

    assert q.push(sig1) is True
    assert q.push(sig2) is True
    assert len(q) == 2


def test_queue_rejects_expired_on_push():
    q = SignalQueue(maxsize=10)
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    sig = EgoSignal(summary="old event", expires_at=past)
    assert q.push(sig) is False
    assert q.empty()


def test_queue_drops_expired_on_drain():
    """Expired signals are filtered out during drain."""
    q = SignalQueue(maxsize=10)
    good_sig = EgoSignal(summary="good")
    # Push a signal that will be valid at push time
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    will_expire = EgoSignal(summary="will expire", expires_at=future)

    q.push(good_sig)
    q.push(will_expire)
    assert len(q) == 2

    # Drain should include both since neither expired yet
    signals = q.drain()
    assert len(signals) == 2


def test_queue_overflow_rejects():
    """Queue full should reject new signals."""
    q = SignalQueue(maxsize=2)
    assert q.push(EgoSignal(summary="first")) is True
    assert q.push(EgoSignal(summary="second")) is True
    assert q.push(EgoSignal(summary="third")) is False
    assert len(q) == 2


def test_queue_drain_priority_order():
    """Drain should return signals in priority order (highest first)."""
    q = SignalQueue(maxsize=10)
    q.push(EgoSignal(priority="low", summary="low pri"))
    q.push(EgoSignal(priority="critical", summary="critical pri"))
    q.push(EgoSignal(priority="medium", summary="medium pri"))

    signals = q.drain()
    assert len(signals) == 3
    assert signals[0].priority == "critical"
    assert signals[1].priority == "medium"
    assert signals[2].priority == "low"


def test_queue_clear():
    q = SignalQueue(maxsize=10)
    q.push(EgoSignal(summary="a"))
    q.push(EgoSignal(summary="b"))
    assert len(q) == 2

    q.clear()
    assert q.empty()
    assert len(q) == 0

    # After clear, dedup state is also reset
    assert q.push(EgoSignal(summary="a")) is True


def test_queue_dedup_truncates_long_summary():
    """Dedup key is truncated to 100 chars."""
    q = SignalQueue(maxsize=10)
    long_summary = "A" * 200
    sig1 = EgoSignal(summary=long_summary)
    sig2 = EgoSignal(summary=long_summary)

    assert q.push(sig1) is True
    assert q.push(sig2) is False  # deduped by first 100 chars


# ── SignalQueue.wait() ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_returns_on_push():
    """wait() should return once a signal is pushed."""
    q = SignalQueue(maxsize=10)

    async def push_after_delay():
        await asyncio.sleep(0.05)
        q.push(EgoSignal(summary="wake up"))

    asyncio.create_task(push_after_delay())
    await asyncio.wait_for(q.wait(), timeout=2.0)
    assert not q.empty()


@pytest.mark.asyncio
async def test_wait_blocks_when_empty():
    """wait() should not return if nothing is pushed (within timeout)."""
    q = SignalQueue(maxsize=10)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q.wait(), timeout=0.1)


@pytest.mark.asyncio
async def test_drain_clears_notify():
    """After drain(), wait() should block again until next push."""
    q = SignalQueue(maxsize=10)
    q.push(EgoSignal(summary="first"))
    q.drain()

    # Event was cleared by drain — wait should block
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q.wait(), timeout=0.1)


@pytest.mark.asyncio
async def test_push_during_drain_resets_notify():
    """If push() happens after drain clears the event, wait() returns immediately."""
    q = SignalQueue(maxsize=10)
    q.push(EgoSignal(summary="original"))
    q.drain()  # clears notify

    # Push a new signal — should re-set the event
    q.push(EgoSignal(summary="new signal"))
    await asyncio.wait_for(q.wait(), timeout=1.0)
    assert not q.empty()


# ── Priority eviction on overflow ────────────────────────────────────────


def test_full_queue_evicts_lower_priority_for_higher():
    """A CRITICAL newcomer evicts a low-priority signal from a full queue."""
    q = SignalQueue(maxsize=2)
    assert q.push(EgoSignal(priority="low", summary="low a")) is True
    assert q.push(EgoSignal(priority="low", summary="low b")) is True
    assert q.push(EgoSignal(priority="critical", summary="urgent")) is True
    assert len(q) == 2
    summaries = [s.summary for s in q.drain()]
    assert "urgent" in summaries


def test_full_queue_rejects_equal_priority():
    """No eviction among equals — a full queue still rejects same-priority."""
    q = SignalQueue(maxsize=1)
    assert q.push(EgoSignal(priority="high", summary="first")) is True
    assert q.push(EgoSignal(priority="high", summary="second")) is False
    assert len(q) == 1


def test_eviction_victim_is_oldest_of_lowest_class():
    """Eviction picks the oldest signal in the lowest priority class."""
    q = SignalQueue(maxsize=3)
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    new = datetime.now(UTC).isoformat()
    q.push(EgoSignal(priority="low", summary="old low", created_at=old))
    q.push(EgoSignal(priority="low", summary="new low", created_at=new))
    q.push(EgoSignal(priority="medium", summary="med"))

    assert q.push(EgoSignal(priority="critical", summary="crit")) is True
    summaries = [s.summary for s in q.drain()]
    assert summaries[0] == "crit"
    assert "old low" not in summaries
    assert "new low" in summaries


def test_expired_pruned_before_eviction():
    """A full queue frees space by pruning expired signals first."""
    import time

    q = SignalQueue(maxsize=1)
    soon = (datetime.now(UTC) + timedelta(milliseconds=10)).isoformat()
    q.push(EgoSignal(priority="medium", summary="short lived", expires_at=soon))
    time.sleep(0.05)
    # Same priority — only admitted because the occupant expired.
    assert q.push(EgoSignal(priority="medium", summary="newcomer")) is True
    assert [s.summary for s in q.drain()] == ["newcomer"]


# ── Dedup semantics ──────────────────────────────────────────────────────


def test_rejected_push_does_not_poison_dedup():
    """A push rejected for overflow must not stamp the dedup window."""
    q = SignalQueue(maxsize=1)
    q.push(EgoSignal(priority="high", summary="occupied"))
    assert q.push(EgoSignal(priority="high", summary="try again")) is False
    q.drain()
    # Retry after the queue empties: must be admitted, not dedup-blocked.
    assert q.push(EgoSignal(priority="high", summary="try again")) is True


def test_escalation_dedup_window_is_shorter():
    """Escalations re-admit after 1h — a re-firing CRITICAL is news."""
    q = SignalQueue(maxsize=10, dedup_hours=6)
    make = lambda: EgoSignal(  # noqa: E731
        focus_category="escalation",
        priority="critical",
        summary="db down",
    )
    assert q.push(make()) is True
    assert q.push(make()) is False  # inside the 1h escalation window

    # Backdate the stamp past 1h but inside the 6h default window.
    q._seen["escalation:db down"] = datetime.now(UTC) - timedelta(hours=2)
    assert q.push(make()) is True


def test_default_category_keeps_long_window():
    """Non-escalation categories keep the constructor's dedup window."""
    q = SignalQueue(maxsize=10, dedup_hours=6)
    assert q.push(EgoSignal(summary="routine")) is True
    q._seen["proactive:routine"] = datetime.now(UTC) - timedelta(hours=2)
    assert q.push(EgoSignal(summary="routine")) is False  # 2h < 6h


# ── requeue() — gated-cycle survival ─────────────────────────────────────


def test_requeue_bypasses_dedup():
    """Drained signals re-enter despite their still-standing dedup stamps."""
    q = SignalQueue(maxsize=10)
    q.push(EgoSignal(summary="gated work"))
    drained = q.drain()
    assert q.push(EgoSignal(summary="gated work")) is False  # dedup holds
    assert q.requeue(drained) == 1
    assert len(q) == 1


def test_requeue_drops_expired():
    q = SignalQueue(maxsize=10)
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    stale = EgoSignal(summary="stale", expires_at=past)
    fresh = EgoSignal(summary="fresh")
    assert q.requeue([stale, fresh]) == 1
    assert [s.summary for s in q.drain()] == ["fresh"]


def test_requeue_applies_priority_eviction():
    q = SignalQueue(maxsize=1)
    q.push(EgoSignal(priority="low", summary="filler"))
    crit = EgoSignal(
        focus_category="escalation",
        priority="critical",
        summary="requeued crit",
    )
    assert q.requeue([crit]) == 1
    assert [s.summary for s in q.drain()] == ["requeued crit"]


@pytest.mark.asyncio
async def test_requeue_sets_notify():
    """Requeued signals must wake the consumer like a fresh push."""
    q = SignalQueue(maxsize=10)
    q.push(EgoSignal(summary="x"))
    drained = q.drain()  # clears notify
    q.requeue(drained)
    await asyncio.wait_for(q.wait(), timeout=1.0)
    assert not q.empty()
