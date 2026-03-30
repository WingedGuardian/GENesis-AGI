"""PredictionMarketsModule — CapabilityModule implementation."""

from __future__ import annotations

import json
import logging
from typing import Any

from genesis.modules.prediction_markets.calibration import CalibrationEngine
from genesis.modules.prediction_markets.scanner import MarketScanner
from genesis.modules.prediction_markets.sizer import PositionSizer
from genesis.modules.prediction_markets.tracker import OutcomeTracker
from genesis.modules.prediction_markets.types import (
    BetRecord,
    Market,
)

logger = logging.getLogger(__name__)

GENERALIZE_PROMPT = """You are evaluating a prediction market outcome for generalizable lessons.

Bet details:
- Market: {title}
- Category: {category}
- Genesis estimate: {genesis_estimate:.1%}
- Market price: {market_price:.1%}
- Actual outcome: {outcome}
- Brier score: {brier:.4f}

Is there a process/methodology lesson here that would help reason better in ANY domain?

Rules:
- Market-specific patterns (e.g., "politics markets are inefficient") are NOT generalizable
- Calibration insights ARE generalizable (e.g., "systematically overconfident by X%")
- Research methodology improvements ARE generalizable
- Signal quality findings ARE generalizable (e.g., "social sentiment is a lagging indicator")
- Random noise is NEVER generalizable
- When in doubt, DO NOT promote.

If generalizable, respond with JSON:
{{"generalizable": true, "lesson": "<domain-agnostic observation>", \
"category": "<calibration|process|source_reliability|tool_effectiveness>"}}

If not:
{{"generalizable": false, "reason": "<brief>"}}
"""


class PredictionMarketsModule:
    """Prediction Market Edge — calibration-driven market analysis.

    Semi-autonomous: Genesis researches and recommends, user approves
    each position. Wraps the forecasting skill methodology with market
    data connectors and execution tracking.
    """

    def __init__(
        self,
        *,
        scanner: MarketScanner | None = None,
        calibration: CalibrationEngine | None = None,
        sizer: PositionSizer | None = None,
        tracker: OutcomeTracker | None = None,
    ) -> None:
        self._scanner = scanner or MarketScanner()
        self._calibration = calibration or CalibrationEngine()
        self._sizer = sizer or PositionSizer()
        self._tracker = tracker or OutcomeTracker()
        self._enabled = False
        self._runtime: Any = None

    @property
    def name(self) -> str:
        return "prediction_markets"

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    async def register(self, runtime: Any) -> None:
        """Register with Genesis runtime and wire market data connectors."""
        self._runtime = runtime

        # Wire market data connectors into scanner
        connectors: list[Any] = []
        try:
            from genesis.modules.prediction_markets.connectors.polymarket import PolymarketConnector

            connectors.append(PolymarketConnector())
        except ImportError:
            logger.debug("Polymarket connector not available")
        try:
            from genesis.modules.prediction_markets.connectors.metaculus import MetaculusConnector

            connectors.append(MetaculusConnector())
        except ImportError:
            logger.debug("Metaculus connector not available")

        if connectors:
            self._scanner = MarketScanner(
                criteria=self._scanner.criteria,
                connectors=connectors,
            )
            logger.info("Prediction markets: %d connectors wired", len(connectors))

        logger.info("Prediction markets module registered")

    async def deregister(self) -> None:
        """Clean shutdown."""
        self._runtime = None
        logger.info("Prediction markets module deregistered")

    def get_research_profile_name(self) -> str | None:
        return "prediction-markets"

    async def handle_opportunity(self, opportunity: dict) -> dict | None:
        """Evaluate a market opportunity and produce an action proposal.

        Args:
            opportunity: Dict with at minimum 'market' (Market instance)
                        and optional 'context' (research context string).

        Returns:
            Action proposal dict for user approval, or None if no edge.
        """
        market = opportunity.get("market")
        if not isinstance(market, Market):
            return None

        context = opportunity.get("context", "")
        router = opportunity.get("router")

        # Get calibrated estimate
        estimate = await self._calibration.estimate(
            market, context=context, router=router,
        )
        if estimate is None:
            return None

        # Size position
        sizing = self._sizer.size(
            estimate.estimated_probability,
            market.current_price,
            confidence=estimate.confidence_in_estimate,
        )

        if sizing.recommended_size == 0:
            return None

        return {
            "type": "prediction_market_bet",
            "market": {
                "id": market.id,
                "title": market.title,
                "source": market.source,
                "url": market.url,
            },
            "estimate": {
                "probability": estimate.estimated_probability,
                "market_price": estimate.market_price,
                "edge": estimate.edge,
                "confidence": estimate.confidence_in_estimate,
                "reasoning": estimate.reasoning,
            },
            "sizing": {
                "recommended_size": sizing.recommended_size,
                "bankroll_percentage": sizing.bankroll_percentage,
                "rationale": sizing.rationale,
            },
            "requires_approval": True,
        }

    async def record_outcome(self, outcome: dict) -> None:
        """Record a bet outcome in isolated tracking."""
        bet = BetRecord(**outcome) if isinstance(outcome, dict) else outcome
        self._tracker.record_bet(bet)

    async def extract_generalizable(
        self,
        outcome: dict,
        *,
        router: Any = None,
    ) -> list[dict] | None:
        """Extract generalizable lessons from a resolved bet.

        Uses LLM to evaluate whether the outcome contains process or
        calibration lessons that would help Genesis reason better in
        any domain.
        """
        if router is None:
            return None

        brier = outcome.get("brier_score")
        if brier is None:
            return None

        prompt = GENERALIZE_PROMPT.format(
            title=outcome.get("market_title", "Unknown"),
            category=outcome.get("category", "general"),
            genesis_estimate=outcome.get("genesis_estimate", 0.5),
            market_price=outcome.get("market_price_at_entry", 0.5),
            outcome="YES" if outcome.get("actual_outcome") == 1.0 else "NO",
            brier=brier,
        )

        try:
            response = await router.route(prompt, tier="free")
            result = json.loads(response)
        except Exception:
            logger.warning("Generalization extraction failed", exc_info=True)
            return None

        if not result.get("generalizable", False):
            return None

        return [{
            "source": "module:prediction_markets",
            "lesson": result.get("lesson", ""),
            "category": result.get("category", "calibration"),
        }]

    def configurable_fields(self) -> list[dict]:
        """Return user-editable configuration fields."""
        return [
            {"name": "bankroll", "label": "Bankroll ($)", "type": "float",
             "value": self._sizer.bankroll, "description": "Total bankroll for position sizing"},
            {"name": "kelly_fraction", "label": "Kelly Fraction", "type": "float",
             "value": self._sizer._kelly_fraction, "description": "Fraction of Kelly criterion (0-1)"},
            {"name": "max_position_pct", "label": "Max Position %", "type": "float",
             "value": self._sizer._max_position_pct, "description": "Max % of bankroll per position"},
            {"name": "min_edge", "label": "Min Edge", "type": "float",
             "value": self._sizer._min_edge, "description": "Minimum edge to recommend a bet"},
        ]

    def update_config(self, updates: dict) -> dict:
        """Apply configuration updates with bounds validation."""
        if "bankroll" in updates:
            val = float(updates["bankroll"])
            if val <= 0:
                raise ValueError("bankroll must be positive")
            self._sizer.bankroll = val
        if "kelly_fraction" in updates:
            val = float(updates["kelly_fraction"])
            if not 0 < val <= 1:
                raise ValueError("kelly_fraction must be in (0, 1]")
            self._sizer._kelly_fraction = val
        if "max_position_pct" in updates:
            val = float(updates["max_position_pct"])
            if not 0 < val <= 1:
                raise ValueError("max_position_pct must be in (0, 1]")
            self._sizer._max_position_pct = val
        if "min_edge" in updates:
            val = float(updates["min_edge"])
            if not 0 <= val <= 1:
                raise ValueError("min_edge must be in [0, 1]")
            self._sizer._min_edge = val
        return {f["name"]: f["value"] for f in self.configurable_fields()}
