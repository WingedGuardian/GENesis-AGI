"""Tests for PositionSizer."""

from __future__ import annotations

import pytest

from genesis.modules.prediction_markets.sizer import PositionSizer


class TestPositionSizerBasic:
    def test_no_edge_no_bet(self):
        sizer = PositionSizer(bankroll=1000)
        result = sizer.size(0.50, 0.50)
        assert result.recommended_size == 0.0

    def test_small_edge_below_threshold(self):
        sizer = PositionSizer(bankroll=1000, min_edge=0.05)
        result = sizer.size(0.53, 0.50)  # 3% edge < 5% threshold
        assert result.recommended_size == 0.0

    def test_positive_edge_recommends_bet(self):
        sizer = PositionSizer(bankroll=1000, kelly_fraction=0.25)
        result = sizer.size(0.70, 0.50)  # 20% edge
        assert result.recommended_size > 0
        assert result.edge > 0
        assert "YES" in result.rationale

    def test_negative_edge_recommends_no_direction(self):
        sizer = PositionSizer(bankroll=1000, kelly_fraction=0.25)
        result = sizer.size(0.30, 0.50)  # -20% edge → bet NO
        assert result.recommended_size > 0
        assert result.edge < 0
        assert "NO" in result.rationale

    def test_max_position_cap(self):
        sizer = PositionSizer(bankroll=1000, kelly_fraction=1.0, max_position_pct=0.10)
        result = sizer.size(0.95, 0.50)  # Huge edge
        assert result.bankroll_percentage <= 0.10
        assert result.recommended_size <= 100.0

    def test_confidence_scales_down(self):
        sizer = PositionSizer(bankroll=1000, kelly_fraction=0.25)
        full = sizer.size(0.70, 0.50, confidence=1.0)
        half = sizer.size(0.70, 0.50, confidence=0.5)
        assert half.recommended_size < full.recommended_size

    def test_quarter_kelly_is_default(self):
        sizer = PositionSizer()
        result = sizer.size(0.70, 0.50)
        assert result.applied_fraction < result.kelly_fraction


class TestPositionSizerValidation:
    def test_invalid_kelly_fraction(self):
        with pytest.raises(ValueError):
            PositionSizer(kelly_fraction=0)

    def test_invalid_max_position(self):
        with pytest.raises(ValueError):
            PositionSizer(max_position_pct=0)

    def test_bankroll_setter_rejects_negative(self):
        sizer = PositionSizer()
        with pytest.raises(ValueError):
            sizer.bankroll = -100


class TestPositionSizerPortfolio:
    def test_portfolio_sizing(self):
        sizer = PositionSizer(bankroll=1000, kelly_fraction=0.25)
        positions = [
            {"estimated_prob": 0.70, "market_price": 0.50},
            {"estimated_prob": 0.80, "market_price": 0.60},
        ]
        results = sizer.size_portfolio(positions)
        assert len(results) == 2
        assert all(r.recommended_size > 0 for r in results)

    def test_portfolio_scales_when_exceeding_limit(self):
        sizer = PositionSizer(bankroll=1000, kelly_fraction=1.0, max_position_pct=0.10)
        # Many high-edge positions should get scaled down
        positions = [
            {"estimated_prob": 0.90, "market_price": 0.50},
            {"estimated_prob": 0.85, "market_price": 0.50},
            {"estimated_prob": 0.80, "market_price": 0.50},
            {"estimated_prob": 0.75, "market_price": 0.50},
            {"estimated_prob": 0.70, "market_price": 0.50},
            {"estimated_prob": 0.65, "market_price": 0.50},
        ]
        results = sizer.size_portfolio(positions)
        total_pct = sum(r.bankroll_percentage for r in results)
        assert total_pct <= 0.50 + 0.001  # Max 50% total
