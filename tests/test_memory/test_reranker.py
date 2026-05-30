"""Tests for VoyageReranker — graceful degradation and score mapping."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from genesis.memory.reranker import VoyageReranker


def _mock_client(status_code: int = 200, body: dict | None = None) -> httpx.AsyncClient:
    """Build a mock httpx.AsyncClient that returns a fixed response."""
    response = httpx.Response(
        status_code=status_code,
        json=body or {},
        request=httpx.Request("POST", "https://api.voyageai.com/v1/rerank"),
    )
    client = AsyncMock(spec=httpx.AsyncClient)
    client.post = AsyncMock(return_value=response)
    return client


SAMPLE_DOCS = [
    {"id": "mem-1", "text": "Genesis uses RRF fusion for memory recall"},
    {"id": "mem-2", "text": "Cooking recipes for pasta dishes"},
    {"id": "mem-3", "text": "The ego system proposes actions for user approval"},
]

VOYAGE_SUCCESS_RESPONSE = {
    "data": [
        {"index": 0, "relevance_score": 0.85},
        {"index": 2, "relevance_score": 0.62},
    ],
    "usage": {"total_tokens": 45},
}


@pytest.mark.asyncio
async def test_rerank_returns_sorted_scores():
    client = _mock_client(200, VOYAGE_SUCCESS_RESPONSE)
    reranker = VoyageReranker(api_key="test-key", client=client)

    results = await reranker.rerank("memory recall system", SAMPLE_DOCS, top_k=2)

    assert len(results) == 2
    assert results[0] == {"id": "mem-1", "score": 0.85}
    assert results[1] == {"id": "mem-3", "score": 0.62}


@pytest.mark.asyncio
async def test_rerank_passes_correct_payload():
    client = _mock_client(200, VOYAGE_SUCCESS_RESPONSE)
    reranker = VoyageReranker(api_key="test-key", client=client)

    await reranker.rerank("test query", SAMPLE_DOCS, top_k=2)

    call_kwargs = client.post.call_args
    payload = call_kwargs.kwargs["json"]
    assert payload["model"] == "rerank-2.5"
    assert payload["query"] == "test query"
    assert payload["documents"] == [d["text"] for d in SAMPLE_DOCS]
    assert payload["top_k"] == 2
    assert "Bearer test-key" in call_kwargs.kwargs["headers"]["Authorization"]


@pytest.mark.asyncio
async def test_rerank_degrades_on_http_error():
    client = _mock_client(429, {"error": "rate limited"})
    reranker = VoyageReranker(api_key="test-key", client=client)

    results = await reranker.rerank("test", SAMPLE_DOCS)

    assert results == []


@pytest.mark.asyncio
async def test_rerank_degrades_on_network_error():
    client = AsyncMock(spec=httpx.AsyncClient)
    client.post = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
    reranker = VoyageReranker(api_key="test-key", client=client)

    results = await reranker.rerank("test", SAMPLE_DOCS)

    assert results == []


@pytest.mark.asyncio
async def test_rerank_disabled_without_key():
    reranker = VoyageReranker(api_key=None)

    assert reranker.enabled is False
    results = await reranker.rerank("test", SAMPLE_DOCS)
    assert results == []


@pytest.mark.asyncio
async def test_rerank_empty_documents():
    reranker = VoyageReranker(api_key="test-key")

    results = await reranker.rerank("test", [])
    assert results == []
