"""Async HTTP client for TinyFish APIs (search, fetch, agent, browser).

Shared by all TinyFish adapters. Uses httpx with connection pooling.
Auth: X-API-Key header. No CLI subprocess overhead.

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

    async with httpx.AsyncClient(timeout=10.0) as client:
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

    async with httpx.AsyncClient(timeout=30.0) as client:
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

    # Agent calls can be slow (30s-5min)
    async with httpx.AsyncClient(timeout=600.0) as client:
        resp = await client.post(_AGENT_URL, json=body, headers=_headers())
        resp.raise_for_status()
        return resp.json()
