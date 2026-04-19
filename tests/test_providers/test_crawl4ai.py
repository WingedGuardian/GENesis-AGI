"""Tests for Crawl4AIAdapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.providers.crawl4ai_adapter import Crawl4AIAdapter
from genesis.providers.protocol import ToolProvider
from genesis.providers.types import (
    CostTier,
    ProviderCategory,
    ProviderResult,
    ProviderStatus,
)


@pytest.fixture
def adapter():
    return Crawl4AIAdapter()


class TestProtocol:
    def test_implements_tool_provider(self, adapter):
        assert isinstance(adapter, ToolProvider)

    def test_name(self, adapter):
        assert adapter.name == "crawl4ai"

    def test_capability_categories(self, adapter):
        assert ProviderCategory.WEB in adapter.capability.categories

    def test_capability_content_types(self, adapter):
        assert "web_page" in adapter.capability.content_types
        assert "markdown" in adapter.capability.content_types

    def test_capability_cost_tier(self, adapter):
        assert adapter.capability.cost_tier == CostTier.FREE


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_available_when_installed(self, adapter):
        status = await adapter.check_health()
        # crawl4ai is installed in our venv
        assert status == ProviderStatus.AVAILABLE

    @pytest.mark.asyncio
    async def test_unavailable_when_not_installed(self, adapter):
        with patch.dict("sys.modules", {"crawl4ai": None}):
            status = await adapter.check_health()
        assert status == ProviderStatus.UNAVAILABLE


class TestInvoke:
    @pytest.mark.asyncio
    async def test_missing_url(self, adapter):
        result = await adapter.invoke({})
        assert result.success is False
        assert "url" in result.error.lower()

    @pytest.mark.asyncio
    async def test_invalid_url_scheme(self, adapter):
        result = await adapter.invoke({"url": "file:///etc/passwd"})
        assert result.success is False
        assert "http" in result.error.lower()

    @pytest.mark.asyncio
    async def test_ftp_url_rejected(self, adapter):
        result = await adapter.invoke({"url": "ftp://example.com/file"})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_success(self, adapter):
        """Mock AsyncWebCrawler to return markdown content."""
        mock_result = MagicMock()
        mock_result.markdown = "# Hello World"

        mock_crawler = AsyncMock()
        mock_crawler.arun = AsyncMock(return_value=mock_result)
        mock_crawler.__aenter__ = AsyncMock(return_value=mock_crawler)
        mock_crawler.__aexit__ = AsyncMock(return_value=False)

        with patch("crawl4ai.AsyncWebCrawler", return_value=mock_crawler):
            result = await adapter.invoke({"url": "https://example.com"})

        assert isinstance(result, ProviderResult)
        assert result.success is True
        assert len(result.data) == 1
        assert result.data[0]["url"] == "https://example.com"
        assert result.data[0]["markdown"] == "# Hello World"
        assert result.provider_name == "crawl4ai"

    @pytest.mark.asyncio
    async def test_empty_markdown(self, adapter):
        """None markdown should be converted to empty string."""
        mock_result = MagicMock()
        mock_result.markdown = None

        mock_crawler = AsyncMock()
        mock_crawler.arun = AsyncMock(return_value=mock_result)
        mock_crawler.__aenter__ = AsyncMock(return_value=mock_crawler)
        mock_crawler.__aexit__ = AsyncMock(return_value=False)

        with patch("crawl4ai.AsyncWebCrawler", return_value=mock_crawler):
            result = await adapter.invoke({"url": "https://example.com"})

        assert result.success is True
        assert result.data[0]["markdown"] == ""

    @pytest.mark.asyncio
    async def test_crawl4ai_not_installed(self, adapter):
        """Graceful degradation when crawl4ai import fails at invoke time."""
        with patch.dict("sys.modules", {"crawl4ai": None}):
            result = await adapter.invoke({"url": "https://example.com"})

        assert result.success is False
        assert "not installed" in result.error.lower()

    @pytest.mark.asyncio
    async def test_generic_error(self, adapter):
        """Exceptions during crawl should be caught and returned as error."""
        mock_crawler = AsyncMock()
        mock_crawler.arun = AsyncMock(side_effect=RuntimeError("browser crashed"))
        mock_crawler.__aenter__ = AsyncMock(return_value=mock_crawler)
        mock_crawler.__aexit__ = AsyncMock(return_value=False)

        with patch("crawl4ai.AsyncWebCrawler", return_value=mock_crawler):
            result = await adapter.invoke({"url": "https://example.com"})

        assert isinstance(result, ProviderResult)
        assert result.success is False
        assert "browser crashed" in result.error

    @pytest.mark.asyncio
    async def test_latency_tracked(self, adapter):
        mock_result = MagicMock()
        mock_result.markdown = "content"

        mock_crawler = AsyncMock()
        mock_crawler.arun = AsyncMock(return_value=mock_result)
        mock_crawler.__aenter__ = AsyncMock(return_value=mock_crawler)
        mock_crawler.__aexit__ = AsyncMock(return_value=False)

        with patch("crawl4ai.AsyncWebCrawler", return_value=mock_crawler):
            result = await adapter.invoke({"url": "https://example.com"})

        assert result.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_never_raises(self, adapter):
        """invoke() must always return ProviderResult, never raise."""
        mock_crawler = AsyncMock()
        mock_crawler.arun = AsyncMock(side_effect=Exception("unexpected"))
        mock_crawler.__aenter__ = AsyncMock(return_value=mock_crawler)
        mock_crawler.__aexit__ = AsyncMock(return_value=False)

        with patch("crawl4ai.AsyncWebCrawler", return_value=mock_crawler):
            result = await adapter.invoke({"url": "https://example.com"})

        assert isinstance(result, ProviderResult)
        assert result.success is False
