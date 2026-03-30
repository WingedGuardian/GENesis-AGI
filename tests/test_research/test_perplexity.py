"""Tests for genesis.research.perplexity."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from genesis.providers.types import ProviderStatus
from genesis.research.perplexity import PerplexityAdapter


def _mock_response(data: dict, status_code: int = 200) -> httpx.Response:
    """Build a fake httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp,
        )
    return resp


SAMPLE_API_RESPONSE = {
    "choices": [
        {"message": {"content": "Python is a programming language."}}
    ],
    "citations": [
        "https://python.org",
        "https://docs.python.org",
        "https://wiki.python.org",
    ],
}


class TestPerplexityAdapter:
    @pytest.mark.asyncio
    async def test_health_no_key(self, monkeypatch):
        monkeypatch.delenv("API_KEY_PERPLEXITY", raising=False)
        adapter = PerplexityAdapter()
        assert await adapter.check_health() == ProviderStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_health_with_key(self, monkeypatch):
        monkeypatch.setenv("API_KEY_PERPLEXITY", "test-key")
        adapter = PerplexityAdapter()
        assert await adapter.check_health() == ProviderStatus.AVAILABLE

    @pytest.mark.asyncio
    async def test_search_returns_results(self, monkeypatch):
        monkeypatch.setenv("API_KEY_PERPLEXITY", "test-key")
        adapter = PerplexityAdapter()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=_mock_response(SAMPLE_API_RESPONSE))

        monkeypatch.setattr("genesis.research.perplexity.httpx.AsyncClient", lambda **kw: mock_client)

        results = await adapter.search("what is python")
        assert len(results) >= 1
        assert results[0].snippet == "Python is a programming language."
        assert results[0].source == "perplexity"

    @pytest.mark.asyncio
    async def test_search_parses_citations(self, monkeypatch):
        monkeypatch.setenv("API_KEY_PERPLEXITY", "test-key")
        adapter = PerplexityAdapter()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=_mock_response(SAMPLE_API_RESPONSE))

        monkeypatch.setattr("genesis.research.perplexity.httpx.AsyncClient", lambda **kw: mock_client)

        results = await adapter.search("what is python")
        # 1 answer + 2 extra citations = 3 results
        assert len(results) == 3
        assert results[0].url == "https://python.org"
        assert results[1].url == "https://docs.python.org"
        assert results[2].url == "https://wiki.python.org"

    @pytest.mark.asyncio
    async def test_search_handles_api_error(self, monkeypatch):
        monkeypatch.setenv("API_KEY_PERPLEXITY", "test-key")
        adapter = PerplexityAdapter()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=_mock_response({}, status_code=500))

        monkeypatch.setattr("genesis.research.perplexity.httpx.AsyncClient", lambda **kw: mock_client)

        # search() raises; invoke() catches
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.search("test")

    @pytest.mark.asyncio
    async def test_search_no_key_returns_empty(self, monkeypatch):
        monkeypatch.delenv("API_KEY_PERPLEXITY", raising=False)
        adapter = PerplexityAdapter()
        results = await adapter.search("test")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_empty_response(self, monkeypatch):
        monkeypatch.setenv("API_KEY_PERPLEXITY", "test-key")
        adapter = PerplexityAdapter()

        empty_resp = {"choices": [], "citations": []}
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=_mock_response(empty_resp))

        monkeypatch.setattr("genesis.research.perplexity.httpx.AsyncClient", lambda **kw: mock_client)

        results = await adapter.search("test")
        assert results == []

    @pytest.mark.asyncio
    async def test_invoke_success(self, monkeypatch):
        monkeypatch.setenv("API_KEY_PERPLEXITY", "test-key")
        adapter = PerplexityAdapter()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=_mock_response(SAMPLE_API_RESPONSE))

        monkeypatch.setattr("genesis.research.perplexity.httpx.AsyncClient", lambda **kw: mock_client)

        result = await adapter.invoke({"query": "what is python"})
        assert result.success is True
        assert result.provider_name == "perplexity"
        assert result.latency_ms >= 0
        assert len(result.data) == 3

    @pytest.mark.asyncio
    async def test_invoke_handles_error(self, monkeypatch):
        monkeypatch.setenv("API_KEY_PERPLEXITY", "test-key")
        adapter = PerplexityAdapter()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=_mock_response({}, status_code=500))

        monkeypatch.setattr("genesis.research.perplexity.httpx.AsyncClient", lambda **kw: mock_client)

        result = await adapter.invoke({"query": "test"})
        assert result.success is False
        assert result.error is not None
        assert result.provider_name == "perplexity"
