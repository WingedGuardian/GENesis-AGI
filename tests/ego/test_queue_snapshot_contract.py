"""Regression tests for ego queue metrics from the real health snapshot."""

from unittest.mock import AsyncMock

import pytest

from genesis.ego.context import EgoContextBuilder
from genesis.ego.genesis_context import GenesisEgoContextBuilder
from genesis.observability.health_data import HealthDataService


@pytest.mark.asyncio
async def test_both_ego_contexts_render_real_queue_snapshot_depths():
    deferred_queue = AsyncMock()
    deferred_queue.count_pending.return_value = 7
    dead_letter = AsyncMock()
    dead_letter.get_pending_count.return_value = 11
    health_data = HealthDataService(
        deferred_queue=deferred_queue,
        dead_letter=dead_letter,
    )

    snapshot = await health_data.snapshot()
    assert snapshot["queues"]["deferred_work"] == 7
    assert snapshot["queues"]["dead_letters"] == 11

    user_context = await EgoContextBuilder(
        db=None,
        health_data=health_data,
    )._system_health_section()
    genesis_context = await GenesisEgoContextBuilder(
        db=None,
        health_data=health_data,
    )._system_health_section()

    for context in (user_context, genesis_context):
        assert "- Deferred work: 7 pending" in context
        assert "- Dead letter: 11 items" in context
@pytest.mark.asyncio
async def test_a_failed_queue_query_reads_unknown_never_zero():
    """A failed query is not an empty queue.

    The producer leaves the value None on a query failure while the KEY stays
    present, so `.get(name, 0)` does NOT default: rendering it raw put the
    literal token "None" in the ego's context, and defaulting to 0 would state
    "nothing pending" for a depth nobody measured — the same confident-wrong
    reading this file's other test exists to prevent, one layer down.
    """
    deferred_queue = AsyncMock()
    deferred_queue.count_pending.side_effect = RuntimeError("db unavailable")
    deferred_queue.count_recovery_pending.side_effect = RuntimeError("db unavailable")
    deferred_queue.count_worklist_pending.side_effect = RuntimeError("db unavailable")
    dead_letter = AsyncMock()
    dead_letter.get_pending_count.side_effect = RuntimeError("db unavailable")
    health_data = HealthDataService(
        deferred_queue=deferred_queue,
        dead_letter=dead_letter,
    )

    snapshot = await health_data.snapshot()
    # The producer's own contract: key present, value None, reason recorded.
    assert snapshot["queues"]["deferred_work"] is None
    assert snapshot["queues"]["dead_letters"] is None
    assert snapshot["queues"]["errors"]

    user_context = await EgoContextBuilder(
        db=None,
        health_data=health_data,
    )._system_health_section()
    genesis_context = await GenesisEgoContextBuilder(
        db=None,
        health_data=health_data,
    )._system_health_section()

    for context in (user_context, genesis_context):
        assert "- Deferred work: unknown pending" in context
        assert "- Dead letter: unknown items" in context
        assert "None" not in context
        assert "- Deferred work: 0 pending" not in context
