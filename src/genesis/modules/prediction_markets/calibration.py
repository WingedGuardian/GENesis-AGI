"""Calibration engine — wraps forecasting methodology with market data."""

from __future__ import annotations

import json
import logging
from typing import Any

from genesis.modules.prediction_markets.types import Estimate, Market

logger = logging.getLogger(__name__)

CALIBRATION_PROMPT = """You are a superforecaster evaluating a prediction market.

Market: {title}
Description: {description}
Current market price (implied probability): {price:.1%}
Categories: {categories}

{context}

Apply the superforecasting methodology:
1. Reference Class (outside view): What's the base rate for events like this?
2. Specific Evidence (inside view): What signals adjust the base rate?
3. Synthesis: Combine outside + inside views
4. Bias Check: anchoring, availability, confirmation, narrative, overconfidence,
   scope insensitivity, recency, status quo

Respond with a JSON object:
{{"estimated_probability": <0.05-0.95>, "reasoning": "<2-3 sentence synthesis>", \
"signals_used": ["<signal1>", "<signal2>"], \
"bias_checks": ["<bias checked and adjustment if any>"], \
"confidence_in_estimate": <0.0-1.0 how confident you are in YOUR estimate>}}
"""


class CalibrationEngine:
    """Produces calibrated probability estimates for prediction markets.

    Two modes:
    - thesis_mode: User provides their estimate, Genesis independently
      researches and compares. Highlights agreement/divergence.
    - scan_mode: Genesis estimates probability for a market, identifies
      edge vs market price.

    Wraps the forecasting skill methodology (Tetlock/GJP):
    reference class → specific evidence → synthesis → bias check.
    """

    def __init__(self, *, router: Any = None) -> None:
        self._router = router

    async def estimate(
        self,
        market: Market,
        *,
        context: str = "",
        router: Any = None,
    ) -> Estimate | None:
        """Produce a probability estimate for a market.

        Args:
            market: The market to evaluate.
            context: Additional research context (pipeline output, etc.).
            router: LLM router override.

        Returns:
            Estimate with probability, reasoning, and edge calculation.
        """
        r = router or self._router
        if r is None:
            logger.debug("No router available for calibration, skipping")
            return None

        prompt = CALIBRATION_PROMPT.format(
            title=market.title,
            description=market.description,
            price=market.current_price,
            categories=", ".join(market.categories) if market.categories else "general",
            context=context or "No additional context available.",
        )

        try:
            response = await r.route(prompt, tier="free")
            result = json.loads(response)
        except Exception:
            logger.warning("Calibration LLM call failed", exc_info=True)
            return None

        est_prob = float(result.get("estimated_probability", 0.5))
        # Clamp to valid range
        est_prob = max(0.05, min(0.95, est_prob))

        return Estimate(
            market_id=market.id,
            estimated_probability=est_prob,
            market_price=market.current_price,
            edge=est_prob - market.current_price,
            confidence_in_estimate=float(result.get("confidence_in_estimate", 0.5)),
            reasoning=result.get("reasoning", ""),
            signals_used=result.get("signals_used", []),
            bias_checks=result.get("bias_checks", []),
        )

    async def compare_with_user(
        self,
        market: Market,
        user_estimate: float,
        *,
        context: str = "",
        router: Any = None,
    ) -> dict:
        """Thesis mode: compare user's estimate with Genesis's independent estimate.

        Returns a comparison dict with both estimates, agreement level,
        and whether the combined view suggests edge against the market.
        """
        genesis_est = await self.estimate(market, context=context, router=router)
        if genesis_est is None:
            return {
                "user_estimate": user_estimate,
                "genesis_estimate": None,
                "market_price": market.current_price,
                "error": "Could not produce Genesis estimate",
            }

        # Agreement: how close user and Genesis are
        agreement = 1.0 - abs(user_estimate - genesis_est.estimated_probability)

        # Combined estimate (simple average, weighted by Genesis confidence)
        weight = genesis_est.confidence_in_estimate
        combined = user_estimate * (1 - weight * 0.5) + genesis_est.estimated_probability * (weight * 0.5)

        return {
            "user_estimate": user_estimate,
            "genesis_estimate": genesis_est.estimated_probability,
            "market_price": market.current_price,
            "agreement": agreement,
            "combined_estimate": combined,
            "combined_edge": combined - market.current_price,
            "genesis_reasoning": genesis_est.reasoning,
            "genesis_confidence": genesis_est.confidence_in_estimate,
            "signals": genesis_est.signals_used,
            "bias_checks": genesis_est.bias_checks,
        }
