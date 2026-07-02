"""Tests for _check_cc_slot_memory — the per-slot RSS Telegram/alert path (PR-2c).

The alert rides the existing critical-observations job: a
type="infrastructure_alert", priority="critical" observation → Telegram. We
assert the observation is created with the right fields, the WARN/CRIT priority
split, the per-slot cooldown, and the db-None / below-threshold no-ops.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from genesis.awareness import loop


@pytest.fixture(autouse=True)
def _reset_cooldown_and_mock_create(monkeypatch):
    monkeypatch.setattr(loop, "_last_slot_alert_at", {})
    create = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", create)
    return create


def _slot(label, rss_mb, pid=1234):
    return {"slot": label, "pid": pid, "rss_mb": rss_mb, "status": "x"}


@pytest.mark.asyncio
async def test_crit_creates_critical_infrastructure_alert(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    db = object()  # any non-None handle
    await loop._check_cc_slot_memory(db, slots=[_slot("4", 6500)])
    create.assert_awaited_once()
    kwargs = create.await_args.kwargs
    assert kwargs["type"] == "infrastructure_alert"
    assert kwargs["priority"] == "critical"
    assert kwargs["source"] == "cc_slot_monitor"
    assert "cc-4" in kwargs["content"]


@pytest.mark.asyncio
async def test_warn_is_high_priority_not_critical(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    await loop._check_cc_slot_memory(object(), slots=[_slot("2", 4500)])
    create.assert_awaited_once()
    assert create.await_args.kwargs["priority"] == "high"


@pytest.mark.asyncio
async def test_below_warn_no_alert(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    await loop._check_cc_slot_memory(object(), slots=[_slot("1", 950)])
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_cooldown_suppresses_second_alert(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    db = object()
    await loop._check_cc_slot_memory(db, slots=[_slot("4", 6500)])
    await loop._check_cc_slot_memory(db, slots=[_slot("4", 6600)])
    # second call is within the 1h cooldown → still only one create
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_distinct_slots_alert_independently(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    await loop._check_cc_slot_memory(object(), slots=[_slot("4", 6500), _slot("5", 6700)])
    assert create.await_count == 2


@pytest.mark.asyncio
async def test_db_none_does_not_write_or_consume_cooldown(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    # db None (e.g. DB down) → no write, and the cooldown is NOT consumed so a
    # later tick with a live db still alerts.
    await loop._check_cc_slot_memory(None, slots=[_slot("4", 6500)])
    create.assert_not_awaited()
    assert loop._last_slot_alert_at == {}
    await loop._check_cc_slot_memory(object(), slots=[_slot("4", 6500)])
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_failure_never_raises(_reset_cooldown_and_mock_create):
    create = _reset_cooldown_and_mock_create
    create.side_effect = RuntimeError("db locked")
    # must not propagate into the tick
    await loop._check_cc_slot_memory(object(), slots=[_slot("4", 6500)])
