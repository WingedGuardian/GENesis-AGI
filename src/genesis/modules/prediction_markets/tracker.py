"""Outcome tracker — Brier scores, calibration curves, isolated bet tracking."""

from __future__ import annotations

import logging
from collections import defaultdict

from genesis.modules.prediction_markets.types import BetRecord, BetStatus

logger = logging.getLogger(__name__)


class OutcomeTracker:
    """Tracks prediction market outcomes in isolation from Genesis core.

    Records every bet (placed or considered). Computes:
    - Brier scores per category and overall
    - Calibration data (predicted probability vs actual frequency)
    - Win rate and ROI for placed bets
    - Category-level accuracy breakdowns

    This data stays module-local. Only process/methodology lessons
    are promoted to Genesis core via the generalization filter.
    """

    def __init__(self) -> None:
        self._records: dict[str, BetRecord] = {}

    @property
    def total_records(self) -> int:
        return len(self._records)

    def record_bet(self, bet: BetRecord) -> None:
        """Add or update a bet record."""
        self._records[bet.id] = bet

    def get_bet(self, bet_id: str) -> BetRecord | None:
        return self._records.get(bet_id)

    def resolve_bet(
        self,
        bet_id: str,
        actual_outcome: float,
        *,
        resolved_at: str = "",
    ) -> BetRecord | None:
        """Resolve a bet and compute Brier score.

        Args:
            bet_id: The bet to resolve.
            actual_outcome: 1.0 (yes) or 0.0 (no).
            resolved_at: ISO timestamp of resolution.
        """
        bet = self._records.get(bet_id)
        if bet is None:
            logger.warning("Cannot resolve unknown bet %s", bet_id)
            return None

        bet.actual_outcome = actual_outcome
        bet.resolved_at = resolved_at

        # Brier score: (forecast - outcome)^2
        bet.brier_score = (bet.genesis_estimate - actual_outcome) ** 2

        # Determine win/loss for placed bets
        if bet.status == BetStatus.PLACED:
            if bet.direction == "yes":
                bet.status = BetStatus.WON if actual_outcome == 1.0 else BetStatus.LOST
            else:
                bet.status = BetStatus.WON if actual_outcome == 0.0 else BetStatus.LOST

            # P&L calculation (simplified binary market)
            if bet.status == BetStatus.WON:
                if bet.direction == "yes":
                    bet.pnl = bet.position_size * (1 - bet.market_price_at_entry) / bet.market_price_at_entry
                else:
                    bet.pnl = bet.position_size * bet.market_price_at_entry / (1 - bet.market_price_at_entry)
            else:
                bet.pnl = -bet.position_size

        return bet

    def brier_score(self, *, category: str | None = None) -> float | None:
        """Compute average Brier score across resolved bets.

        Args:
            category: Optional category filter.

        Returns:
            Average Brier score, or None if no resolved bets.
        """
        resolved = [
            r for r in self._records.values()
            if r.brier_score is not None
            and (category is None or r.category == category)
        ]
        if not resolved:
            return None
        return sum(r.brier_score for r in resolved) / len(resolved)

    def calibration_data(self, *, n_bins: int = 10) -> list[dict]:
        """Generate calibration curve data.

        Groups resolved bets into probability bins and compares
        predicted probability vs actual frequency.

        Returns:
            List of dicts with 'bin_center', 'predicted_avg',
            'actual_frequency', 'count' for each bin.
        """
        resolved = [
            r for r in self._records.values()
            if r.brier_score is not None and r.actual_outcome is not None
        ]
        if not resolved:
            return []

        bin_width = 1.0 / n_bins
        bins: dict[int, list[BetRecord]] = defaultdict(list)

        for r in resolved:
            bin_idx = min(int(r.genesis_estimate / bin_width), n_bins - 1)
            bins[bin_idx].append(r)

        result = []
        for i in range(n_bins):
            bets = bins.get(i, [])
            if not bets:
                continue
            bin_center = (i + 0.5) * bin_width
            predicted_avg = sum(b.genesis_estimate for b in bets) / len(bets)
            actual_freq = sum(b.actual_outcome for b in bets) / len(bets)
            result.append({
                "bin_center": round(bin_center, 2),
                "predicted_avg": round(predicted_avg, 3),
                "actual_frequency": round(actual_freq, 3),
                "count": len(bets),
            })

        return result

    def win_rate(self) -> float | None:
        """Win rate for placed bets only."""
        placed = [
            r for r in self._records.values()
            if r.status in (BetStatus.WON, BetStatus.LOST)
        ]
        if not placed:
            return None
        wins = sum(1 for r in placed if r.status == BetStatus.WON)
        return wins / len(placed)

    def total_pnl(self) -> float:
        """Total P&L across all resolved placed bets."""
        return sum(
            r.pnl for r in self._records.values()
            if r.pnl is not None
        )

    def roi(self) -> float | None:
        """Return on investment (total P&L / total invested)."""
        placed = [
            r for r in self._records.values()
            if r.status in (BetStatus.WON, BetStatus.LOST) and r.position_size > 0
        ]
        if not placed:
            return None
        total_invested = sum(r.position_size for r in placed)
        if total_invested == 0:
            return None
        return self.total_pnl() / total_invested

    def category_summary(self) -> dict[str, dict]:
        """Per-category performance breakdown."""
        categories: dict[str, list[BetRecord]] = defaultdict(list)
        for r in self._records.values():
            if r.brier_score is not None:
                categories[r.category or "uncategorized"].append(r)

        result = {}
        for cat, bets in categories.items():
            brier = sum(b.brier_score for b in bets) / len(bets)
            placed = [b for b in bets if b.status in (BetStatus.WON, BetStatus.LOST)]
            wins = sum(1 for b in placed if b.status == BetStatus.WON)
            result[cat] = {
                "count": len(bets),
                "brier_score": round(brier, 4),
                "win_rate": wins / len(placed) if placed else None,
                "pnl": sum(b.pnl for b in bets if b.pnl is not None),
            }
        return result

    def stats(self) -> dict:
        """Overall statistics summary."""
        return {
            "total_records": self.total_records,
            "brier_score": self.brier_score(),
            "win_rate": self.win_rate(),
            "total_pnl": self.total_pnl(),
            "roi": self.roi(),
            "categories": self.category_summary(),
        }
