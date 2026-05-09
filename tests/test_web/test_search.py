"""Tests for genesis.web.search — WebSearcher with Tinyfish/Brave fallback."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

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


TINYFISH_RESPONSE = json.dumps({
    "query": "test",
    "results": [
        {"position": 1, "title": "Result 1", "url": "https://a.com", "snippet": "Snippet 1", "site_name": "a.com"},
        {"position": 2, "title": "Result 2", "url": "https://b.com", "snippet": "Snippet 2", "site_name": "b.com"},
    ],
    "total_results": 2,
}).encode()

BRAVE_RESPONSE = {
    "web": {
        "results": [
            {"title": "Brave 1", "url": "https://c.com", "description": "Brave snippet"},
        ],
    },
}


@pytest.fixture
def searcher():
    return WebSearcher(brave_url="http://test-brave/search")


def _mock_tinyfish(stdout: bytes, returncode: int = 0, stderr: bytes = b""):
    """Create a mock async subprocess for tinyfish CLI."""
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


@pytest.mark.asyncio
async def test_tinyfish_success(searcher: WebSearcher):
    proc = _mock_tinyfish(TINYFISH_RESPONSE)
    with patch("genesis.web.search.asyncio.create_subprocess_exec", return_value=proc):
        resp = await searcher.search("test")
    assert len(resp.results) == 2
    assert "Snippet 1" in resp.results[0].snippet
    assert resp.results[0].score == 1.0
    assert resp.backend_used == SearchBackend.TINYFISH
    assert not resp.fallback_used


@pytest.mark.asyncio
async def test_tinyfish_failure_falls_back_to_brave(searcher: WebSearcher, monkeypatch):
    monkeypatch.setenv("API_KEY_BRAVE", "test-key")

    proc = _mock_tinyfish(b"", returncode=1, stderr=b"auth error")
    with patch("genesis.web.search.asyncio.create_subprocess_exec", return_value=proc):
        searcher._client.get = AsyncMock(return_value=_ok_response(BRAVE_RESPONSE))
        resp = await searcher.search("test")

    assert resp.backend_used == SearchBackend.BRAVE
    assert resp.fallback_used is True
    assert len(resp.results) == 1
    assert "Brave snippet" in resp.results[0].snippet


@pytest.mark.asyncio
async def test_both_backends_fail(searcher: WebSearcher, monkeypatch):
    monkeypatch.setenv("API_KEY_BRAVE", "test-key")

    proc = _mock_tinyfish(b"", returncode=1, stderr=b"error")
    with patch("genesis.web.search.asyncio.create_subprocess_exec", return_value=proc):
        searcher._client.get = AsyncMock(side_effect=httpx.ConnectError("also down"))
        resp = await searcher.search("test")

    assert resp.error is not None
    assert resp.results == []


@pytest.mark.asyncio
async def test_brave_no_api_key(searcher: WebSearcher, monkeypatch):
    monkeypatch.delenv("API_KEY_BRAVE", raising=False)

    proc = _mock_tinyfish(b"", returncode=1, stderr=b"error")
    with patch("genesis.web.search.asyncio.create_subprocess_exec", return_value=proc):
        resp = await searcher.search("test")

    assert resp.error is not None
    assert "unavailable" in resp.error.lower()


@pytest.mark.asyncio
async def test_max_results_limits_output(searcher: WebSearcher):
    results = [
        {"position": i, "title": f"R{i}", "url": f"https://{i}.com", "snippet": f"S{i}"}
        for i in range(20)
    ]
    big_response = json.dumps({
        "query": "test", "results": results, "total_results": 20,
    }).encode()
    proc = _mock_tinyfish(big_response)
    with patch("genesis.web.search.asyncio.create_subprocess_exec", return_value=proc):
        resp = await searcher.search("test", max_results=5)
    assert len(resp.results) == 5


@pytest.mark.asyncio
async def test_brave_count_capped_at_20(searcher: WebSearcher, monkeypatch):
    monkeypatch.setenv("API_KEY_BRAVE", "test-key")

    proc = _mock_tinyfish(b"", returncode=1, stderr=b"error")
    with patch("genesis.web.search.asyncio.create_subprocess_exec", return_value=proc):
        searcher._client.get = AsyncMock(return_value=_ok_response(BRAVE_RESPONSE))
        await searcher.search("test", max_results=50)

    call_kwargs = searcher._client.get.call_args
    assert call_kwargs.kwargs["params"]["count"] == 20


@pytest.mark.asyncio
async def test_event_bus_on_tinyfish_failure(searcher: WebSearcher, monkeypatch):
    monkeypatch.setenv("API_KEY_BRAVE", "test-key")
    bus = AsyncMock()
    searcher._event_bus = bus

    proc = _mock_tinyfish(b"", returncode=1, stderr=b"error")
    with patch("genesis.web.search.asyncio.create_subprocess_exec", return_value=proc):
        searcher._client.get = AsyncMock(return_value=_ok_response(BRAVE_RESPONSE))
        await searcher.search("test")

    bus.emit.assert_called_once()
    args = bus.emit.call_args[0]
    assert args[2] == "search.tinyfish_failed"


@pytest.mark.asyncio
async def test_event_bus_on_all_failure(searcher: WebSearcher, monkeypatch):
    monkeypatch.setenv("API_KEY_BRAVE", "test-key")
    bus = AsyncMock()
    searcher._event_bus = bus

    proc = _mock_tinyfish(b"", returncode=1, stderr=b"error")
    with patch("genesis.web.search.asyncio.create_subprocess_exec", return_value=proc):
        searcher._client.get = AsyncMock(side_effect=httpx.ConnectError("also down"))
        await searcher.search("test")

    assert bus.emit.call_count == 2
    last_args = bus.emit.call_args_list[-1][0]
    assert last_args[2] == "search.all_failed"


@pytest.mark.asyncio
async def test_empty_results(searcher: WebSearcher):
    empty = json.dumps({"query": "test", "results": [], "total_results": 0}).encode()
    proc = _mock_tinyfish(empty)
    with patch("genesis.web.search.asyncio.create_subprocess_exec", return_value=proc):
        resp = await searcher.search("test")
    assert resp.results == []
    assert resp.error is None
