"""Tests for MarketScanner."""

from __future__ import annotations

from unittest.mock import AsyncMock

from genesis.modules.prediction_markets.scanner import MarketScanner, ScanCriteria
from genesis.modules.prediction_markets.types import Market, MarketStatus


def _market(**kw) -> Market:
    defaults = {
        "title": "Test",
        "current_price": 0.5,
        "volume": 1000,
        "liquidity": 500,
        "status": MarketStatus.OPEN,
        "categories": ["politics"],
    }
    defaults.update(kw)
    return Market(**defaults)


class TestMarketScannerFilter:
    def test_filters_closed_markets(self):
        scanner = MarketScanner()
        markets = [_market(status=MarketStatus.CLOSED)]
        assert scanner.filter_markets(markets) == []

    def test_filters_by_min_volume(self):
        scanner = MarketScanner(criteria=ScanCriteria(min_volume=500))
        markets = [_market(volume=100), _market(volume=1000)]
        result = scanner.filter_markets(markets)
        assert len(result) == 1

    def test_filters_extreme_prices(self):
        scanner = MarketScanner(criteria=ScanCriteria(min_price=0.05, max_price=0.95))
        markets = [_market(current_price=0.02), _market(current_price=0.5), _market(current_price=0.98)]
        result = scanner.filter_markets(markets)
        assert len(result) == 1

    def test_filters_by_category(self):
        scanner = MarketScanner(criteria=ScanCriteria(categories=["crypto"]))
        markets = [_market(categories=["politics"]), _market(categories=["crypto"])]
        result = scanner.filter_markets(markets)
        assert len(result) == 1

    def test_excludes_categories(self):
        scanner = MarketScanner(criteria=ScanCriteria(exclude_categories=["sports"]))
        markets = [_market(categories=["sports"]), _market(categories=["politics"])]
        result = scanner.filter_markets(markets)
        assert len(result) == 1

    def test_no_criteria_passes_open_markets(self):
        scanner = MarketScanner()
        markets = [_market(), _market()]
        result = scanner.filter_markets(markets)
        assert len(result) == 2


class TestMarketScannerRank:
    def test_mid_price_ranked_higher(self):
        scanner = MarketScanner()
        m1 = _market(current_price=0.5, volume=1000)
        m2 = _market(current_price=0.9, volume=1000)
        ranked = scanner.rank_markets([m1, m2])
        assert ranked[0].current_price == 0.5

    def test_higher_volume_helps_ranking(self):
        scanner = MarketScanner()
        m1 = _market(current_price=0.5, volume=100)
        m2 = _market(current_price=0.5, volume=100000)
        ranked = scanner.rank_markets([m1, m2])
        assert ranked[0].volume > ranked[1].volume


class TestMarketScannerFetch:
    async def test_fetch_from_connectors(self):
        connector = AsyncMock()
        connector.fetch_markets.return_value = [_market(), _market()]
        scanner = MarketScanner(connectors=[connector])
        markets = await scanner.fetch_markets()
        assert len(markets) == 2
        assert scanner.market_count == 2

    async def test_fetch_handles_connector_error(self):
        connector = AsyncMock()
        connector.fetch_markets.side_effect = RuntimeError("API down")
        scanner = MarketScanner(connectors=[connector])
        markets = await scanner.fetch_markets()
        assert len(markets) == 0

    async def test_scan_full_pipeline(self):
        connector = AsyncMock()
        connector.fetch_markets.return_value = [
            _market(current_price=0.5, volume=1000),
            _market(current_price=0.99, volume=10),  # filtered out
        ]
        scanner = MarketScanner(
            criteria=ScanCriteria(max_price=0.95),
            connectors=[connector],
        )
        result = await scanner.scan()
        assert len(result) == 1
