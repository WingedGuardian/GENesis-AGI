"""Voyage AI rerank-2.5 — post-retrieval cross-encoder reranker.

Reranks memory candidates after RRF fusion using Voyage's cross-encoder
model, which scores (query, document) pairs by semantic relevance.
Operates on raw text — no embedding compatibility needed.

Graceful degradation: if the API is unavailable, unconfigured, or errors,
returns an empty list and the caller keeps original RRF scores.
"""

from __future__ import annotations

import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

_VOYAGE_RERANK_URL = "https://api.voyageai.com/v1/rerank"
_DEFAULT_MODEL = "rerank-2.5"


class VoyageReranker:
    """Post-retrieval reranker using Voyage AI rerank-2.5."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = _DEFAULT_MODEL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("API_KEY_VOYAGE")
        self._model = model
        self._client = client or httpx.AsyncClient(timeout=10.0)

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def rerank(
        self,
        query: str,
        documents: list[dict[str, str]],
        *,
        top_k: int = 10,
    ) -> list[dict]:
        """Rerank documents by query relevance.

        Parameters
        ----------
        query:
            The search query.
        documents:
            List of ``{"id": memory_id, "text": content}`` dicts.
        top_k:
            Max results to return.

        Returns
        -------
        Sorted list of ``{"id": memory_id, "score": float}`` or
        empty list on any failure (caller should keep original order).
        """
        if not self._api_key or not documents:
            return []

        # Extract text for the API; preserve ID mapping by index
        texts = [d["text"] for d in documents]
        id_by_index = {i: d["id"] for i, d in enumerate(documents)}

        t0 = time.monotonic()
        try:
            resp = await self._client.post(
                _VOYAGE_RERANK_URL,
                json={
                    "model": self._model,
                    "query": query,
                    "documents": texts,
                    "top_k": top_k,
                },
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            logger.warning(
                "Voyage rerank HTTP %d — degrading to RRF order",
                e.response.status_code,
            )
            return []
        except Exception:
            logger.warning(
                "Voyage rerank unavailable — degrading to RRF order",
                exc_info=True,
            )
            return []

        latency_ms = (time.monotonic() - t0) * 1000
        usage = data.get("usage", {})
        logger.debug(
            "Voyage rerank: %d docs → %d results in %.0fms (%d tokens)",
            len(documents),
            len(data.get("data", [])),
            latency_ms,
            usage.get("total_tokens", 0),
        )

        return [
            {"id": id_by_index[item["index"]], "score": item["relevance_score"]}
            for item in data.get("data", [])
            if item["index"] in id_by_index
        ]
