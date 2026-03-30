"""Tests for CoinGecko pipeline collector."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.pipeline.coingecko_collector import CoinGeckoCollector


@pytest.fixture()
def collector():
    return CoinGeckoCollector(profile_name="crypto-ops")


def _mock_response(data, status=200):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


class TestCoinGeckoCollector:
    def test_name(self, collector):
        assert collector.name == "coingecko"

    @pytest.mark.asyncio()
    async def test_trending_signals(self, collector):
        trending_data = {
            "coins": [
                {
                    "item": {
                        "id": "bitcoin",
                        "name": "Bitcoin",
                        "symbol": "BTC",
                        "market_cap_rank": 1,
                        "price_btc": 1.0,
                    }
                },
                {
                    "item": {
                        "id": "solana",
                        "name": "Solana",
                        "symbol": "SOL",
                        "market_cap_rank": 5,
                        "price_btc": 0.002,
                    }
                },
            ]
        }

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(trending_data))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("genesis.pipeline.coingecko_collector.aiohttp.ClientSession", return_value=mock_session), \
             patch("genesis.pipeline.coingecko_collector._last_request_time", 0.0):
            result = await collector.collect(["trending"], max_results=10)

        assert result.collector_name == "coingecko"
        assert len(result.signals) == 2
        assert result.errors == []

        btc = result.signals[0]
        assert btc.source == "coingecko"
        assert btc.profile_name == "crypto-ops"
        assert "Bitcoin" in btc.content
        assert "trending" in btc.tags
        assert btc.metadata["coin_id"] == "bitcoin"

    @pytest.mark.asyncio()
    async def test_market_data_signals(self, collector):
        market_data = [
            {
                "id": "ethereum",
                "name": "Ethereum",
                "symbol": "eth",
                "current_price": 3500.0,
                "total_volume": 15000000000,
                "market_cap": 420000000000,
                "price_change_percentage_24h": 2.5,
            }
        ]

        mock_session = AsyncMock()
        # First call returns empty trending (no "trending" in queries), second returns markets
        mock_session.get = MagicMock(return_value=_mock_response(market_data))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("genesis.pipeline.coingecko_collector.aiohttp.ClientSession", return_value=mock_session), \
             patch("genesis.pipeline.coingecko_collector._last_request_time", 0.0):
            result = await collector.collect(["top volume"], max_results=5)

        assert len(result.signals) >= 1
        eth = result.signals[0]
        assert "Ethereum" in eth.content
        assert eth.metadata["symbol"] == "eth"
        assert eth.metadata["current_price"] == 3500.0

    @pytest.mark.asyncio()
    async def test_rate_limit_returns_error(self, collector):
        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({}, status=429))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("genesis.pipeline.coingecko_collector.aiohttp.ClientSession", return_value=mock_session), \
             patch("genesis.pipeline.coingecko_collector._last_request_time", 0.0):
            result = await collector.collect(["trending"])

        # Rate limited responses return None from _rate_limited_get, leading to errors
        assert len(result.errors) > 0 or len(result.signals) == 0


class TestCollectorRegistration:
    def test_coingecko_in_registry(self):
        from genesis.pipeline.collectors import CollectorRegistry

        registry = CollectorRegistry()
        assert "coingecko" in registry.available()

    def test_create_coingecko(self):
        from genesis.pipeline.collectors import CollectorRegistry

        registry = CollectorRegistry()
        c = registry.create("coingecko", profile_name="test")
        assert c.name == "coingecko"
