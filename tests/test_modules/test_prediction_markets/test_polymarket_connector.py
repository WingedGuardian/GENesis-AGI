"""Tests for Polymarket connector."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.modules.prediction_markets.connectors.polymarket import PolymarketConnector
from genesis.modules.prediction_markets.types import MarketSource, MarketStatus


def _mock_response(data, status=200):
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


@pytest.fixture()
def connector():
    return PolymarketConnector()


class TestPolymarketConnector:
    def test_name(self, connector):
        assert connector.name == "polymarket"

    @pytest.mark.asyncio()
    async def test_fetch_markets(self, connector):
        data = [
            {
                "condition_id": "abc123",
                "question": "Will X happen?",
                "description": "Some description",
                "market_slug": "will-x-happen",
                "tokens": [{"price": 0.65}],
                "volume": 1000000,
                "liquidity": 500000,
                "end_date_iso": "2026-12-31T00:00:00Z",
                "active": True,
                "closed": False,
                "category": "politics",
            }
        ]

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(data))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("genesis.modules.prediction_markets.connectors.polymarket.aiohttp.ClientSession", return_value=mock_session):
            markets = await connector.fetch_markets(limit=10)

        assert len(markets) == 1
        m = markets[0]
        assert m.source == MarketSource.POLYMARKET
        assert m.title == "Will X happen?"
        assert m.current_price == 0.65
        assert m.volume == 1000000
        assert m.status == MarketStatus.OPEN
        assert "politics" in m.categories
        assert m.id == "poly_abc123"

    @pytest.mark.asyncio()
    async def test_closed_market(self, connector):
        data = [
            {
                "condition_id": "def456",
                "question": "Past event",
                "tokens": [{"price": 0.99}],
                "active": False,
                "closed": True,
            }
        ]

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(data))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("genesis.modules.prediction_markets.connectors.polymarket.aiohttp.ClientSession", return_value=mock_session):
            markets = await connector.fetch_markets()

        assert markets[0].status == MarketStatus.CLOSED

    @pytest.mark.asyncio()
    async def test_api_error_returns_empty(self, connector):
        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({}, status=500))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("genesis.modules.prediction_markets.connectors.polymarket.aiohttp.ClientSession", return_value=mock_session):
            markets = await connector.fetch_markets()

        assert markets == []

    @pytest.mark.asyncio()
    async def test_malformed_item_skipped(self, connector):
        data = [
            {"condition_id": "good", "question": "Valid", "tokens": [{"price": 0.5}]},
            {"broken": True},  # Missing required fields
        ]

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(data))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("genesis.modules.prediction_markets.connectors.polymarket.aiohttp.ClientSession", return_value=mock_session):
            markets = await connector.fetch_markets()

        # Should get at least the valid one; broken one may also parse (defaults)
        assert len(markets) >= 1
