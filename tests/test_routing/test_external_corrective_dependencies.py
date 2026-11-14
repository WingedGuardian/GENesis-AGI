"""The restricted loader must retain dependencies of admitted corrective recall."""

import importlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest


@pytest.fixture
def loaded(monkeypatch, tmp_path):
    from genesis.routing import standalone
    from scripts.genesis_mcp_server import _external_file_keys

    monkeypatch.setattr(os, "environ", {
        "HOME": str(tmp_path), "GENESIS_HOME": str(tmp_path / "genesis"),
        "GENESIS_REPO_ROOT": str(Path(__file__).resolve().parents[2]),
    })
    keys = _external_file_keys("memory")
    monkeypatch.setattr(standalone, "_external_secret_keys", None)
    standalone.configure_external_secrets(keys)
    path = tmp_path / "selected.env"
    monkeypatch.setattr("genesis.env.secrets_path", lambda: path)
    monkeypatch.setattr("genesis.routing.config.load_config", Mock(side_effect=RuntimeError("owned stop after loader")))
    return standalone, path


@pytest.mark.parametrize("profile", ["external", "validator"])
@pytest.mark.parametrize("key", ["API_KEY_TINYFISH", "API_KEY_BRAVE"])
async def test_admitted_corrective_recall_keeps_automatic_search_credentials(loaded, monkeypatch, profile, key):
    from genesis.mcp.external_profiles import ExternalProfileMiddleware, current_external_profile
    from genesis.mcp.health import web_tools
    from genesis.memory import corrective, graph_expansion
    from genesis.providers import tinyfish_client
    from genesis.web.search import WebSearcher
    from genesis.web.types import SearchBackend, SearchResult

    standalone, path = loaded
    path.write_text(f"{key}=synthetic-noncredential\n")
    assert standalone._build_standalone_router() is None
    monkeypatch.setattr(corrective, "_grade_results", AsyncMock(return_value=[0.0]))
    monkeypatch.setattr(corrective, "_log", AsyncMock())
    monkeypatch.setattr(graph_expansion, "reranker_enabled", lambda: False)
    tiny_search = AsyncMock(return_value={"results": [{"title": "fixture", "url": "https://example.invalid/fixture", "snippet": "synthetic correction"}]})
    monkeypatch.setattr(tinyfish_client, "search", tiny_search)
    monkeypatch.setattr(tinyfish_client, "fetch", AsyncMock(return_value={"results": [{"url": "https://example.invalid/fixture", "text": "synthetic correction"}]}))
    searcher = object.__new__(WebSearcher)
    searcher._max_results = 10
    searcher._event_bus = None
    searcher._search_searxng = AsyncMock(side_effect=httpx.ConnectError("owned offline fixture"))
    brave_search = AsyncMock(return_value=[SearchResult(title="fixture", url="https://example.invalid/fixture", snippet="synthetic correction", backend=SearchBackend.BRAVE, score=1.0)])
    searcher._search_brave = brave_search
    monkeypatch.setattr(web_tools, "_get_searcher", lambda: searcher)
    monkeypatch.setattr(web_tools, "_get_fetcher", lambda: SimpleNamespace(fetch=AsyncMock(return_value=SimpleNamespace(url="https://example.invalid/fixture", title="fixture", text="synthetic correction", status_code=200, client="fixture", truncated=False, error=None))))

    async def admitted(context):
        assert current_external_profile.get() == profile
        return await corrective.maybe_correct_recall(
            query="synthetic query", results=[{"unit_id": "old", "content": "unhelpful", "score": 0.1}],
            retriever=SimpleNamespace(recall=AsyncMock(return_value=[])), db=None, path="knowledge", router=object(),
        )

    result = await ExternalProfileMiddleware("memory", profile).on_call_tool(
        SimpleNamespace(message=SimpleNamespace(name="knowledge_recall")), admitted,
    )
    assert any(item.get("origin") == "web" for item in result)
    assert tiny_search.await_count + brave_search.await_count == 1
    assert current_external_profile.get() is None


def test_lazy_search_constructor_keeps_selected_file_endpoints(loaded, monkeypatch):
    from genesis.mcp.health import web_tools

    standalone, path = loaded
    path.write_text("SEARXNG_URL=https://searxng.example.invalid/search\nBRAVE_API_URL=https://brave.example.invalid/search\n")
    assert standalone._build_standalone_router() is None
    # Exercise a fresh first import without leaving its import-time defaults
    # in another test's cached module/package binding.
    import genesis.web

    previous = getattr(genesis.web, "search", None)
    previous_module = sys.modules.pop("genesis.web.search", None)
    module = importlib.import_module("genesis.web.search")
    monkeypatch.setattr(httpx, "AsyncClient", Mock(return_value=None))
    monkeypatch.setattr(web_tools, "_searcher", None)
    try:
        actual = web_tools._get_searcher()
        assert actual._searxng_url == "https://searxng.example.invalid/search"
        assert actual._brave_url == "https://brave.example.invalid/search"
        assert sys.modules["genesis.web.search"] is module
    finally:
        if previous_module is None:
            sys.modules.pop("genesis.web.search", None)
        else:
            sys.modules["genesis.web.search"] = previous_module
        if previous is None:
            delattr(genesis.web, "search")
        else:
            genesis.web.search = previous
