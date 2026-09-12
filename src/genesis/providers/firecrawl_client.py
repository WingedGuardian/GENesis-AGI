"""Async HTTP client for the Firecrawl API (scrape + search).

PAID escalation backend — never part of the web_fetch/web_search auto chains
(every call burns account credits). Reachable as ``backend="firecrawl"`` on
the web tools, which makes it available to Bash-less background sessions
(the Firecrawl CLI plugin is foreground-Bash-only and there is NO Firecrawl
MCP server).

Auth: ``Authorization: Bearer $FIRECRAWL_API_KEY``. Lazy singleton httpx
client for connection pooling (mirrors tinyfish_client).

API endpoints (v2):
  Scrape: POST https://api.firecrawl.dev/v2/scrape  {url, formats}
  Search: POST https://api.firecrawl.dev/v2/search  {query, limit}
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"
_SEARCH_URL = "https://api.firecrawl.dev/v2/search"

_client: httpx.AsyncClient | None = None


def _get_key() -> str:
    key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not key:
        raise ValueError("FIRECRAWL_API_KEY required")
    return key


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_get_key()}",
        "Content-Type": "application/json",
    }


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # Scrapes render JS server-side; generous but bounded.
        _client = httpx.AsyncClient(timeout=60.0)
    return _client


async def scrape(url: str) -> dict[str, Any]:
    """Scrape one URL to markdown. Returns the API's ``data`` object.

    Raises on HTTP/transport errors; returns ``{}`` when the API reports
    a non-success payload (caller degrades to its own fallback).
    """
    client = _get_client()
    resp = await client.post(
        _SCRAPE_URL,
        headers=_headers(),
        json={"url": url, "formats": ["markdown"]},
    )
    resp.raise_for_status()
    body = resp.json()
    if not isinstance(body, dict) or not body.get("success"):
        logger.debug("Firecrawl scrape non-success for %s: %s", url, body)
        return {}
    data = body.get("data")
    return data if isinstance(data, dict) else {}


async def search(query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """Search the web. Returns a list of {title, url, description} dicts.

    Raises on HTTP/transport errors; returns ``[]`` on non-success payloads.
    """
    client = _get_client()
    resp = await client.post(
        _SEARCH_URL,
        headers=_headers(),
        json={"query": query, "limit": max(1, min(limit, 20))},
    )
    resp.raise_for_status()
    body = resp.json()
    if not isinstance(body, dict) or not body.get("success"):
        logger.debug("Firecrawl search non-success for %r: %s", query, body)
        return []
    data = body.get("data")
    # v2 returns {"web": [...]} (plus optional news/images); older shapes
    # returned a bare list — accept both defensively.
    if isinstance(data, dict):
        results = data.get("web", [])
    elif isinstance(data, list):
        results = data
    else:
        results = []
    return [r for r in results if isinstance(r, dict)]
