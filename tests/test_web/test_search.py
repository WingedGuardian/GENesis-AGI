"""Tests for genesis.web.search — WebSearcher with SearXNG/Brave fallback."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from genesis.web.search import WebSearcher
from genesis.web.types import SearchBackend


def _ok_response(data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    return resp


SEARXNG_RESPONSE = {
    "results": [
        {"title": "Result 1", "url": "https://a.com", "content": "Snippet 1", "score": 0.9},
        {"title": "Result 2", "url": "https://b.com", "content": "Snippet 2", "score": 0.5},
    ],
}

BRAVE_RESPONSE = {
    "web": {
        "results": [
            {"title": "Brave 1", "url": "https://c.com", "description": "Brave snippet"},
        ],
    },
}


@pytest.fixture
def searcher():
    return WebSearcher(searxng_url="http://test:55510/search", brave_url="http://test-brave/search")


@pytest.mark.asyncio
async def test_searxng_success(searcher: WebSearcher):
    searcher._client.post = AsyncMock(return_value=_ok_response(SEARXNG_RESPONSE))
    resp = await searcher.search("test")
    assert len(resp.results) == 2
    assert "Snippet 1" in resp.results[0].snippet
    assert resp.results[0].score == 0.9
    assert resp.backend_used == SearchBackend.SEARXNG
    assert not resp.fallback_used


@pytest.mark.asyncio
async def test_searxng_failure_falls_back_to_brave(searcher: WebSearcher, monkeypatch):
    monkeypatch.setenv("API_KEY_BRAVE", "test-key")

    async def side_effect(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    searcher._client.post = AsyncMock(side_effect=side_effect)
    searcher._client.get = AsyncMock(return_value=_ok_response(BRAVE_RESPONSE))

    resp = await searcher.search("test")
    assert resp.backend_used == SearchBackend.BRAVE
    assert resp.fallback_used is True
    assert len(resp.results) == 1
    assert "Brave snippet" in resp.results[0].snippet


@pytest.mark.asyncio
async def test_both_backends_fail(searcher: WebSearcher, monkeypatch):
    monkeypatch.setenv("API_KEY_BRAVE", "test-key")
    searcher._client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
    searcher._client.get = AsyncMock(side_effect=httpx.ConnectError("also down"))

    resp = await searcher.search("test")
    assert resp.error is not None
    assert resp.results == []


@pytest.mark.asyncio
async def test_brave_no_api_key(searcher: WebSearcher, monkeypatch):
    monkeypatch.delenv("API_KEY_BRAVE", raising=False)
    searcher._client.post = AsyncMock(side_effect=httpx.ConnectError("down"))

    resp = await searcher.search("test")
    assert resp.error is not None
    assert "unavailable" in resp.error.lower()


@pytest.mark.asyncio
async def test_max_results_limits_output(searcher: WebSearcher):
    big = {"results": [{"title": f"R{i}", "url": f"https://{i}.com", "content": f"S{i}"}
                        for i in range(20)]}
    searcher._client.post = AsyncMock(return_value=_ok_response(big))

    resp = await searcher.search("test", max_results=5)
    assert len(resp.results) == 5


@pytest.mark.asyncio
async def test_brave_count_capped_at_20(searcher: WebSearcher, monkeypatch):
    monkeypatch.setenv("API_KEY_BRAVE", "test-key")
    searcher._client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
    searcher._client.get = AsyncMock(return_value=_ok_response(BRAVE_RESPONSE))

    await searcher.search("test", max_results=50)
    call_kwargs = searcher._client.get.call_args
    assert call_kwargs.kwargs["params"]["count"] == 20


@pytest.mark.asyncio
async def test_event_bus_on_searxng_failure(searcher: WebSearcher, monkeypatch):
    monkeypatch.setenv("API_KEY_BRAVE", "test-key")
    bus = AsyncMock()
    searcher._event_bus = bus
    searcher._client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
    searcher._client.get = AsyncMock(return_value=_ok_response(BRAVE_RESPONSE))

    await searcher.search("test")
    bus.emit.assert_called_once()
    args = bus.emit.call_args[0]
    assert args[2] == "search.searxng_failed"


@pytest.mark.asyncio
async def test_event_bus_on_all_failure(searcher: WebSearcher, monkeypatch):
    monkeypatch.setenv("API_KEY_BRAVE", "test-key")
    bus = AsyncMock()
    searcher._event_bus = bus
    searcher._client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
    searcher._client.get = AsyncMock(side_effect=httpx.ConnectError("also down"))

    await searcher.search("test")
    assert bus.emit.call_count == 2
    last_args = bus.emit.call_args_list[-1][0]
    assert last_args[2] == "search.all_failed"


@pytest.mark.asyncio
async def test_empty_results(searcher: WebSearcher):
    searcher._client.post = AsyncMock(return_value=_ok_response({"results": []}))
    resp = await searcher.search("test")
    assert resp.results == []
    assert resp.error is None
