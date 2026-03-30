"""Market scanner — filters and ranks active prediction markets."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from genesis.modules.prediction_markets.types import Market, MarketStatus

logger = logging.getLogger(__name__)


@dataclass
class ScanCriteria:
    """Criteria for filtering markets worth analyzing."""

    min_volume: float = 0.0
    min_liquidity: float = 0.0
    max_close_days: int = 365  # Only markets closing within N days
    categories: list[str] = field(default_factory=list)  # Empty = all
    exclude_categories: list[str] = field(default_factory=list)
    min_price: float = 0.05  # Skip markets priced below 5%
    max_price: float = 0.95  # Skip markets priced above 95%


class MarketScanner:
    """Scans and filters prediction market opportunities.

    Maintains a ranked view of markets worth analyzing. Filters by
    liquidity, timeframe, and category. Identifies markets where
    Genesis has existing signal (knowledge pipeline overlap).
    """

    def __init__(
        self,
        criteria: ScanCriteria | None = None,
        *,
        connectors: list[Any] | None = None,
    ) -> None:
        self._criteria = criteria or ScanCriteria()
        self._connectors = connectors or []
        self._markets: dict[str, Market] = {}

    @property
    def criteria(self) -> ScanCriteria:
        return self._criteria

    @property
    def market_count(self) -> int:
        return len(self._markets)

    async def fetch_markets(self) -> list[Market]:
        """Fetch markets from all registered connectors.

        Each connector must implement: async def fetch_markets() -> list[Market]
        """
        all_markets: list[Market] = []
        for connector in self._connectors:
            try:
                markets = await connector.fetch_markets()
                all_markets.extend(markets)
            except Exception:
                logger.warning(
                    "Failed to fetch from connector %s",
                    getattr(connector, "name", "unknown"),
                    exc_info=True,
                )
        # Update internal cache
        for m in all_markets:
            self._markets[m.id] = m
        return all_markets

    def filter_markets(self, markets: list[Market]) -> list[Market]:
        """Apply scan criteria to filter markets."""
        result = []
        for m in markets:
            if m.status != MarketStatus.OPEN:
                continue
            if m.volume < self._criteria.min_volume:
                continue
            if m.liquidity < self._criteria.min_liquidity:
                continue
            if m.current_price < self._criteria.min_price:
                continue
            if m.current_price > self._criteria.max_price:
                continue
            if self._criteria.categories and not any(
                c in m.categories for c in self._criteria.categories
            ):
                continue
            if any(c in m.categories for c in self._criteria.exclude_categories):
                continue
            result.append(m)
        return result

    def rank_markets(self, markets: list[Market]) -> list[Market]:
        """Rank markets by attractiveness.

        Scoring: higher volume + mid-range prices (more edge potential)
        get ranked higher. Markets priced near 0.5 have more room for
        disagreement than those near extremes.
        """
        def score(m: Market) -> float:
            # Price entropy: max at 0.5, min at 0/1
            price_score = 1.0 - abs(m.current_price - 0.5) * 2
            # Volume score: log-scale, normalized
            import math
            vol_score = math.log1p(m.volume) / 20.0  # rough normalization
            return price_score * 0.6 + min(vol_score, 1.0) * 0.4

        return sorted(markets, key=score, reverse=True)

    async def scan(self) -> list[Market]:
        """Full scan: fetch → filter → rank."""
        raw = await self.fetch_markets()
        filtered = self.filter_markets(raw)
        ranked = self.rank_markets(filtered)
        logger.info(
            "Market scan: %d fetched, %d after filter, returning top ranked",
            len(raw),
            len(filtered),
        )
        return ranked
