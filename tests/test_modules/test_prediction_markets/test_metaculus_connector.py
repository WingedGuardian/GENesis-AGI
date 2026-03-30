"""Tests for Metaculus connector."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.modules.prediction_markets.connectors.metaculus import MetaculusConnector
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
    return MetaculusConnector()


class TestMetaculusConnector:
    def test_name(self, connector):
        assert connector.name == "metaculus"

    @pytest.mark.asyncio()
    async def test_fetch_questions(self, connector):
        data = {
            "results": [
                {
                    "id": 12345,
                    "title": "Will AI pass the bar exam by 2027?",
                    "description": "A question about AI capabilities",
                    "community_prediction": {"full": {"q2": 0.78}},
                    "number_of_predictions": 250,
                    "close_time": "2027-01-01T00:00:00Z",
                    "resolve_time": "2027-06-01T00:00:00Z",
                    "url": "/questions/12345/ai-bar-exam/",
                    "categories": [{"name": "AI"}],
                }
            ]
        }

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(data))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("genesis.modules.prediction_markets.connectors.metaculus.aiohttp.ClientSession", return_value=mock_session):
            markets = await connector.fetch_markets(limit=10)

        assert len(markets) == 1
        m = markets[0]
        assert m.source == MarketSource.METACULUS
        assert m.title == "Will AI pass the bar exam by 2027?"
        assert m.current_price == 0.78
        assert m.volume == 250.0  # prediction count
        assert m.status == MarketStatus.OPEN
        assert "AI" in m.categories
        assert m.id == "meta_12345"
        assert "metaculus.com" in m.url

    @pytest.mark.asyncio()
    async def test_numeric_community_prediction(self, connector):
        """Handle cases where community_prediction is a simple float."""
        data = {
            "results": [
                {
                    "id": 99,
                    "title": "Simple question",
                    "community_prediction": 0.42,
                    "number_of_predictions": 10,
                    "url": "/questions/99/",
                }
            ]
        }

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(data))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("genesis.modules.prediction_markets.connectors.metaculus.aiohttp.ClientSession", return_value=mock_session):
            markets = await connector.fetch_markets()

        assert markets[0].current_price == 0.42

    @pytest.mark.asyncio()
    async def test_api_error_returns_empty(self, connector):
        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response({}, status=403))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("genesis.modules.prediction_markets.connectors.metaculus.aiohttp.ClientSession", return_value=mock_session):
            markets = await connector.fetch_markets()

        assert markets == []

    @pytest.mark.asyncio()
    async def test_missing_prediction_defaults(self, connector):
        """Questions without community prediction default to 0.5."""
        data = {
            "results": [
                {
                    "id": 77,
                    "title": "New question",
                    "url": "/questions/77/",
                }
            ]
        }

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=_mock_response(data))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("genesis.modules.prediction_markets.connectors.metaculus.aiohttp.ClientSession", return_value=mock_session):
            markets = await connector.fetch_markets()

        assert markets[0].current_price == 0.5
