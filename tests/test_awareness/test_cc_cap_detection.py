"""Tests for _check_cc_cap_detection — the silent-CC-cap alert path.

The detector counts recent `cc_cap_empty_event` observations (written by the
invoker's empty-output callback) and raises ONE critical infrastructure_alert
when a run of them lands in the window — the signal that an Anthropic-subscription
cap is making output-expecting cognitive invocations return empty. We assert the
threshold, the alert fields, the cooldown, and the db-None / query-failure /
create-failure no-ops (never breaks the tick).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from genesis.awareness import loop


@pytest.fixture(autouse=True)
def _mocks(monkeypatch):
    monkeypatch.setattr(loop, "_last_cap_alert_at", None)
    create = AsyncMock()          # returns a truthy handle by default (not a dup)
    resolve = AsyncMock(return_value=0)
    monkeypatch.setattr(loop.observations, "create", create)
    monkeypatch.setattr(loop.observations, "resolve_by_source_and_type", resolve)
    return create, resolve


class _FakeCursor:
    def __init__(self, count: int):
        self._count = count

    async def fetchone(self):
        return (self._count,)


class _FakeDB:
    """Minimal async db whose COUNT(*) query returns a controllable value."""

    def __init__(self, count: int, *, raise_on_execute: bool = False):
        self._count = count
        self._raise = raise_on_execute

    async def execute(self, *args, **kwargs):
        if self._raise:
            raise RuntimeError("db error")
        return _FakeCursor(self._count)

    async def commit(self):
        pass


@pytest.mark.asyncio
async def test_run_of_empties_creates_critical_alert(_mocks):
    create, _ = _mocks
    await loop._check_cc_cap_detection(_FakeDB(3))
    create.assert_awaited_once()
    kwargs = create.await_args.kwargs
    assert kwargs["type"] == "infrastructure_alert"
    assert kwargs["priority"] == "critical"
    assert kwargs["source"] == "cc_cap_monitor"
    assert kwargs["skip_if_duplicate"] is True  # DB-backed dedup, one row per cap
    assert kwargs.get("content_hash")
    assert "capped" in kwargs["content"].lower()


@pytest.mark.asyncio
async def test_below_threshold_resolves_not_alerts(_mocks):
    create, resolve = _mocks
    await loop._check_cc_cap_detection(_FakeDB(loop._CAP_EMPTY_THRESHOLD - 1))
    create.assert_not_awaited()
    resolve.assert_awaited_once()  # recovery path clears any outstanding alert


@pytest.mark.asyncio
async def test_recovery_resets_cooldown(_mocks):
    create, resolve = _mocks
    resolve.return_value = 1  # an outstanding alert was actually resolved
    loop._last_cap_alert_at = 123.0
    await loop._check_cc_cap_detection(_FakeDB(0))
    assert loop._last_cap_alert_at is None  # reset so a fresh cap re-alerts now


@pytest.mark.asyncio
async def test_cooldown_suppresses_second_alert(_mocks):
    create, _ = _mocks
    db = _FakeDB(5)
    await loop._check_cc_cap_detection(db)
    await loop._check_cc_cap_detection(db)
    create.assert_awaited_once()  # second within the 1h cooldown → still one


@pytest.mark.asyncio
async def test_duplicate_alert_returns_none_no_log(_mocks):
    create, _ = _mocks
    create.return_value = None  # skip_if_duplicate → an unresolved alert exists
    await loop._check_cc_cap_detection(_FakeDB(4))
    create.assert_awaited_once()  # attempted, but the row already existed (no dup)


@pytest.mark.asyncio
async def test_db_none_does_not_write_or_consume_cooldown(_mocks):
    create, _ = _mocks
    await loop._check_cc_cap_detection(None)
    create.assert_not_awaited()
    assert loop._last_cap_alert_at is None  # cooldown not consumed
    await loop._check_cc_cap_detection(_FakeDB(3))
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_query_failure_never_raises_and_no_alert(_mocks):
    create, _ = _mocks
    await loop._check_cc_cap_detection(_FakeDB(3, raise_on_execute=True))
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_failure_never_raises(_mocks):
    create, _ = _mocks
    create.side_effect = RuntimeError("db locked")
    # must not propagate into the tick
    await loop._check_cc_cap_detection(_FakeDB(3))
