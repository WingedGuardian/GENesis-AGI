"""Tests for the queues snapshot — dead-letter alert lifecycle.

Regression guard for the stale "DLQ N pending" crisis: the accumulation alert
observation was never cleared when the queue drained, so a long-since-empty
queue kept surfacing a critical observation in the morning report.
"""

from __future__ import annotations

import importlib
from unittest.mock import AsyncMock

import pytest

# The snapshots package re-exports the `queues` function, shadowing the
# submodule attribute — import the module object explicitly for its globals.
queues_mod = importlib.import_module("genesis.observability.snapshots.queues")


@pytest.fixture(autouse=True)
def _reset_alert_state():
    """The accumulation cooldown is module-global; isolate it per test."""
    queues_mod._last_dead_letter_alert_at = 0.0
    yield
    queues_mod._last_dead_letter_alert_at = 0.0


def _mock_dead_letter(count: int) -> AsyncMock:
    dl = AsyncMock()
    dl.get_pending_count = AsyncMock(return_value=count)
    return dl


async def _open_alerts(db) -> list[dict]:
    from genesis.db.crud import observations as obs

    return await obs.query(
        db, source="dead_letter_monitor", type="infrastructure_alert", resolved=False,
    )


async def test_dead_letter_alert_created_on_accumulation(db):
    await queues_mod.queues(db, None, _mock_dead_letter(60))
    rows = await _open_alerts(db)
    assert len(rows) == 1
    assert "60 pending" in rows[0]["content"]
    assert rows[0]["priority"] == "critical"


async def test_dead_letter_alert_resolved_on_drain(db):
    # Accumulate → alert created
    await queues_mod.queues(db, None, _mock_dead_letter(60))
    assert len(await _open_alerts(db)) == 1

    # Drain below threshold → the stale alert is auto-resolved
    await queues_mod.queues(db, None, _mock_dead_letter(0))
    assert await _open_alerts(db) == []

    # And it no longer surfaces in the morning report's unsurfaced feed
    from genesis.db.crud import observations as obs

    unsurfaced = await obs.get_unsurfaced(db)
    assert not any(o["source"] == "dead_letter_monitor" for o in unsurfaced)

    # Cooldown was reset, so a genuine re-accumulation re-alerts
    await queues_mod.queues(db, None, _mock_dead_letter(75))
    rows = await _open_alerts(db)
    assert len(rows) == 1
    assert "75 pending" in rows[0]["content"]


async def test_no_resolve_when_never_alerted(db):
    # Below threshold from the start, no prior alert → no-op, no observation
    await queues_mod.queues(db, None, _mock_dead_letter(0))
    assert await _open_alerts(db) == []
