"""Tests for OutcomeTracker."""

from __future__ import annotations

import pytest

from genesis.modules.prediction_markets.tracker import OutcomeTracker
from genesis.modules.prediction_markets.types import BetRecord, BetStatus


def _bet(genesis_est: float = 0.7, market_price: float = 0.5, **kw) -> BetRecord:
    defaults = {
        "market_id": "m1",
        "market_title": "Test market",
        "genesis_estimate": genesis_est,
        "market_price_at_entry": market_price,
        "category": "politics",
    }
    defaults.update(kw)
    return BetRecord(**defaults)


class TestOutcomeTrackerRecord:
    def test_record_and_retrieve(self):
        tracker = OutcomeTracker()
        bet = _bet()
        tracker.record_bet(bet)
        assert tracker.total_records == 1
        assert tracker.get_bet(bet.id) is bet

    def test_unknown_bet_returns_none(self):
        tracker = OutcomeTracker()
        assert tracker.get_bet("nonexistent") is None


class TestOutcomeTrackerResolve:
    def test_resolve_computes_brier_score(self):
        tracker = OutcomeTracker()
        bet = _bet(genesis_est=0.7)
        tracker.record_bet(bet)
        resolved = tracker.resolve_bet(bet.id, 1.0)
        assert resolved is not None
        assert resolved.brier_score == pytest.approx((0.7 - 1.0) ** 2)
        assert resolved.actual_outcome == 1.0

    def test_resolve_placed_bet_win(self):
        tracker = OutcomeTracker()
        bet = _bet(
            genesis_est=0.7,
            market_price=0.5,
            status=BetStatus.PLACED,
            position_size=100.0,
            direction="yes",
        )
        tracker.record_bet(bet)
        resolved = tracker.resolve_bet(bet.id, 1.0)
        assert resolved.status == BetStatus.WON
        assert resolved.pnl > 0

    def test_resolve_placed_bet_loss(self):
        tracker = OutcomeTracker()
        bet = _bet(
            genesis_est=0.7,
            status=BetStatus.PLACED,
            position_size=100.0,
            direction="yes",
        )
        tracker.record_bet(bet)
        resolved = tracker.resolve_bet(bet.id, 0.0)
        assert resolved.status == BetStatus.LOST
        assert resolved.pnl == -100.0

    def test_resolve_no_direction_bet(self):
        tracker = OutcomeTracker()
        bet = _bet(
            genesis_est=0.3,
            market_price=0.5,
            status=BetStatus.PLACED,
            position_size=100.0,
            direction="no",
        )
        tracker.record_bet(bet)
        resolved = tracker.resolve_bet(bet.id, 0.0)
        assert resolved.status == BetStatus.WON
        assert resolved.pnl > 0

    def test_resolve_unknown_bet(self):
        tracker = OutcomeTracker()
        assert tracker.resolve_bet("nope", 1.0) is None


class TestOutcomeTrackerStats:
    def _make_resolved_tracker(self):
        tracker = OutcomeTracker()
        # Win
        b1 = _bet(genesis_est=0.8, market_price=0.5, status=BetStatus.PLACED,
                   position_size=100.0, direction="yes", category="politics")
        tracker.record_bet(b1)
        tracker.resolve_bet(b1.id, 1.0)
        # Loss
        b2 = _bet(genesis_est=0.7, market_price=0.5, status=BetStatus.PLACED,
                   position_size=50.0, direction="yes", category="crypto")
        tracker.record_bet(b2)
        tracker.resolve_bet(b2.id, 0.0)
        return tracker

    def test_brier_score_overall(self):
        tracker = self._make_resolved_tracker()
        brier = tracker.brier_score()
        assert brier is not None
        assert 0 <= brier <= 1

    def test_brier_score_by_category(self):
        tracker = self._make_resolved_tracker()
        brier = tracker.brier_score(category="politics")
        assert brier is not None

    def test_win_rate(self):
        tracker = self._make_resolved_tracker()
        assert tracker.win_rate() == 0.5

    def test_total_pnl(self):
        tracker = self._make_resolved_tracker()
        pnl = tracker.total_pnl()
        assert isinstance(pnl, float)

    def test_roi(self):
        tracker = self._make_resolved_tracker()
        roi = tracker.roi()
        assert roi is not None

    def test_category_summary(self):
        tracker = self._make_resolved_tracker()
        summary = tracker.category_summary()
        assert "politics" in summary
        assert "crypto" in summary
        assert summary["politics"]["count"] == 1

    def test_stats_summary(self):
        tracker = self._make_resolved_tracker()
        stats = tracker.stats()
        assert "brier_score" in stats
        assert "win_rate" in stats
        assert "total_pnl" in stats

    def test_empty_tracker_stats(self):
        tracker = OutcomeTracker()
        assert tracker.brier_score() is None
        assert tracker.win_rate() is None
        assert tracker.roi() is None
        assert tracker.total_pnl() == 0.0


class TestCalibrationData:
    def test_calibration_bins(self):
        tracker = OutcomeTracker()
        # Create bets across probability range
        for i in range(10):
            prob = 0.1 * i + 0.1
            bet = _bet(genesis_est=prob, status=BetStatus.CONSIDERED)
            tracker.record_bet(bet)
            tracker.resolve_bet(bet.id, 1.0 if prob > 0.5 else 0.0)

        data = tracker.calibration_data(n_bins=5)
        assert len(data) > 0
        for d in data:
            assert "bin_center" in d
            assert "predicted_avg" in d
            assert "actual_frequency" in d
            assert "count" in d

    def test_empty_calibration(self):
        tracker = OutcomeTracker()
        assert tracker.calibration_data() == []
