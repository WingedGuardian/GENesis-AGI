"""Tests for _check_cc_slot_memory — the per-slot RSS Telegram/alert path (PR-2c).

The alert rides the existing critical-observations job: a
type="infrastructure_alert", priority="critical" observation → Telegram. We
assert the observation is created with the right fields, the WARN/CRIT priority
split, the per-slot cooldown, and the db-None / below-threshold no-ops.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

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
async def test_null_slot_session_alerts_with_pid_key_and_label(_reset_cooldown_and_mock_create):
    # An unregistered/manual interactive session has slot=None; it must still
    # alert, keyed and labeled by pid (not collapsed onto a shared "None" bucket).
    create = _reset_cooldown_and_mock_create
    await loop._check_cc_slot_memory(object(), slots=[_slot(None, 6500, pid=98765)])
    create.assert_awaited_once()
    assert "pid 98765" in create.await_args.kwargs["content"]
    assert "pid:98765" in loop._last_slot_alert_at  # cooldown keyed by pid, not "None"


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
    # distinct sessions have distinct pids; cooldown is keyed by pid
    await loop._check_cc_slot_memory(
        object(), slots=[_slot("4", 6500, pid=111), _slot("5", 6700, pid=222)]
    )
    assert create.await_count == 2


@pytest.mark.asyncio
async def test_expired_cooldown_keys_are_evicted(_reset_cooldown_and_mock_create):
    # pid-keyed cooldown must stay bounded: an entry older than the cooldown window
    # is evicted on the next tick (behaviour-neutral hygiene, not a leak).
    import time as _time

    now = _time.monotonic()
    loop._last_slot_alert_at["pid:999"] = now - loop._SLOT_ALERT_COOLDOWN_S - 100  # stale
    loop._last_slot_alert_at["pid:111"] = now  # fresh, still cooling
    # a below-threshold tick still runs the eviction pass at the top of the check
    await loop._check_cc_slot_memory(object(), slots=[_slot("1", 100, pid=1)])
    assert "pid:999" not in loop._last_slot_alert_at  # stale evicted
    assert "pid:111" in loop._last_slot_alert_at  # fresh retained


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


# ── MW-0 A2: the /proc enumeration must run OFF the event loop ───────────────


@pytest.mark.asyncio
async def test_slots_none_offloads_enumeration(monkeypatch):
    """When slots are not injected, enumerate_cc_slots (~1s of sync /proc
    syscalls) must be dispatched through asyncio.to_thread, never on the loop."""
    import genesis.observability.cc_slots as cc_slots_mod

    sentinel = MagicMock(return_value=[])
    monkeypatch.setattr(cc_slots_mod, "enumerate_cc_slots", sentinel)

    dispatched = []
    real_to_thread = asyncio.to_thread

    async def spy(fn, *a, **k):
        dispatched.append(fn)
        return await real_to_thread(fn, *a, **k)

    monkeypatch.setattr(loop.asyncio, "to_thread", spy)

    await loop._check_cc_slot_memory(object(), slots=None)

    assert sentinel in dispatched  # enumeration went through to_thread
    sentinel.assert_called_once()


@pytest.mark.asyncio
async def test_injected_slots_skip_enumeration(monkeypatch):
    """The slots= injection path must NOT enumerate at all (tests / future
    snapshot-sharing pass pre-collected slots)."""
    import genesis.observability.cc_slots as cc_slots_mod

    enum = MagicMock(side_effect=AssertionError("must not enumerate with injected slots"))
    monkeypatch.setattr(cc_slots_mod, "enumerate_cc_slots", enum)

    await loop._check_cc_slot_memory(object(), slots=[_slot("1", 100)])
    enum.assert_not_called()
