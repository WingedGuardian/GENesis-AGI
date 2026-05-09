"""Web intelligence MCP tools — external world discoverability.

Exposes Genesis's web infrastructure (Scrapling, Crawl4AI, Tinyfish, Brave,
Tavily, Exa, Perplexity) as MCP tools accessible from all session types.

Smart fallback chains — callers say what they want, the tool figures out how.
Parallel to code intelligence tools (CBM, Serena, GitNexus) for internal
discoverability.
"""

from __future__ import annotations

import logging
import time

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
        # Primary: Scrapling/httpx via WebFetcher
        result = await fetcher.fetch(url, max_chars=max_chars)
        latency = (time.monotonic() - start) * 1000

        # If challenge detected, escalate to Crawl4AI
        if _is_challenge_response(result.text, result.status_code):
            logger.info("Challenge detected for %s, escalating to Crawl4AI", url)
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
        return {"error": f"Unknown backend '{backend}'. Use: auto, scrapling, crawl4ai, httpx"}


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

    if backend in ("auto", "tinyfish", "searxng", "brave"):
        # Use WebSearcher (Tinyfish primary, Brave fallback)
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
        return {"error": f"Unknown backend '{backend}'. Use: auto, tinyfish, brave, tavily, exa, perplexity"}


@mcp.tool()
async def web_fetch(
    url: str,
    backend: str = "auto",
    max_chars: int = 50000,
) -> dict:
    """Fetch a URL and return clean text content.

    Smart fallback chain: Scrapling (TLS impersonation, fast) → Crawl4AI
    (JS rendering, handles SPAs) → httpx (plain, last resort). The tool
    decides the best backend unless overridden.

    Args:
        url: The URL to fetch.
        backend: "auto" (smart fallback), "scrapling" (fast, anti-bot TLS),
                 "crawl4ai" (JS-rendered markdown), or "httpx" (plain HTTP).
        max_chars: Maximum characters to return (default 50000 ≈ 12k tokens).

    Returns dict with: url, title, content, backend_used, status_code,
    truncated, error, latency_ms.

    Use this instead of CC WebFetch for:
    - Anti-bot protected sites (Scrapling's TLS fingerprinting)
    - JS-heavy SPAs (Crawl4AI's Playwright rendering)
    - Background sessions (no Bash available)
    - Consistent structured output (no AI summarization)

    Use CC WebFetch when you specifically need AI-processed summaries.
    Use browser_navigate when you need to interact with the page.
    """
    return await _impl_web_fetch(url, backend, max_chars)


@mcp.tool()
async def web_search(
    query: str,
    backend: str = "auto",
    max_results: int = 10,
) -> dict:
    """Search the web and return structured results.

    Smart fallback chain: Tinyfish (free, cloud) → Brave (API).
    Paid backends (tavily, exa, perplexity) available via explicit backend param.

    Args:
        query: Search query string.
        backend: "auto" (Tinyfish→Brave), "tinyfish", "brave", "tavily"
                 (AI-optimized), "exa" (semantic), or "perplexity" (synthesized).
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
