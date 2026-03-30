"""Tests for PredictionMarketsModule."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from genesis.modules.prediction_markets.module import PredictionMarketsModule
from genesis.modules.prediction_markets.types import Market


class TestModuleProperties:
    def test_name(self):
        mod = PredictionMarketsModule()
        assert mod.name == "prediction_markets"

    def test_enabled_default(self):
        mod = PredictionMarketsModule()
        assert mod.enabled is False

    def test_research_profile_name(self):
        mod = PredictionMarketsModule()
        assert mod.get_research_profile_name() == "prediction-markets"


class TestModuleLifecycle:
    async def test_register_and_deregister(self):
        mod = PredictionMarketsModule()
        runtime = AsyncMock()
        await mod.register(runtime)
        await mod.deregister()


class TestModuleHandleOpportunity:
    async def test_returns_none_for_invalid_input(self):
        mod = PredictionMarketsModule()
        result = await mod.handle_opportunity({"not_a_market": True})
        assert result is None

    async def test_returns_proposal_with_edge(self):
        router = AsyncMock()
        router.route.return_value = json.dumps({
            "estimated_probability": 0.75,
            "reasoning": "Strong evidence",
            "signals_used": ["data"],
            "bias_checks": [],
            "confidence_in_estimate": 0.8,
        })
        from genesis.modules.prediction_markets.calibration import CalibrationEngine
        from genesis.modules.prediction_markets.sizer import PositionSizer

        mod = PredictionMarketsModule(
            calibration=CalibrationEngine(),
            sizer=PositionSizer(bankroll=1000),
        )
        market = Market(title="Test", current_price=0.5)
        result = await mod.handle_opportunity({
            "market": market,
            "router": router,
        })
        assert result is not None
        assert result["type"] == "prediction_market_bet"
        assert result["requires_approval"] is True
        assert result["estimate"]["probability"] == 0.75
        assert result["sizing"]["recommended_size"] > 0

    async def test_returns_none_when_no_edge(self):
        router = AsyncMock()
        router.route.return_value = json.dumps({
            "estimated_probability": 0.51,
            "reasoning": "Uncertain",
            "confidence_in_estimate": 0.3,
        })
        from genesis.modules.prediction_markets.calibration import CalibrationEngine
        from genesis.modules.prediction_markets.sizer import PositionSizer

        mod = PredictionMarketsModule(
            calibration=CalibrationEngine(),
            sizer=PositionSizer(bankroll=1000, min_edge=0.05),
        )
        market = Market(title="Test", current_price=0.50)
        result = await mod.handle_opportunity({
            "market": market,
            "router": router,
        })
        assert result is None


class TestModuleExtractGeneralizable:
    async def test_extracts_lesson(self):
        router = AsyncMock()
        router.route.return_value = json.dumps({
            "generalizable": True,
            "lesson": "Overconfident on novel predictions by ~10%",
            "category": "calibration",
        })
        mod = PredictionMarketsModule()
        outcome = {
            "market_title": "Test",
            "category": "politics",
            "genesis_estimate": 0.8,
            "market_price_at_entry": 0.6,
            "actual_outcome": 0.0,
            "brier_score": 0.64,
        }
        result = await mod.extract_generalizable(outcome, router=router)
        assert result is not None
        assert len(result) == 1
        assert result[0]["source"] == "module:prediction_markets"
        assert "overconfident" in result[0]["lesson"].lower()

    async def test_returns_none_without_router(self):
        mod = PredictionMarketsModule()
        result = await mod.extract_generalizable({"brier_score": 0.5})
        assert result is None

    async def test_returns_none_for_non_generalizable(self):
        router = AsyncMock()
        router.route.return_value = json.dumps({
            "generalizable": False,
            "reason": "Domain-specific",
        })
        mod = PredictionMarketsModule()
        result = await mod.extract_generalizable(
            {"brier_score": 0.3, "market_title": "Test", "category": "x",
             "genesis_estimate": 0.5, "market_price_at_entry": 0.5,
             "actual_outcome": 1.0},
            router=router,
        )
        assert result is None
