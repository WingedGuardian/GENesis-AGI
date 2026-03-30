"""ToolProvider adapter for Cloudflare Browser Rendering /crawl endpoint."""

from __future__ import annotations

import logging
import os
import time

import httpx

from genesis.providers.types import (
    CostTier,
    ProviderCapability,
    ProviderCategory,
    ProviderResult,
    ProviderStatus,
)

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.cloudflare.com/client/v4/accounts/{account_id}/browser-rendering"
_POLL_INTERVAL = 2.0
_POLL_TIMEOUT = 120.0


class CloudflareCrawlAdapter:
    """Cloudflare /crawl endpoint — whole-site crawl to Markdown."""

    name = "cloudflare_crawl"
    capability = ProviderCapability(
        content_types=("web_page", "crawl"),
        categories=(ProviderCategory.WEB,),
        cost_tier=CostTier.FREE,
        description="Cloudflare /crawl — whole-site crawling to Markdown",
    )

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    def _get_config(self) -> tuple[str, str]:
        """Return (api_key, account_id). Raises ValueError if missing."""
        api_key = os.environ.get("API_KEY_CLOUDFLARE", "")
        account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
        if not api_key or not account_id:
            raise ValueError("API_KEY_CLOUDFLARE and CLOUDFLARE_ACCOUNT_ID required")
        return api_key, account_id

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    def _crawl_url(self, account_id: str) -> str:
        return f"{_BASE_URL.format(account_id=account_id)}/crawl"

    async def check_health(self) -> ProviderStatus:
        """HEAD request to crawl endpoint to verify auth and availability."""
        try:
            api_key, account_id = self._get_config()
        except ValueError:
            return ProviderStatus.UNAVAILABLE

        try:
            client = self._get_client()
            resp = await client.head(
                self._crawl_url(account_id),
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code < 400:
                return ProviderStatus.AVAILABLE
            if resp.status_code < 500:
                # 401/403 = auth issue, 404/405 = endpoint issue
                logger.warning("Cloudflare crawl health: HTTP %s", resp.status_code)
                return ProviderStatus.DEGRADED
            return ProviderStatus.UNAVAILABLE
        except httpx.TimeoutException:
            logger.warning("Cloudflare crawl health check timed out")
            return ProviderStatus.DEGRADED
        except Exception:
            logger.error("Cloudflare crawl health check failed", exc_info=True)
            return ProviderStatus.UNAVAILABLE

    async def invoke(self, request: dict) -> ProviderResult:
        """Start a crawl job and poll for results.

        Request keys:
            url (str): Starting URL (required).
            max_pages (int): Max pages to crawl (default 10).
            render (bool): Render JS (default False for free tier).
        """
        start = time.monotonic()
        try:
            api_key, account_id = self._get_config()
        except ValueError as exc:
            return ProviderResult(
                success=False,
                error=str(exc),
                latency_ms=round((time.monotonic() - start) * 1000, 2),
                provider_name=self.name,
            )

        url = request.get("url", "")
        if not url:
            return ProviderResult(
                success=False,
                error="'url' is required in request",
                latency_ms=round((time.monotonic() - start) * 1000, 2),
                provider_name=self.name,
            )

        # Defense in depth: only allow http(s) URLs
        if not url.startswith(("http://", "https://")):
            return ProviderResult(
                success=False,
                error=f"URL must use http:// or https:// scheme, got: {url[:50]}",
                latency_ms=round((time.monotonic() - start) * 1000, 2),
                provider_name=self.name,
            )

        max_pages = min(request.get("max_pages", 10), 100)  # Cap at 100
        render = request.get("render", False)

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "url": url,
            "scrapeOptions": {"formats": ["markdown"]},
            "limit": max_pages,
            "render": render,
        }

        try:
            client = self._get_client()
            resp = await client.post(
                self._crawl_url(account_id),
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            body = resp.json()

            # The API may return results directly or provide a job ID for polling.
            # Handle both patterns for robustness.
            if "data" in body:
                data = self._extract_results(body["data"])
                latency = round((time.monotonic() - start) * 1000, 2)
                return ProviderResult(
                    success=True,
                    data=data,
                    latency_ms=latency,
                    provider_name=self.name,
                )

            # Async job — poll for completion
            job_id = body.get("jobId") or body.get("id") or body.get("result", {}).get("id")
            if not job_id:
                latency = round((time.monotonic() - start) * 1000, 2)
                return ProviderResult(
                    success=False,
                    error=f"Unexpected response format: {body!r}",
                    latency_ms=latency,
                    provider_name=self.name,
                )

            data = await self._poll_job(client, api_key, account_id, job_id)
            latency = round((time.monotonic() - start) * 1000, 2)
            return ProviderResult(
                success=True,
                data=data,
                latency_ms=latency,
                provider_name=self.name,
            )

        except httpx.HTTPStatusError as exc:
            latency = round((time.monotonic() - start) * 1000, 2)
            logger.error(
                "Cloudflare crawl HTTP error: %s", exc.response.status_code, exc_info=True
            )
            return ProviderResult(
                success=False,
                error=f"HTTP {exc.response.status_code}: {exc.response.text[:200]}",
                latency_ms=latency,
                provider_name=self.name,
            )
        except httpx.TimeoutException:
            latency = round((time.monotonic() - start) * 1000, 2)
            logger.error("Cloudflare crawl timed out", exc_info=True)
            return ProviderResult(
                success=False,
                error="Request timed out",
                latency_ms=latency,
                provider_name=self.name,
            )
        except Exception as exc:
            latency = round((time.monotonic() - start) * 1000, 2)
            logger.error("Cloudflare crawl failed", exc_info=True)
            return ProviderResult(
                success=False,
                error=str(exc),
                latency_ms=latency,
                provider_name=self.name,
            )

    async def _poll_job(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        account_id: str,
        job_id: str,
    ) -> list[dict]:
        """Poll a crawl job until completion or timeout."""
        import asyncio

        status_url = f"{self._crawl_url(account_id)}/{job_id}"
        headers = {"Authorization": f"Bearer {api_key}"}
        deadline = time.monotonic() + _POLL_TIMEOUT

        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)
            resp = await client.get(status_url, headers=headers)
            resp.raise_for_status()
            body = resp.json()

            status = body.get("status", "")
            if status in ("completed", "done"):
                return self._extract_results(body.get("data", []))
            if status in ("failed", "error"):
                raise RuntimeError(f"Crawl job failed: {body.get('error', 'unknown')}")

        raise TimeoutError(f"Crawl job {job_id} did not complete within {_POLL_TIMEOUT}s")

    @staticmethod
    def _extract_results(data: list | dict) -> list[dict]:
        """Normalize crawl results into [{url, markdown}] format."""
        if isinstance(data, dict):
            data = [data]
        results = []
        for item in data:
            if not isinstance(item, dict):
                continue
            results.append({
                "url": item.get("url", item.get("sourceURL", "")),
                "markdown": item.get("markdown", item.get("content", "")),
            })
        return results
