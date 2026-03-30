"""Tests for CloudflareCrawlAdapter."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from genesis.providers.cloudflare_crawl import CloudflareCrawlAdapter
from genesis.providers.protocol import ToolProvider
from genesis.providers.types import (
    CostTier,
    ProviderCategory,
    ProviderResult,
    ProviderStatus,
)

ENV = {"API_KEY_CLOUDFLARE": "test-key", "CLOUDFLARE_ACCOUNT_ID": "test-account"}


@pytest.fixture
def adapter():
    return CloudflareCrawlAdapter()


class TestProtocol:
    def test_implements_tool_provider(self, adapter):
        assert isinstance(adapter, ToolProvider)

    def test_name(self, adapter):
        assert adapter.name == "cloudflare_crawl"

    def test_capability_categories(self, adapter):
        assert ProviderCategory.WEB in adapter.capability.categories

    def test_capability_content_types(self, adapter):
        assert "crawl" in adapter.capability.content_types
        assert "web_page" in adapter.capability.content_types

    def test_capability_cost_tier(self, adapter):
        assert adapter.capability.cost_tier == CostTier.FREE


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_available(self, adapter):
        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.head = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        with patch.dict("os.environ", ENV):
            status = await adapter.check_health()
        assert status == ProviderStatus.AVAILABLE

    @pytest.mark.asyncio
    async def test_auth_failure_degraded(self, adapter):
        """4xx (auth/not-found) should be DEGRADED, not AVAILABLE."""
        mock_resp = MagicMock(status_code=401)
        mock_client = AsyncMock()
        mock_client.head = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        with patch.dict("os.environ", ENV):
            status = await adapter.check_health()
        assert status == ProviderStatus.DEGRADED

    @pytest.mark.asyncio
    async def test_server_error(self, adapter):
        mock_resp = MagicMock(status_code=500)
        mock_client = AsyncMock()
        mock_client.head = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        with patch.dict("os.environ", ENV):
            status = await adapter.check_health()
        assert status == ProviderStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_timeout(self, adapter):
        mock_client = AsyncMock()
        mock_client.head = AsyncMock(side_effect=httpx.ConnectTimeout("timeout"))
        adapter._client = mock_client

        with patch.dict("os.environ", ENV):
            status = await adapter.check_health()
        assert status == ProviderStatus.DEGRADED

    @pytest.mark.asyncio
    async def test_connection_error(self, adapter):
        mock_client = AsyncMock()
        mock_client.head = AsyncMock(side_effect=httpx.ConnectError("refused"))
        adapter._client = mock_client

        with patch.dict("os.environ", ENV):
            status = await adapter.check_health()
        assert status == ProviderStatus.UNAVAILABLE

    @pytest.mark.asyncio
    async def test_missing_env_vars(self, adapter):
        with patch.dict("os.environ", {}, clear=True):
            status = await adapter.check_health()
        assert status == ProviderStatus.UNAVAILABLE


class TestInvoke:
    @pytest.mark.asyncio
    async def test_success_direct_data(self, adapter):
        """API returns data directly (no polling)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "data": [
                {"url": "https://example.com", "markdown": "# Hello"},
                {"url": "https://example.com/about", "markdown": "# About"},
            ]
        }
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        with patch.dict("os.environ", ENV):
            result = await adapter.invoke({"url": "https://example.com"})

        assert isinstance(result, ProviderResult)
        assert result.success is True
        assert len(result.data) == 2
        assert result.data[0]["url"] == "https://example.com"
        assert result.data[0]["markdown"] == "# Hello"
        assert result.provider_name == "cloudflare_crawl"

    @pytest.mark.asyncio
    async def test_success_with_polling(self, adapter):
        """API returns a job ID, then polling returns results."""
        post_resp = MagicMock()
        post_resp.raise_for_status = MagicMock()
        post_resp.json.return_value = {"jobId": "job-123"}

        poll_resp = MagicMock()
        poll_resp.raise_for_status = MagicMock()
        poll_resp.json.return_value = {
            "status": "completed",
            "data": [{"url": "https://example.com", "markdown": "# Done"}],
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=post_resp)
        mock_client.get = AsyncMock(return_value=poll_resp)
        adapter._client = mock_client

        with patch.dict("os.environ", ENV), patch("asyncio.sleep", new_callable=AsyncMock):
            result = await adapter.invoke({"url": "https://example.com"})

        assert result.success is True
        assert len(result.data) == 1

    @pytest.mark.asyncio
    async def test_missing_url(self, adapter):
        with patch.dict("os.environ", ENV):
            result = await adapter.invoke({})
        assert result.success is False
        assert "url" in result.error.lower()

    @pytest.mark.asyncio
    async def test_missing_env(self, adapter):
        with patch.dict("os.environ", {}, clear=True):
            result = await adapter.invoke({"url": "https://example.com"})
        assert result.success is False
        assert "required" in result.error.lower()

    @pytest.mark.asyncio
    async def test_invalid_url_scheme_rejected(self, adapter):
        """URLs without http(s):// must be rejected."""
        with patch.dict("os.environ", ENV):
            result = await adapter.invoke({"url": "file:///etc/passwd"})
        assert result.success is False
        assert "http" in result.error.lower()

    @pytest.mark.asyncio
    async def test_ftp_url_rejected(self, adapter):
        with patch.dict("os.environ", ENV):
            result = await adapter.invoke({"url": "ftp://example.com/file"})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_max_pages_capped_at_100(self, adapter):
        """max_pages should be capped at 100 even if caller requests more."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"data": []}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        with patch.dict("os.environ", ENV):
            await adapter.invoke({"url": "https://example.com", "max_pages": 5000})

        payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1]["json"]
        assert payload["limit"] == 100

    @pytest.mark.asyncio
    async def test_http_error(self, adapter):
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "403", request=MagicMock(), response=mock_resp
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        with patch.dict("os.environ", ENV):
            result = await adapter.invoke({"url": "https://example.com"})
        assert result.success is False
        assert "403" in result.error

    @pytest.mark.asyncio
    async def test_timeout_error(self, adapter):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
        adapter._client = mock_client

        with patch.dict("os.environ", ENV):
            result = await adapter.invoke({"url": "https://example.com"})
        assert result.success is False
        assert "timed out" in result.error.lower()

    @pytest.mark.asyncio
    async def test_generic_error(self, adapter):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=RuntimeError("boom"))
        adapter._client = mock_client

        with patch.dict("os.environ", ENV):
            result = await adapter.invoke({"url": "https://example.com"})
        assert result.success is False
        assert "boom" in result.error

    @pytest.mark.asyncio
    async def test_latency_tracked(self, adapter):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"data": []}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        with patch.dict("os.environ", ENV):
            result = await adapter.invoke({"url": "https://example.com"})
        assert result.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_default_parameters(self, adapter):
        """Verify render=False and max_pages=10 defaults in payload."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"data": []}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        with patch.dict("os.environ", ENV):
            await adapter.invoke({"url": "https://example.com"})

        call_kwargs = mock_client.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert payload["render"] is False
        assert payload["limit"] == 10

    @pytest.mark.asyncio
    async def test_never_raises(self, adapter):
        """invoke() must always return ProviderResult, never raise."""
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("unexpected"))
        adapter._client = mock_client

        with patch.dict("os.environ", ENV):
            result = await adapter.invoke({"url": "https://example.com"})
        assert isinstance(result, ProviderResult)
        assert result.success is False


class TestPolling:
    @pytest.mark.asyncio
    async def test_poll_job_failed_status(self, adapter):
        """Job that returns status='failed' should result in error."""
        post_resp = MagicMock()
        post_resp.raise_for_status = MagicMock()
        post_resp.json.return_value = {"jobId": "job-fail"}

        poll_resp = MagicMock()
        poll_resp.raise_for_status = MagicMock()
        poll_resp.json.return_value = {"status": "failed", "error": "site unreachable"}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=post_resp)
        mock_client.get = AsyncMock(return_value=poll_resp)
        adapter._client = mock_client

        with patch.dict("os.environ", ENV), patch("asyncio.sleep", new_callable=AsyncMock):
            result = await adapter.invoke({"url": "https://example.com"})

        assert result.success is False
        assert "failed" in result.error.lower() or "site unreachable" in result.error.lower()

    @pytest.mark.asyncio
    async def test_poll_timeout(self, adapter):
        """Job that never completes should timeout."""
        post_resp = MagicMock()
        post_resp.raise_for_status = MagicMock()
        post_resp.json.return_value = {"jobId": "job-stuck"}

        poll_resp = MagicMock()
        poll_resp.raise_for_status = MagicMock()
        poll_resp.json.return_value = {"status": "running"}

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=post_resp)
        mock_client.get = AsyncMock(return_value=poll_resp)
        adapter._client = mock_client

        # Make time.monotonic advance past the deadline
        call_count = 0
        original_monotonic = time.monotonic

        def advancing_monotonic():
            nonlocal call_count
            call_count += 1
            # After a few calls, jump past deadline
            if call_count > 4:
                return original_monotonic() + 200  # Past _POLL_TIMEOUT
            return original_monotonic()

        with (
            patch.dict("os.environ", ENV),
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch("genesis.providers.cloudflare_crawl.time") as mock_time,
        ):
            mock_time.monotonic = advancing_monotonic
            result = await adapter.invoke({"url": "https://example.com"})

        assert result.success is False
        assert "timed out" in result.error.lower() or "did not complete" in result.error.lower()


class TestExtractResults:
    def test_list_input(self):
        data = [{"url": "a", "markdown": "b"}]
        assert CloudflareCrawlAdapter._extract_results(data) == [{"url": "a", "markdown": "b"}]

    def test_dict_input(self):
        data = {"url": "a", "markdown": "b"}
        assert CloudflareCrawlAdapter._extract_results(data) == [{"url": "a", "markdown": "b"}]

    def test_alternate_keys(self):
        data = [{"sourceURL": "a", "content": "b"}]
        result = CloudflareCrawlAdapter._extract_results(data)
        assert result == [{"url": "a", "markdown": "b"}]

    def test_non_dict_items_skipped(self):
        data = [{"url": "a", "markdown": "b"}, "garbage", 42]
        assert len(CloudflareCrawlAdapter._extract_results(data)) == 1
