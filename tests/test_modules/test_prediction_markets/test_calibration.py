"""Tests for CalibrationEngine."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from genesis.modules.prediction_markets.calibration import CalibrationEngine
from genesis.modules.prediction_markets.types import Market


class TestCalibrationEstimate:
    async def test_returns_none_without_router(self):
        engine = CalibrationEngine()
        market = Market(title="Test", current_price=0.6)
        result = await engine.estimate(market)
        assert result is None

    async def test_produces_estimate_with_router(self):
        router = AsyncMock()
        router.route.return_value = json.dumps({
            "estimated_probability": 0.72,
            "reasoning": "Base rate suggests higher probability",
            "signals_used": ["historical data", "expert opinion"],
            "bias_checks": ["checked anchoring"],
            "confidence_in_estimate": 0.8,
        })
        engine = CalibrationEngine()
        market = Market(title="Will X happen?", current_price=0.6)
        est = await engine.estimate(market, router=router)
        assert est is not None
        assert est.estimated_probability == 0.72
        assert est.market_price == 0.6
        assert abs(est.edge - 0.12) < 0.001
        assert est.confidence_in_estimate == 0.8
        assert len(est.signals_used) == 2

    async def test_clamps_extreme_probabilities(self):
        router = AsyncMock()
        router.route.return_value = json.dumps({
            "estimated_probability": 0.99,
            "reasoning": "test",
        })
        engine = CalibrationEngine()
        market = Market(title="Test", current_price=0.5)
        est = await engine.estimate(market, router=router)
        assert est.estimated_probability == 0.95  # clamped

    async def test_handles_llm_error(self):
        router = AsyncMock()
        router.route.side_effect = RuntimeError("LLM down")
        engine = CalibrationEngine()
        market = Market(title="Test", current_price=0.5)
        est = await engine.estimate(market, router=router)
        assert est is None


class TestCalibrationCompare:
    async def test_compare_with_user(self):
        router = AsyncMock()
        router.route.return_value = json.dumps({
            "estimated_probability": 0.65,
            "reasoning": "Analysis suggests...",
            "signals_used": ["data"],
            "bias_checks": [],
            "confidence_in_estimate": 0.7,
        })
        engine = CalibrationEngine()
        market = Market(title="Test", current_price=0.5)
        result = await engine.compare_with_user(market, 0.75, router=router)
        assert result["user_estimate"] == 0.75
        assert result["genesis_estimate"] == 0.65
        assert result["market_price"] == 0.5
        assert 0 <= result["agreement"] <= 1
        assert "combined_estimate" in result

    async def test_compare_returns_error_without_router(self):
        engine = CalibrationEngine()
        market = Market(title="Test", current_price=0.5)
        result = await engine.compare_with_user(market, 0.7)
        assert "error" in result
