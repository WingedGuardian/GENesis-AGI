"""Tests for the ego signal system — EgoSignal and SignalQueue.

Covers: priority ordering, dedup, expiry, queue overflow, drain, clear.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
