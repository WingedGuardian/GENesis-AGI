"""Web intelligence MCP tools — external world discoverability.

Exposes Genesis's web infrastructure (TinyFish, Scrapling, Ladder, Crawl4AI,
SearXNG, Brave, Tavily, Exa, Perplexity) as MCP tools accessible from all
session types.

Smart fallback chains — callers say what they want, the tool figures out how.
Parallel to code intelligence tools (CBM, Serena, GitNexus) for internal
discoverability.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

# Lazy singletons — avoids import-time overhead for Playwright/httpx
_fetcher = None
_searcher = None


def _get_fetcher():
    global _fetcher
    if _fetcher is None:
        from genesis.web.fetch import WebFetcher

        _fetcher = WebFetcher()
    return _fetcher


def _get_searcher():
    global _searcher
    if _searcher is None:
        from genesis.web.search import WebSearcher

        _searcher = WebSearcher()
    return _searcher


def _is_challenge_response(text: str, status_code: int) -> bool:
    """Detect anti-bot challenge responses that need JS rendering.

    Intentional trade-off: only checks markers in short pages (<500 chars).
    Most Cloudflare challenges return 403/503 (caught by status check).
    Rare 200+challenge pages that exceed 500 chars will not trigger escalation
    — acceptable because large pages with challenge markers mixed into real
    content would cause false positives on legitimate pages.
    """
    if status_code in (403, 429, 503):
        return True
    if not text or len(text) < 500:
        lower = text.lower() if text else ""
        challenge_markers = ("captcha", "cloudflare", "challenge", "verify you are human")
        return any(m in lower for m in challenge_markers)
    return False


async def _try_tinyfish_fetch(url: str, max_chars: int) -> dict | None:
    """Attempt TinyFish fetch. Returns result dict or None on failure."""
    import os

    if not os.environ.get("API_KEY_TINYFISH"):
        return None
    try:
        from genesis.providers import tinyfish_client

        response = await tinyfish_client.fetch([url])
        results = response.get("results", [])
        if results:
            item = results[0]
            text = item.get("text", "")
            content = text[:max_chars]
            return {
                "url": item.get("url", url),
                "title": item.get("title", ""),
                "content": content,
                "backend_used": "tinyfish",
                "status_code": 200,
                "truncated": len(text) > max_chars,
                "error": None,
                "latency_ms": round(item.get("latency_ms", 0), 1),
            }
        logger.debug("TinyFish returned empty results for %s", url)
    except Exception as exc:
        logger.debug("TinyFish fetch failed for %s: %s", url, exc)
    return None


async def _try_tinyfish_search(query: str, max_results: int) -> dict | None:
    """Attempt TinyFish search. Returns result dict or None on failure."""
    import os

    if not os.environ.get("API_KEY_TINYFISH"):
        return None
    try:
        from genesis.providers import tinyfish_client

        response = await tinyfish_client.search(query)
        raw_results = response.get("results", [])[:max_results]
        results = [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("snippet", ""),
                "score": max(0.0, 1.0 - (r.get("position", 1) - 1) * 0.1),
            }
            for r in raw_results
        ]
        return {
            "query": query,
            "results": results,
            "backend_used": "tinyfish",
            "fallback_used": False,
            "answer": None,
            "error": None,
        }
    except Exception as exc:
        logger.debug("TinyFish search failed: %s", exc)
    return None


async def _try_crawl4ai(url: str, max_chars: int) -> dict | None:
    """Attempt Crawl4AI fetch. Returns result dict or None on failure."""
    try:
        from crawl4ai import AsyncWebCrawler

        start = time.monotonic()
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=url)
        latency = (time.monotonic() - start) * 1000

        if result and result.markdown:
            content = result.markdown[:max_chars]
            return {
                "url": url,
                "title": result.metadata.get("title", "") if result.metadata else "",
                "content": content,
                "backend_used": "crawl4ai",
                "status_code": 200,
                "truncated": len(result.markdown) > max_chars,
                "error": None,
                "latency_ms": round(latency, 1),
            }
    except ImportError:
        logger.debug("Crawl4AI not available for fallback")
    except Exception as exc:
        logger.warning("Crawl4AI fallback failed for %s: %s", url, exc)
    return None


async def _try_ladder_fetch(url: str, max_chars: int) -> dict | None:
    """Attempt Ladder proxy fetch. Returns result dict or None on failure.

    Ladder impersonates Googlebot with per-domain rules for ~41 domains
    (major publications, Medium, etc.). Runs locally on port 8079.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            start = time.monotonic()
            resp = await client.get(f"http://localhost:8079/raw/{url}")
            latency = (time.monotonic() - start) * 1000
        if resp.status_code == 200 and len(resp.text.strip()) > 100:
            content = resp.text[:max_chars]
            return {
                "url": url,
                "title": "",
                "content": content,
                "backend_used": "ladder",
                "status_code": 200,
                "truncated": len(resp.text) > max_chars,
                "error": None,
                "latency_ms": round(latency, 1),
            }
    except httpx.ConnectError:
        pass  # Ladder not running — silent fallthrough
    except Exception as exc:
        logger.debug("Ladder fetch failed for %s: %s", url, exc)
    return None


async def _impl_web_fetch(
    url: str,
    backend: str = "auto",
    max_chars: int = 50000,
) -> dict:
    """Fetch a URL and return clean text content."""
    if not url or not url.strip():
        return {"error": "url is required"}

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    start = time.monotonic()
    fetcher = _get_fetcher()

    if backend == "auto":
        # Primary: TinyFish (free, server-side anti-bot, JS rendering)
        tf_result = await _try_tinyfish_fetch(url, max_chars)
        if tf_result:
            return tf_result

        # Fallback: Scrapling/httpx via WebFetcher
        result = await fetcher.fetch(url, max_chars=max_chars)
        latency = (time.monotonic() - start) * 1000

        # If challenge detected, escalate: Ladder (lightweight) → Crawl4AI (heavy)
        if _is_challenge_response(result.text, result.status_code):
            logger.info("Challenge detected for %s, trying Ladder proxy", url)
            ladder_result = await _try_ladder_fetch(url, max_chars)
            if ladder_result:
                return ladder_result
            logger.info("Ladder unavailable/failed for %s, escalating to Crawl4AI", url)
            crawl_result = await _try_crawl4ai(url, max_chars)
            if crawl_result:
                return crawl_result

        # Return WebFetcher result (even if partial)
        return {
            "url": result.url,
            "title": result.title,
            "content": result.text,
            "backend_used": "scrapling" if not result.error else "httpx",
            "status_code": result.status_code,
            "truncated": result.truncated,
            "error": result.error,
            "latency_ms": round(latency, 1),
        }

    elif backend == "tinyfish":
        tf_result = await _try_tinyfish_fetch(url, max_chars)
        if tf_result:
            return tf_result
        return {"url": url, "error": "TinyFish fetch failed or unavailable", "backend_used": "tinyfish"}

    elif backend == "ladder":
        ladder_result = await _try_ladder_fetch(url, max_chars)
        if ladder_result:
            return ladder_result
        return {"url": url, "error": "Ladder proxy failed or unavailable", "backend_used": "ladder"}

    elif backend == "crawl4ai":
        crawl_result = await _try_crawl4ai(url, max_chars)
        if crawl_result:
            return crawl_result
        return {"url": url, "error": "Crawl4AI failed or unavailable", "backend_used": "crawl4ai"}

    elif backend in ("scrapling", "httpx"):
        # Force WebFetcher (which uses scrapling if available, else httpx)
        result = await fetcher.fetch(url, max_chars=max_chars)
        latency = (time.monotonic() - start) * 1000
        return {
            "url": result.url,
            "title": result.title,
            "content": result.text,
            "backend_used": backend,
            "status_code": result.status_code,
            "truncated": result.truncated,
            "error": result.error,
            "latency_ms": round(latency, 1),
        }

    else:
        return {"error": f"Unknown backend '{backend}'. Use: auto, tinyfish, ladder, scrapling, crawl4ai, httpx"}


async def _impl_web_fetch_multi(
    urls: list[str],
    max_chars: int = 50000,
) -> dict:
    """Fetch multiple URLs in parallel via TinyFish."""
    import os

    if not os.environ.get("API_KEY_TINYFISH"):
        return {"error": "Multi-URL fetch requires API_KEY_TINYFISH"}

    clean_urls = []
    for u in urls[:10]:
        u = u.strip()
        if not u.startswith(("http://", "https://")):
            u = "https://" + u
        clean_urls.append(u)

    start = time.monotonic()
    try:
        from genesis.providers import tinyfish_client

        response = await tinyfish_client.fetch(clean_urls)
        for item in response.get("results", []):
            text = item.get("text", "")
            if len(text) > max_chars:
                item["text"] = text[:max_chars]
                item["truncated"] = True
            else:
                item["truncated"] = False

        latency = round((time.monotonic() - start) * 1000, 1)
        return {
            "results": response.get("results", []),
            "errors": response.get("errors", []),
            "backend_used": "tinyfish",
            "latency_ms": latency,
        }
    except Exception as exc:
        latency = round((time.monotonic() - start) * 1000, 1)
        return {"error": f"TinyFish multi-fetch failed: {exc}", "backend_used": "tinyfish", "latency_ms": latency}


async def _impl_web_search(
    query: str,
    backend: str = "auto",
    max_results: int = 10,
) -> dict:
    """Search the web and return structured results."""
    if not query or not query.strip():
        return {"error": "query is required"}

    query = query.strip()
    max_results = min(max(1, max_results), 20)
    start = time.monotonic()

    if backend == "auto":
        # Primary: TinyFish (free, faster, better quality)
        tf_result = await _try_tinyfish_search(query, max_results)
        if tf_result:
            latency = (time.monotonic() - start) * 1000
            tf_result["latency_ms"] = round(latency, 1)
            return tf_result

        # Fallback: SearXNG → Brave
        searcher = _get_searcher()
        response = await searcher.search(query, max_results=max_results)
        latency = (time.monotonic() - start) * 1000

        results = [
            {"title": r.title, "url": r.url, "snippet": r.snippet, "score": r.score}
            for r in response.results
        ]
        return {
            "query": response.query,
            "results": results,
            "backend_used": response.backend_used.value if response.backend_used else "unknown",
            "fallback_used": True,
            "answer": None,
            "error": response.error,
            "latency_ms": round(latency, 1),
        }

    elif backend in ("searxng", "brave"):
        # Explicit SearXNG/Brave selection
        searcher = _get_searcher()
        response = await searcher.search(query, max_results=max_results)
        latency = (time.monotonic() - start) * 1000

        results = [
            {"title": r.title, "url": r.url, "snippet": r.snippet, "score": r.score}
            for r in response.results
        ]
        return {
            "query": response.query,
            "results": results,
            "backend_used": response.backend_used.value if response.backend_used else "unknown",
            "fallback_used": response.fallback_used,
            "answer": None,
            "error": response.error,
            "latency_ms": round(latency, 1),
        }

    elif backend == "tinyfish":
        tf_result = await _try_tinyfish_search(query, max_results)
        if tf_result:
            latency = (time.monotonic() - start) * 1000
            tf_result["latency_ms"] = round(latency, 1)
            return tf_result
        return {"query": query, "error": "TinyFish search failed or unavailable", "backend_used": "tinyfish"}

    elif backend == "tavily":
        try:
            from genesis.providers.tavily_adapter import TavilyAdapter

            adapter = TavilyAdapter()
            result = await adapter.invoke({
                "query": query,
                "max_results": max_results,
                "include_answer": True,
            })
            latency = (time.monotonic() - start) * 1000

            if not result.success:
                return {"query": query, "error": result.error or "Tavily search failed", "backend_used": "tavily"}

            data = result.data or {}
            results = [
                {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", ""), "score": r.get("score", 0)}
                for r in data.get("results", [])
            ]
            return {
                "query": query,
                "results": results,
                "backend_used": "tavily",
                "fallback_used": False,
                "answer": data.get("answer"),
                "error": None,
                "latency_ms": round(latency, 1),
            }
        except (ImportError, ValueError) as exc:
            return {"query": query, "error": f"Tavily unavailable: {exc}", "backend_used": "tavily"}

    elif backend == "exa":
        try:
            from genesis.providers.exa_adapter import ExaAdapter

            adapter = ExaAdapter()
            result = await adapter.invoke({
                "query": query,
                "num_results": max_results,
            })
            latency = (time.monotonic() - start) * 1000

            if not result.success:
                return {"query": query, "error": result.error or "Exa search failed", "backend_used": "exa"}

            data = result.data or {}
            results = [
                {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("text", "")[:300], "score": r.get("score", 0)}
                for r in data.get("results", [])
            ]
            return {
                "query": query,
                "results": results,
                "backend_used": "exa",
                "fallback_used": False,
                "answer": None,
                "error": None,
                "latency_ms": round(latency, 1),
            }
        except (ImportError, ValueError) as exc:
            return {"query": query, "error": f"Exa unavailable: {exc}", "backend_used": "exa"}

    elif backend == "perplexity":
        try:
            from genesis.research.perplexity import PerplexityAdapter

            adapter = PerplexityAdapter()
            result = await adapter.invoke({"query": query})
            latency = (time.monotonic() - start) * 1000

            if not result.success:
                return {"query": query, "error": result.error or "Perplexity failed", "backend_used": "perplexity"}

            return {
                "query": query,
                "results": [],
                "backend_used": "perplexity",
                "fallback_used": False,
                "answer": result.data if isinstance(result.data, str) else str(result.data),
                "error": None,
                "latency_ms": round(latency, 1),
            }
        except (ImportError, ValueError) as exc:
            return {"query": query, "error": f"Perplexity unavailable: {exc}", "backend_used": "perplexity"}

    else:
        return {"error": f"Unknown backend '{backend}'. Use: auto, tinyfish, searxng, brave, tavily, exa, perplexity"}


@mcp.tool()
async def web_fetch(
    url: str = "",
    urls: list[str] | None = None,
    backend: str = "auto",
    max_chars: int = 50000,
) -> dict:
    """Fetch URL(s) and return clean text content.

    Smart fallback chain: TinyFish (anti-bot, JS rendering) → Scrapling
    (TLS impersonation) → Ladder (Googlebot proxy) → Crawl4AI (local
    Playwright) → httpx (plain).

    Args:
        url: Single URL to fetch.
        urls: Multiple URLs (1-10) for parallel fetch via TinyFish.
        backend: "auto" (smart fallback), "tinyfish" (cloud anti-bot),
                 "ladder" (Googlebot proxy, paywalls), "scrapling" (fast, TLS),
                 "crawl4ai" (JS-rendered), "httpx".
        max_chars: Maximum characters per URL (default 50000 ≈ 12k tokens).

    Returns dict with: url, title, content, backend_used, status_code,
    truncated, error, latency_ms. For multi-URL: results[] array.

    Use this instead of CC WebFetch for:
    - Anti-bot protected sites (TinyFish's server-side bypass)
    - JS-heavy SPAs (TinyFish or Crawl4AI rendering)
    - Parallel multi-URL fetching (urls parameter)
    - Background sessions (no Bash available)

    Use CC WebFetch when you specifically need AI-processed summaries.
    Use browser_navigate when you need to interact with the page.
    """
    if urls:
        return await _impl_web_fetch_multi(urls, max_chars)
    return await _impl_web_fetch(url, backend, max_chars)


@mcp.tool()
async def web_search(
    query: str,
    backend: str = "auto",
    max_results: int = 10,
) -> dict:
    """Search the web and return structured results.

    Smart fallback chain: TinyFish (fast, free) → SearXNG (self-hosted) → Brave.
    Paid backends (tavily, exa, perplexity) available via explicit backend param.

    Args:
        query: Search query string. Supports site: filters with SearXNG.
        backend: "auto" (TinyFish→SearXNG→Brave), "tinyfish", "searxng",
                 "brave", "tavily", "exa", or "perplexity".
        max_results: Maximum results (default 10, max 20).

    Returns dict with: query, results (list of title/url/snippet/score),
    backend_used, fallback_used, answer (for tavily/perplexity), error, latency_ms.

    Use this instead of CC WebSearch for:
    - Structured JSON results
    - Background sessions (no Bash available)
    - Agent pipelines needing structured data

    Use CC WebSearch for quick general lookups in foreground sessions.
    Use "perplexity" backend when you need a synthesized multi-source answer.
    Use "exa" backend for conceptual/semantic discovery.
    """
    return await _impl_web_search(query, backend, max_results)


@mcp.tool()
async def web_agent(
    url: str,
    goal: str,
    output_schema: dict | None = None,
    browser_profile: str = "stealth",
    max_steps: int = 100,
) -> dict:
    """Run a goal-based browser automation and return structured results.

    Uses TinyFish's AI agent to achieve a natural language goal on a web page.
    The agent navigates, clicks, fills forms, and extracts data autonomously.

    PAID: ~$0.015 per step. Budget-checked before execution.

    Args:
        url: Target URL to automate.
        goal: Natural language description of what to achieve. Include the
              desired JSON structure for extraction tasks.
        output_schema: Optional JSON Schema for structured output validation.
        browser_profile: "stealth" (anti-bot) or "lite" (fast).
        max_steps: Maximum agent steps, 1-500 (default 100).

    Returns dict with: run_id, status, result, num_of_steps, cost_usd,
    latency_ms, error.

    Use this when:
    - Local Camoufox can't pass anti-bot detection
    - You need structured data extraction from complex pages
    - Step-by-step browser_navigate would be too many tool calls

    Don't use for simple page reads — use web_fetch instead.
    """
    import os

    if not os.environ.get("API_KEY_TINYFISH"):
        return {"error": "web_agent requires API_KEY_TINYFISH"}

    # Budget check before execution — agent calls cost $0.015/step
    try:
        import aiosqlite

        from genesis.env import genesis_db_path
        from genesis.routing.cost_tracker import CostTracker

        async with aiosqlite.connect(genesis_db_path()) as db:
            tracker = CostTracker(db)
            status = await tracker.check_budget()
            if hasattr(status, "value"):
                status = status.value
            if status == "EXCEEDED":
                return {"error": "Daily budget exceeded. web_agent costs ~$0.015/step."}
    except Exception as exc:
        logger.debug("Budget check skipped: %s", exc)

    start = time.monotonic()
    try:
        from genesis.providers.tinyfish_agent import COST_PER_STEP_USD, TinyFishAgentAdapter

        adapter = TinyFishAgentAdapter()
        result = await adapter.invoke({
            "url": url,
            "goal": goal,
            "output_schema": output_schema,
            "browser_profile": browser_profile,
            "max_steps": max_steps,
        })
        latency = round((time.monotonic() - start) * 1000, 1)

        if result.success:
            data = result.data or {}
            # Record cost
            try:
                import uuid
                from datetime import datetime

                import aiosqlite

                from genesis.db.crud import cost_events
                from genesis.env import genesis_db_path

                num_steps = data.get("num_of_steps", 0)
                cost_usd = round(num_steps * COST_PER_STEP_USD, 4)
                async with aiosqlite.connect(genesis_db_path()) as db:
                    await cost_events.create(
                        db,
                        id=str(uuid.uuid4()),
                        event_type="tinyfish_agent",
                        provider="tinyfish",
                        cost_usd=cost_usd,
                        cost_known=True,
                        metadata={"run_id": data.get("run_id"), "goal": goal, "url": url, "steps": num_steps},
                        created_at=datetime.now(UTC).isoformat(),
                    )
            except Exception as exc:
                logger.warning("Failed to record TinyFish agent cost: %s", exc)

            return {
                "run_id": data.get("run_id"),
                "status": data.get("status"),
                "result": data.get("result"),
                "num_of_steps": data.get("num_of_steps", 0),
                "cost_usd": data.get("cost_usd", 0),
                "error": data.get("error"),
                "latency_ms": latency,
            }
        else:
            return {"error": result.error or "TinyFish agent failed", "latency_ms": latency}

    except Exception as exc:
        latency = round((time.monotonic() - start) * 1000, 1)
        return {"error": f"web_agent failed: {exc}", "latency_ms": latency}
