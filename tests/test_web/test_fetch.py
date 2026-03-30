"""Tests for genesis.web.fetch — WebFetcher with HTML extraction."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from genesis.web.fetch import WebFetcher


def _make_response(
    text: str, content_type: str = "text/html", status_code: int = 200,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = {"content-type": content_type}
    resp.raise_for_status = MagicMock()
    return resp


SIMPLE_HTML = """
<html>
<head><title>Test Page</title></head>
<body>
<nav>Navigation</nav>
<h1>Hello World</h1>
<p>This is content.</p>
<script>alert('bad')</script>
<style>.x{color:red}</style>
<footer>Footer stuff</footer>
</body>
</html>
"""


@pytest.fixture
def fetcher():
    return WebFetcher(max_chars=50_000)


@pytest.mark.asyncio
async def test_fetch_html_extracts_text(fetcher: WebFetcher):
    fetcher._client.get = AsyncMock(return_value=_make_response(SIMPLE_HTML))
    result = await fetcher.fetch("https://example.com")
    assert result.title == "Test Page"
    assert "Hello World" in result.text
    assert "This is content" in result.text
    assert result.status_code == 200
    assert result.error is None


@pytest.mark.asyncio
async def test_fetch_plain_text(fetcher: WebFetcher):
    fetcher._client.get = AsyncMock(
        return_value=_make_response("Plain text here", "text/plain"),
    )
    result = await fetcher.fetch("https://example.com")
    assert "Plain text here" in result.text
    assert "<external-content" in result.text  # boundary markers
    assert result.title == ""


@pytest.mark.asyncio
async def test_fetch_json(fetcher: WebFetcher):
    fetcher._client.get = AsyncMock(
        return_value=_make_response('{"key": "value"}', "application/json"),
    )
    result = await fetcher.fetch("https://example.com/api")
    assert '"key"' in result.text


@pytest.mark.asyncio
async def test_fetch_binary_returns_error(fetcher: WebFetcher):
    fetcher._client.get = AsyncMock(
        return_value=_make_response("", "application/pdf"),
    )
    result = await fetcher.fetch("https://example.com/doc.pdf")
    assert result.error is not None
    assert "Unsupported" in result.error


@pytest.mark.asyncio
async def test_fetch_http_error(fetcher: WebFetcher):
    exc = httpx.HTTPStatusError(
        "Not Found", request=MagicMock(), response=MagicMock(status_code=404),
    )
    fetcher._client.get = AsyncMock(side_effect=exc)
    result = await fetcher.fetch("https://example.com/missing")
    assert result.error is not None
    assert result.status_code == 404


@pytest.mark.asyncio
async def test_fetch_connection_error(fetcher: WebFetcher):
    fetcher._client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    result = await fetcher.fetch("https://down.example.com")
    assert result.error is not None
    assert result.text == ""


@pytest.mark.asyncio
async def test_fetch_truncation(fetcher: WebFetcher):
    long_html = "<html><body><p>" + "x" * 1000 + "</p></body></html>"
    fetcher._client.get = AsyncMock(return_value=_make_response(long_html))
    result = await fetcher.fetch("https://example.com", max_chars=100)
    assert result.truncated is True
    # Content is truncated to max_chars THEN wrapped in boundary markers
    assert "x" * 50 in result.text  # truncated content present
    assert "<external-content" in result.text


@pytest.mark.asyncio
async def test_fetch_noise_stripping(fetcher: WebFetcher):
    fetcher._client.get = AsyncMock(return_value=_make_response(SIMPLE_HTML))
    result = await fetcher.fetch("https://example.com")
    assert "alert" not in result.text
    assert "Navigation" not in result.text
    assert "Footer stuff" not in result.text
    assert "color:red" not in result.text


@pytest.mark.asyncio
async def test_fetch_custom_max_chars(fetcher: WebFetcher):
    html = "<html><body><p>" + "y" * 500 + "</p></body></html>"
    fetcher._client.get = AsyncMock(return_value=_make_response(html))
    result = await fetcher.fetch("https://example.com", max_chars=200)
    # Content truncated to 200 chars then wrapped (markers add ~70 chars)
    assert "y" * 100 in result.text
    assert "<external-content" in result.text
