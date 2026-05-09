"""Tests for DB-fault-tolerant awareness tick (A1).

Verifies that when DB operations fail, the tick degrades gracefully:
- Signals still collected
- db_available=False in result
- No exception propagated
"""

from unittest.mock import patch

from genesis.awareness.loop import perform_tick
from genesis.awareness.signals import ConversationCollector
from genesis.awareness.types import TickResult


async def test_degraded_tick_on_db_failure(db):
    """When DB ops raise, tick returns degraded result with db_available=False."""
    collectors = [ConversationCollector()]

    # Make compute_scores raise (simulating DB lock)
    with patch(
        "genesis.awareness.loop.compute_scores",
        side_effect=Exception("database is locked"),
    ):
        result = await perform_tick(db, collectors, source="scheduled")

    assert isinstance(result, TickResult)
    assert result.db_available is False
    assert result.classified_depth is None
    assert result.scores == []
    assert len(result.signals) > 0  # signals still collected


async def test_normal_tick_has_db_available_true(db):
    """Normal tick sets db_available=True."""
    collectors = [ConversationCollector()]
    result = await perform_tick(db, collectors, source="scheduled")

    assert result.db_available is True


async def test_degraded_tick_clears_escalation(db):
    """Degraded tick should not carry escalation state."""
    collectors = [ConversationCollector()]

    with patch(
        "genesis.awareness.loop.compute_scores",
        side_effect=Exception("database is locked"),
    ):
        result = await perform_tick(db, collectors, source="scheduled")

    assert result.escalation_source is None
    assert result.escalation_pending_id is None
