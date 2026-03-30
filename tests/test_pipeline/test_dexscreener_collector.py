"""Tests for DexScreener pipeline collector."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.pipeline.dexscreener_collector import DexScreenerCollector


@pytest.fixture()
def collector():
    return DexScreenerCollector(profile_name="crypto-ops")


def _mock_response(data, status=200):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


class TestDexScreenerCollector:
    def test_name(self, collector):
        assert collector.name == "dexscreener"

    @pytest.mark.asyncio()
    async def test_pair_search(self, collector):
        data = {
            "pairs": [
                {
                    "pairAddress": "0xabc123",
                    "chainId": "solana",
                    "dexId": "raydium",
                    "url": "https://dexscreener.com/solana/0xabc123",
                    "baseToken": {
                        "address": "0xtoken",
                        "name": "TestCoin",
                        "symbol": "TEST",
                    },
                    "priceUsd": "0.001234",
                    "volume": {"h24": 500000},
                    "liquidity": {"usd": 100000},
                    "priceChange": {"h24": 15.5},
                    "pairCreatedAt": 1710000000000,
                }
            ]
        }

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(data))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("genesis.pipeline.dexscreener_collector.aiohttp.ClientSession", return_value=mock_session):
            result = await collector.collect(["new pairs solana"], max_results=10)

        assert result.collector_name == "dexscreener"
        assert len(result.signals) == 1
        assert result.errors == []

        signal = result.signals[0]
        assert signal.source == "dexscreener"
        assert "TestCoin" in signal.content
        assert signal.metadata["chain"] == "solana"
        assert signal.metadata["volume_24h"] == 500000
        assert signal.metadata["liquidity_usd"] == 100000

    @pytest.mark.asyncio()
    async def test_dedup_across_queries(self, collector):
        """Same pair from different queries should not produce duplicates."""
        pair = {
            "pairAddress": "0xsame",
            "chainId": "base",
            "baseToken": {"name": "Dup", "symbol": "DUP"},
            "priceUsd": "1.0",
            "volume": {"h24": 100},
            "liquidity": {"usd": 50},
            "priceChange": {"h24": 0},
        }
        data = {"pairs": [pair]}

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(data))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("genesis.pipeline.dexscreener_collector.aiohttp.ClientSession", return_value=mock_session):
            result = await collector.collect(["query1", "query2"])

        # Same pair address — should appear only once
        assert len(result.signals) == 1

    @pytest.mark.asyncio()
    async def test_rate_limit_records_error(self, collector):
        mock_session = AsyncMock()
        resp = _mock_response({}, status=429)
        resp.raise_for_status = MagicMock(side_effect=Exception("429 Too Many Requests"))
        mock_session.get = MagicMock(return_value=resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("genesis.pipeline.dexscreener_collector.aiohttp.ClientSession", return_value=mock_session):
            result = await collector.collect(["test"])

        assert len(result.errors) > 0
        assert len(result.signals) == 0

    @pytest.mark.asyncio()
    async def test_empty_pairs(self, collector):
        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({"pairs": []}))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("genesis.pipeline.dexscreener_collector.aiohttp.ClientSession", return_value=mock_session):
            result = await collector.collect(["nothing"])

        assert len(result.signals) == 0
        assert result.errors == []


class TestCollectorRegistration:
    def test_dexscreener_in_registry(self):
        from genesis.pipeline.collectors import CollectorRegistry

        registry = CollectorRegistry()
        assert "dexscreener" in registry.available()

    def test_create_dexscreener(self):
        from genesis.pipeline.collectors import CollectorRegistry

        registry = CollectorRegistry()
        c = registry.create("dexscreener", profile_name="test")
        assert c.name == "dexscreener"
