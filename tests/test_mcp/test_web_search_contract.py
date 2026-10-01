"""web_search failure contract (PR #2496 review round 1).

- ``backend_used`` names a backend only when it produced results; a failed search
  of ANY backend reports None and says what was tried in ``backend_tried``.
- The auto chain treats an empty TinyFish result as a miss and falls through,
  and when every backend fails its error names TinyFish's reason too.
- Outward error text carries exception TYPES, never exception messages, which
  can embed a configured service URL.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from genesis.mcp.health import web_tools
from genesis.web.types import SearchBackend, SearchResponse, SearchResult

_SECRET_URL = "http://search.internal.example:55510/search"


def _response(results=(), error=None, backend=None):
    return SearchResponse(query="q", results=list(results), backend_used=backend, error=error)


async def test_auto_falls_through_when_tinyfish_returns_nothing(monkeypatch):
    monkeypatch.setenv("API_KEY_TINYFISH", "k")
    searcher = AsyncMock()
    searcher.search.return_value = _response(
        [SearchResult(title="t", url="u", snippet="s", backend=SearchBackend.SEARXNG)],
        backend=SearchBackend.SEARXNG,
    )
    with (
        patch("genesis.providers.tinyfish_client.search", AsyncMock(return_value={"results": []})),
        patch("genesis.mcp.health.web_tools._get_searcher", return_value=searcher),
    ):
        out = await web_tools._impl_web_search("q", backend="auto")
    assert out["backend_used"] == "searxng"
    assert out["results"]


async def test_auto_total_failure_names_tinyfish_too(monkeypatch):
    monkeypatch.setenv("API_KEY_TINYFISH", "k")
    searcher = AsyncMock()
    searcher.search.return_value = _response(
        error="All search backends failed — searxng: ConnectError"
    )
    with (
        patch(
            "genesis.providers.tinyfish_client.search",
            AsyncMock(side_effect=httpx.ConnectError("refused " + _SECRET_URL)),
        ),
        patch("genesis.mcp.health.web_tools._get_searcher", return_value=searcher),
    ):
        out = await web_tools._impl_web_search("q", backend="auto")
    assert out["backend_used"] is None
    assert "tinyfish: ConnectError" in out["error"]
    assert "internal.example" not in out["error"]


@pytest.mark.parametrize("backend", ["tinyfish", "firecrawl", "tavily", "exa", "perplexity"])
async def test_a_failed_explicit_search_names_no_backend_used(monkeypatch, backend):
    for key in (
        "API_KEY_TINYFISH",
        "FIRECRAWL_API_KEY",
        "API_KEY_TAVILY",
        "API_KEY_EXA",
        "API_KEY_PERPLEXITY",
    ):
        monkeypatch.delenv(key, raising=False)
    with (
        patch("genesis.mcp.health.web_tools._try_firecrawl_search", AsyncMock(return_value=None)),
        patch(
            "genesis.providers.tavily_adapter.TavilyAdapter", side_effect=ValueError(_SECRET_URL)
        ),
        patch("genesis.providers.exa_adapter.ExaAdapter", side_effect=ValueError(_SECRET_URL)),
        patch(
            "genesis.research.perplexity.PerplexityAdapter",
            side_effect=ValueError(_SECRET_URL),
        ),
    ):
        out = await web_tools._impl_web_search("q", backend=backend)
    assert out.get("error")
    assert out["backend_used"] is None
    assert out["backend_tried"] == backend
    assert "internal.example" not in out["error"]


@pytest.mark.parametrize(
    ("backend", "target"),
    [
        ("tavily", "genesis.providers.tavily_adapter.TavilyAdapter"),
        ("exa", "genesis.providers.exa_adapter.ExaAdapter"),
        ("perplexity", "genesis.research.perplexity.PerplexityAdapter"),
    ],
)
async def test_a_runtime_adapter_failure_keeps_its_message_out(backend, target):
    """The adapters put str(exc) in result.error on a runtime failure. web_search
    must not hand that text to its caller; it goes to the log instead."""
    from unittest.mock import MagicMock

    failed = MagicMock(success=False, error="ConnectError: refused " + _SECRET_URL, data=None)
    adapter = MagicMock()
    adapter.invoke = AsyncMock(return_value=failed)
    with patch(target, return_value=adapter):
        out = await web_tools._impl_web_search("q", backend=backend)
    assert out["backend_used"] is None
    assert "internal.example" not in out["error"]


_KEYS = {
    "tinyfish": "API_KEY_TINYFISH",
    "firecrawl": "FIRECRAWL_API_KEY",
    "tavily": "API_KEY_TAVILY",
    "exa": "API_KEY_EXA",
    "perplexity": "API_KEY_PERPLEXITY",
}


@pytest.mark.parametrize("backend", sorted(_KEYS))
async def test_a_missing_key_is_named_in_the_error(monkeypatch, backend):
    """A caller can fix configuration only if the error says which key is missing.
    The reason is classified from the environment, never from exception text."""
    for key in _KEYS.values():
        monkeypatch.delenv(key, raising=False)
    with patch("genesis.mcp.health.web_tools._try_firecrawl_search", AsyncMock(return_value=None)):
        out = await web_tools._impl_web_search("q", backend=backend)
    assert out["error"] == f"{_KEYS[backend]} is not set"
    assert out["backend_used"] is None
    assert out["backend_tried"] == backend
