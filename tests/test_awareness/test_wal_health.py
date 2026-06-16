"""Tests for the WAL-size health alert (_check_wal_health).

A pinned checkpoint (e.g. a long-lived connection holding a read snapshot from
an unclosed/cancelled cursor) makes the SQLite WAL file grow unbounded. This
monitor turns that 2-day-silent failure into a high/critical observation that
surfaces to the user within minutes.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from genesis.awareness import loop


def _make_wal(tmp_path, size_bytes: int):
    db_path = tmp_path / "genesis.db"
    (tmp_path / "genesis.db-wal").write_bytes(b"\0" * size_bytes)
    return db_path


@pytest.fixture(autouse=True)
def _reset_cooldown():
    loop._last_wal_alert_at = None
    yield
    loop._last_wal_alert_at = None


@pytest.fixture
def _tiny_thresholds(monkeypatch):
    monkeypatch.setattr(loop, "_WAL_SIZE_WARN_BYTES", 100)
    monkeypatch.setattr(loop, "_WAL_SIZE_CRIT_BYTES", 500)


@pytest.mark.asyncio
async def test_no_alert_below_threshold(tmp_path, monkeypatch, _tiny_thresholds):
    db_path = _make_wal(tmp_path, 50)
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: str(db_path))
    spy = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", spy)

    await loop._check_wal_health(object())

    spy.assert_not_called()


@pytest.mark.asyncio
async def test_critical_alert_on_large_wal(tmp_path, monkeypatch, _tiny_thresholds):
    db_path = _make_wal(tmp_path, 600)
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: str(db_path))
    spy = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", spy)

    await loop._check_wal_health(object())

    spy.assert_called_once()
    assert spy.call_args.kwargs["priority"] == "critical"
    assert spy.call_args.kwargs["type"] == "infrastructure_alert"
    assert spy.call_args.kwargs["source"] == "wal_health_monitor"


@pytest.mark.asyncio
async def test_high_alert_on_moderate_wal(tmp_path, monkeypatch, _tiny_thresholds):
    db_path = _make_wal(tmp_path, 300)
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: str(db_path))
    spy = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", spy)

    await loop._check_wal_health(object())

    spy.assert_called_once()
    assert spy.call_args.kwargs["priority"] == "high"


@pytest.mark.asyncio
async def test_cooldown_one_alert_per_window(tmp_path, monkeypatch, _tiny_thresholds):
    db_path = _make_wal(tmp_path, 600)
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: str(db_path))
    spy = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", spy)

    await loop._check_wal_health(object())
    await loop._check_wal_health(object())

    spy.assert_called_once()  # second call suppressed by cooldown


@pytest.mark.asyncio
async def test_no_alert_when_wal_absent(tmp_path, monkeypatch):
    db_path = tmp_path / "genesis.db"  # no -wal file created
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: str(db_path))
    spy = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", spy)

    await loop._check_wal_health(object())

    spy.assert_not_called()


@pytest.mark.asyncio
async def test_swallows_create_errors(tmp_path, monkeypatch, _tiny_thresholds):
    db_path = _make_wal(tmp_path, 600)
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: str(db_path))
    monkeypatch.setattr(
        loop.observations, "create", AsyncMock(side_effect=RuntimeError("db down"))
    )

    # Must not raise into the tick.
    await loop._check_wal_health(object())


def test_infrastructure_alert_type_is_registered():
    """The alert type must be in _TTL_BY_TYPE so creating one doesn't emit a
    spurious 'unknown observation type' warning (also covers the dead-letter
    monitor, which uses the same type)."""
    from genesis.db.crud import observations

    assert "infrastructure_alert" in observations._TTL_BY_TYPE


@pytest.mark.asyncio
async def test_first_alert_fires_on_fresh_boot_small_monotonic(
    tmp_path, monkeypatch, _tiny_thresholds
):
    """Regression: time.monotonic() is since-boot, so on a freshly-booted host it
    is small. With a 0.0 sentinel, `now - 0.0 < cooldown` wrongly suppressed the
    FIRST alert (it passed only on a long-uptime host). The None sentinel must fire
    the first alert regardless of the monotonic value."""
    db_path = _make_wal(tmp_path, 600)
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: str(db_path))
    monkeypatch.setattr(loop.time, "monotonic", lambda: 5.0)  # fresh boot, ~5s uptime
    spy = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", spy)

    await loop._check_wal_health(object())

    spy.assert_called_once()  # must NOT be suppressed by the cooldown on first run
