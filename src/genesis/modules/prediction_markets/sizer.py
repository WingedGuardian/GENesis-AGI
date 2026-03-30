"""Position sizer — fractional Kelly criterion for bet sizing."""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SizingResult:
    """Result of position sizing calculation."""

    recommended_size: float  # Dollar amount
    kelly_fraction: float  # Full Kelly fraction
    applied_fraction: float  # After fractional Kelly adjustment
    edge: float  # Estimated edge
    bankroll_percentage: float  # % of bankroll
    rationale: str = ""


class PositionSizer:
    """Fractional Kelly criterion for prediction market position sizing.

    Conservative by default (1/4 Kelly). Never suggests going all-in.
    Respects configurable max position size as a hard cap.

    Kelly formula for binary markets:
        f* = (p * b - q) / b
    where:
        p = estimated true probability
        q = 1 - p
        b = odds offered (payout ratio)
    """

    def __init__(
        self,
        *,
        bankroll: float = 1000.0,
        kelly_fraction: float = 0.25,  # 1/4 Kelly
        max_position_pct: float = 0.10,  # Max 10% of bankroll
        min_edge: float = 0.05,  # Minimum 5% edge to recommend a bet
    ) -> None:
        if not 0 < kelly_fraction <= 1:
            raise ValueError("kelly_fraction must be in (0, 1]")
        if not 0 < max_position_pct <= 1:
            raise ValueError("max_position_pct must be in (0, 1]")

        self._bankroll = bankroll
        self._kelly_fraction = kelly_fraction
        self._max_position_pct = max_position_pct
        self._min_edge = min_edge

    @property
    def bankroll(self) -> float:
        return self._bankroll

    @bankroll.setter
    def bankroll(self, value: float) -> None:
        if value <= 0:
            raise ValueError("Bankroll must be positive")
        self._bankroll = value

    def size(
        self,
        estimated_prob: float,
        market_price: float,
        *,
        confidence: float = 1.0,
    ) -> SizingResult:
        """Calculate recommended position size.

        Args:
            estimated_prob: Our estimated true probability (0-1).
            market_price: Current market price / implied probability (0-1).
            confidence: Confidence in our estimate (0-1). Scales down Kelly.

        Returns:
            SizingResult with recommended size and rationale.
        """
        # Validate inputs
        estimated_prob = max(0.01, min(0.99, estimated_prob))
        market_price = max(0.01, min(0.99, market_price))
        confidence = max(0.0, min(1.0, confidence))

        edge = estimated_prob - market_price

        # Check minimum edge threshold
        if abs(edge) < self._min_edge:
            return SizingResult(
                recommended_size=0.0,
                kelly_fraction=0.0,
                applied_fraction=0.0,
                edge=edge,
                bankroll_percentage=0.0,
                rationale=f"Edge ({edge:.1%}) below minimum threshold ({self._min_edge:.1%})",
            )

        # Determine direction and calculate Kelly
        if edge > 0:
            # Bet YES: buy at market_price, win (1 - market_price) if yes
            b = (1 - market_price) / market_price  # payout odds
            p = estimated_prob
        else:
            # Bet NO: buy at (1 - market_price), win market_price if no
            b = market_price / (1 - market_price)
            p = 1 - estimated_prob

        q = 1 - p
        full_kelly = (p * b - q) / b if b > 0 else 0

        # Kelly can be negative if edge is illusory after odds conversion
        if full_kelly <= 0:
            return SizingResult(
                recommended_size=0.0,
                kelly_fraction=full_kelly,
                applied_fraction=0.0,
                edge=edge,
                bankroll_percentage=0.0,
                rationale="Kelly criterion negative — no bet recommended",
            )

        # Apply fractional Kelly and confidence scaling
        applied = full_kelly * self._kelly_fraction * confidence

        # Cap at max position
        applied = min(applied, self._max_position_pct)

        # Calculate dollar amount
        size = self._bankroll * applied

        direction = "YES" if edge > 0 else "NO"
        rationale = (
            f"Bet {direction} | Edge: {abs(edge):.1%} | "
            f"Full Kelly: {full_kelly:.1%} | "
            f"Applied ({self._kelly_fraction:.0%} Kelly × {confidence:.0%} confidence): {applied:.1%} | "
            f"Size: ${size:.2f} of ${self._bankroll:.2f} bankroll"
        )

        return SizingResult(
            recommended_size=round(size, 2),
            kelly_fraction=full_kelly,
            applied_fraction=applied,
            edge=edge,
            bankroll_percentage=applied,
            rationale=rationale,
        )

    def size_portfolio(
        self,
        positions: list[dict],
    ) -> list[SizingResult]:
        """Size multiple positions with portfolio-level constraints.

        Args:
            positions: List of dicts with 'estimated_prob', 'market_price',
                      and optional 'confidence'.

        Returns:
            List of SizingResults, scaled down if total exceeds limits.
        """
        results = [
            self.size(
                p["estimated_prob"],
                p["market_price"],
                confidence=p.get("confidence", 1.0),
            )
            for p in positions
        ]

        # Check total allocation
        total_pct = sum(r.bankroll_percentage for r in results)
        max_total = self._max_position_pct * 5  # Max 50% total allocation (5 × 10%)

        if total_pct > max_total and total_pct > 0:
            scale = max_total / total_pct
            scaled = []
            for r in results:
                new_pct = r.bankroll_percentage * scale
                scaled.append(SizingResult(
                    recommended_size=round(self._bankroll * new_pct, 2),
                    kelly_fraction=r.kelly_fraction,
                    applied_fraction=r.applied_fraction * scale,
                    edge=r.edge,
                    bankroll_percentage=new_pct,
                    rationale=r.rationale + f" [scaled {scale:.0%} for portfolio limit]",
                ))
            return scaled

        return results
