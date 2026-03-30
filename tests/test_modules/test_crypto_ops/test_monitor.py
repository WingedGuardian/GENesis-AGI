"""Tests for PositionMonitor."""

from genesis.modules.crypto_ops.monitor import PositionMonitor
from genesis.modules.crypto_ops.types import TokenPosition


def _pos(**kw) -> TokenPosition:
    defaults = {
        "token_name": "TestCoin",
        "entry_price": 1.0,
        "current_price": 1.0,
        "invested": 100.0,
        "volume_24h": 10000,
        "liquidity": 5000,
        "narrative_momentum": 0.6,
    }
    defaults.update(kw)
    return TokenPosition(**defaults)


class TestPositionMonitorBasic:
    def test_add_and_retrieve(self):
        monitor = PositionMonitor()
        pos = _pos()
        monitor.add_position(pos)
        assert monitor.position_count == 1
        assert monitor.get_position(pos.id) is pos

    def test_active_positions(self):
        monitor = PositionMonitor()
        p1 = _pos()
        p2 = _pos(exited_at="2026-01-01T00:00:00Z")
        monitor.add_position(p1)
        monitor.add_position(p2)
        assert len(monitor.active_positions) == 1


class TestPositionMonitorUpdate:
    def test_update_price_recalculates_pnl(self):
        monitor = PositionMonitor()
        pos = _pos(entry_price=1.0, invested=100.0)
        monitor.add_position(pos)
        monitor.update_position(pos.id, current_price=2.0)
        assert pos.current_value == 200.0
        assert pos.pnl == 100.0
        assert pos.pnl_pct == 1.0

    def test_update_records_history(self):
        monitor = PositionMonitor()
        pos = _pos()
        monitor.add_position(pos)
        monitor.update_position(pos.id, current_price=1.5)
        assert len(monitor._history[pos.id]) == 1

    def test_update_unknown_returns_none(self):
        monitor = PositionMonitor()
        assert monitor.update_position("nope", current_price=1.0) is None


class TestExitSignals:
    def test_volume_drop_signal(self):
        monitor = PositionMonitor(volume_drop_threshold=0.7)
        pos = _pos(volume_24h=10000)
        monitor.add_position(pos)
        # First update to create history
        monitor.update_position(pos.id, volume_24h=10000)
        # Volume drops 80%
        monitor.update_position(pos.id, volume_24h=2000)
        signals = monitor.check_exit_signals(pos.id)
        reasons = [s.reason for s in signals]
        assert "volume_drop" in reasons

    def test_narrative_fading_signal(self):
        monitor = PositionMonitor(momentum_exit_threshold=0.2)
        pos = _pos(narrative_momentum=0.1)
        monitor.add_position(pos)
        signals = monitor.check_exit_signals(pos.id)
        reasons = [s.reason for s in signals]
        assert "narrative_fading" in reasons

    def test_heavy_loss_signal(self):
        monitor = PositionMonitor()
        pos = _pos(pnl_pct=-0.6)
        monitor.add_position(pos)
        signals = monitor.check_exit_signals(pos.id)
        reasons = [s.reason for s in signals]
        assert "heavy_loss" in reasons

    def test_no_signals_for_healthy_position(self):
        monitor = PositionMonitor()
        pos = _pos(narrative_momentum=0.8, pnl_pct=0.5)
        monitor.add_position(pos)
        signals = monitor.check_exit_signals(pos.id)
        assert signals == []


class TestMarkExited:
    def test_mark_exited_calculates_final_pnl(self):
        monitor = PositionMonitor()
        pos = _pos(entry_price=1.0, invested=100.0)
        monitor.add_position(pos)
        result = monitor.mark_exited(pos.id, exit_price=1.5)
        assert result.exit_price == 1.5
        assert result.pnl == 50.0
        assert result.exited_at

    def test_mark_exited_unknown(self):
        monitor = PositionMonitor()
        assert monitor.mark_exited("nope", 1.0) is None
