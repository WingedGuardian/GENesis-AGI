"""Tests for DeadLetterQueue.get_stuck_count — the DLQ cry-wolf fix.

The critical DLQ-accumulation alert must count only GENUINELY-STUCK items: a
short-TTL self-healing type (e.g. chain_exhausted:judge, 1h) counts only once it
is OLDER than its TTL; a type with no short TTL counts immediately (no regression).
A fresh short-TTL burst -> not counted (no cry-wolf); aged-past-TTL or a genuine
accumulation -> counted. The raw get_pending_count stays the full total.
"""

from datetime import UTC, datetime

import pytest

from genesis.routing.dead_letter import DeadLetterQueue

_NOW = datetime(2026, 3, 4, 12, 0, 0, tzinfo=UTC)


def _at(hours_ago: float):
    from datetime import timedelta
    t = _NOW - timedelta(hours=hours_ago)
    return lambda: t


@pytest.mark.asyncio
async def test_fresh_judge_burst_is_not_stuck(db):
    """The exact incident: a fresh chain_exhausted:judge burst must not count."""
    fresh = DeadLetterQueue(db, clock=lambda: _NOW)
    for i in range(5):
        await fresh.enqueue("chain_exhausted:judge", "{}", "all", f"exhausted {i}")
    now_q = DeadLetterQueue(db, clock=lambda: _NOW)
    assert await now_q.get_pending_count() == 5   # raw total unchanged
    assert await now_q.get_stuck_count() == 0     # within the 1h self-heal window


@pytest.mark.asyncio
async def test_aged_short_ttl_is_stuck(db):
    """A judge item older than its 1h TTL is genuinely stuck (drainer failing)."""
    old = DeadLetterQueue(db, clock=_at(2))  # 2h ago > 1h judge TTL
    await old.enqueue("chain_exhausted:judge", "{}", "all", "exhausted")
    assert await DeadLetterQueue(db, clock=lambda: _NOW).get_stuck_count() == 1


@pytest.mark.asyncio
async def test_non_short_ttl_counts_immediately(db):
    """A type with no short-TTL entry counts immediately (unchanged behavior)."""
    fresh = DeadLetterQueue(db, clock=lambda: _NOW)
    await fresh.enqueue("llm_call", "{}", "anthropic", "err")
    assert await DeadLetterQueue(db, clock=lambda: _NOW).get_stuck_count() == 1


@pytest.mark.asyncio
async def test_chain_exhausted_six_hour_window(db):
    """chain_exhausted:<other> (6h TTL): not stuck at 2h, stuck at 7h."""
    two_h = DeadLetterQueue(db, clock=_at(2))
    await two_h.enqueue("chain_exhausted:other", "{}", "all", "err")
    assert await DeadLetterQueue(db, clock=lambda: _NOW).get_stuck_count() == 0

    seven_h = DeadLetterQueue(db, clock=_at(7))
    await seven_h.enqueue("chain_exhausted:other", "{}", "all", "err")
    assert await DeadLetterQueue(db, clock=lambda: _NOW).get_stuck_count() == 1


@pytest.mark.asyncio
async def test_mixed_population(db):
    """Full mix: 3 stuck (aged judge, aged chain, fresh llm_call) of 5 pending."""
    await DeadLetterQueue(db, clock=_at(2)).enqueue("chain_exhausted:judge", "{}", "all", "e")   # stuck
    await DeadLetterQueue(db, clock=lambda: _NOW).enqueue("chain_exhausted:judge", "{}", "all", "e")  # fresh
    await DeadLetterQueue(db, clock=_at(2)).enqueue("chain_exhausted:other", "{}", "all", "e")   # within 6h
    await DeadLetterQueue(db, clock=_at(7)).enqueue("chain_exhausted:other", "{}", "all", "e")   # stuck
    await DeadLetterQueue(db, clock=lambda: _NOW).enqueue("llm_call", "{}", "x", "e")            # stuck (no short TTL)

    q = DeadLetterQueue(db, clock=lambda: _NOW)
    assert await q.get_pending_count() == 5
    assert await q.get_stuck_count() == 3
