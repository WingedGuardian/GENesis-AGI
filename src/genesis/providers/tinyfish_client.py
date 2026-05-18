"""Async HTTP client for TinyFish APIs (search, fetch, agent, browser).

Shared by all TinyFish adapters. Lazy singleton httpx clients for
connection pooling. Auth: X-API-Key header. No CLI subprocess overhead.

API endpoints:
  Search: GET  https://api.search.tinyfish.ai/?query=...
  Fetch:  POST https://api.fetch.tinyfish.ai/  {urls, format}
  Agent:  POST https://agent.tinyfish.ai/v1/automation/run  {url, goal}
  Browser: POST https://api.browser.tinyfish.ai/  {url}
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.search.tinyfish.ai/"
_FETCH_URL = "https://api.fetch.tinyfish.ai/"
_AGENT_URL = "https://agent.tinyfish.ai/v1/automation/run"
_BROWSER_URL = "https://api.browser.tinyfish.ai/"

# Lazy singleton clients — reused across calls for connection pooling
_search_client: httpx.AsyncClient | None = None
_fetch_client: httpx.AsyncClient | None = None
_agent_client: httpx.AsyncClient | None = None
_browser_client: httpx.AsyncClient | None = None


def _get_key() -> str:
    key = os.environ.get("API_KEY_TINYFISH", "")
    if not key:
        raise ValueError("API_KEY_TINYFISH required")
    return key


def _headers() -> dict[str, str]:
    return {
        "X-API-Key": _get_key(),
        "Content-Type": "application/json",
    }


def _get_search_client() -> httpx.AsyncClient:
    global _search_client
    if _search_client is None:
        _search_client = httpx.AsyncClient(timeout=10.0)
    return _search_client


def _get_fetch_client() -> httpx.AsyncClient:
    global _fetch_client
    if _fetch_client is None:
        _fetch_client = httpx.AsyncClient(timeout=30.0)
    return _fetch_client


def _get_agent_client() -> httpx.AsyncClient:
    global _agent_client
    if _agent_client is None:
        _agent_client = httpx.AsyncClient(timeout=600.0)
    return _agent_client


def _get_browser_client() -> httpx.AsyncClient:
    global _browser_client
    if _browser_client is None:
        # Session creation takes 10-30s per TinyFish docs; 90s gives safe margin
        _browser_client = httpx.AsyncClient(timeout=90.0)
    return _browser_client


async def search(
    query: str,
    *,
    location: str | None = None,
    language: str | None = None,
) -> dict:
    """Web search. Free, no credits consumed."""
    params: dict[str, str] = {"query": query}
    if location:
        params["location"] = location
    if language:
        params["language"] = language

    client = _get_search_client()
    resp = await client.get(_SEARCH_URL, params=params, headers=_headers())
    resp.raise_for_status()
    return resp.json()


async def fetch(
    urls: list[str],
    *,
    fmt: str = "markdown",
    links: bool = False,
    image_links: bool = False,
) -> dict:
    """Fetch content from 1-10 URLs in parallel. Free, no credits consumed."""
    body: dict = {"urls": urls, "format": fmt}
    if links:
        body["links"] = True
    if image_links:
        body["image_links"] = True

    client = _get_fetch_client()
    resp = await client.post(_FETCH_URL, json=body, headers=_headers())
    resp.raise_for_status()
    return resp.json()


async def agent_run(
    url: str,
    goal: str,
    *,
    output_schema: dict | None = None,
    browser_profile: str = "stealth",
    max_steps: int = 100,
) -> dict:
    """Run NL browser automation. Paid: $0.015 per step."""
    body: dict = {
        "url": url,
        "goal": goal,
        "browser_profile": browser_profile,
        "agent_config": {"max_steps": min(max(1, max_steps), 500)},
    }
    if output_schema:
        body["output_schema"] = output_schema

    client = _get_agent_client()
    resp = await client.post(_AGENT_URL, json=body, headers=_headers())
    resp.raise_for_status()
    return resp.json()


async def browser_session_create(
    url: str | None = None,
    *,
    timeout_seconds: int | None = None,
) -> dict:
    """Create a TinyFish remote browser session.

    Paid: 1 credit per 4 minutes of session time (~$0.015/credit PAYG).
    60-minute hard cap per session. Failed sessions cost $0.

    Returns dict with:
        session_id: str — unique identifier (e.g. "br-a1b2c3d4-...")
        cdp_url: str — WebSocket URL for playwright.chromium.connect_over_cdp()
        base_url: str — HTTPS base for session-level REST calls
    """
    body: dict = {}
    if url:
        body["url"] = url
    if timeout_seconds is not None:
        body["timeout_seconds"] = max(5, min(timeout_seconds, 3600))

    client = _get_browser_client()
    resp = await client.post(_BROWSER_URL, json=body, headers=_headers())
    resp.raise_for_status()
    return resp.json()


async def browser_session_delete(session_id: str) -> None:
    """Terminate a TinyFish browser session. Idempotent.

    Always call in a finally block after CDP work to avoid credit burn
    from inactivity timeout.
    """
    client = _get_browser_client()
    resp = await client.delete(
        f"{_BROWSER_URL}{session_id}",
        headers=_headers(),
    )
    # 204 = deleted, 404 = already gone — both are fine
    if resp.status_code not in (204, 404):
        resp.raise_for_status()
