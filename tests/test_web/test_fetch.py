"""Tests for genesis.web.fetch — WebFetcher with HTML extraction."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from genesis.web.fetch import WebFetcher


def _make_httpx_response(
    text: str, content_type: str = "text/html", status_code: int = 200,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = {"content-type": content_type}
    resp.raise_for_status = MagicMock()
    return resp


def _make_scrapling_response(
    body: bytes, status: int = 200, content_type: str = "text/html",
) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.body = body
    resp.headers = {"content-type": content_type}
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


# ── httpx fallback path tests ────────────────────────────────────


@pytest.mark.asyncio
@patch("genesis.web.fetch._HAS_SCRAPLING", False)
async def test_fetch_html_extracts_text(fetcher: WebFetcher):
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=_make_httpx_response(SIMPLE_HTML))
    fetcher._client = mock_client
    result = await fetcher.fetch("https://example.com")
    assert result.title == "Test Page"
    assert "Hello World" in result.text
    assert "This is content" in result.text
    assert result.status_code == 200
    assert result.error is None


@pytest.mark.asyncio
@patch("genesis.web.fetch._HAS_SCRAPLING", False)
async def test_fetch_plain_text(fetcher: WebFetcher):
    mock_client = MagicMock()
    mock_client.get = AsyncMock(
        return_value=_make_httpx_response("Plain text here", "text/plain"),
    )
    fetcher._client = mock_client
    result = await fetcher.fetch("https://example.com")
    assert "Plain text here" in result.text
    assert "<external-content" in result.text
    assert result.title == ""


@pytest.mark.asyncio
@patch("genesis.web.fetch._HAS_SCRAPLING", False)
async def test_fetch_json(fetcher: WebFetcher):
    mock_client = MagicMock()
    mock_client.get = AsyncMock(
        return_value=_make_httpx_response('{"key": "value"}', "application/json"),
    )
    fetcher._client = mock_client
    result = await fetcher.fetch("https://example.com/api")
    assert '"key"' in result.text


@pytest.mark.asyncio
@patch("genesis.web.fetch._HAS_SCRAPLING", False)
async def test_fetch_binary_returns_error(fetcher: WebFetcher):
    mock_client = MagicMock()
    mock_client.get = AsyncMock(
        return_value=_make_httpx_response("", "application/pdf"),
    )
    fetcher._client = mock_client
    result = await fetcher.fetch("https://example.com/doc.pdf")
    assert result.error is not None
    assert "Unsupported" in result.error


@pytest.mark.asyncio
@patch("genesis.web.fetch._HAS_SCRAPLING", False)
async def test_fetch_http_error(fetcher: WebFetcher):
    exc = httpx.HTTPStatusError(
        "Not Found", request=MagicMock(), response=MagicMock(status_code=404),
    )
    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=exc)
    fetcher._client = mock_client
    result = await fetcher.fetch("https://example.com/missing")
    assert result.error is not None
    assert result.status_code == 404


@pytest.mark.asyncio
@patch("genesis.web.fetch._HAS_SCRAPLING", False)
async def test_fetch_connection_error(fetcher: WebFetcher):
    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    fetcher._client = mock_client
    result = await fetcher.fetch("https://down.example.com")
    assert result.error is not None
    assert result.text == ""


@pytest.mark.asyncio
@patch("genesis.web.fetch._HAS_SCRAPLING", False)
async def test_fetch_truncation(fetcher: WebFetcher):
    long_html = "<html><body><p>" + "x" * 1000 + "</p></body></html>"
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=_make_httpx_response(long_html))
    fetcher._client = mock_client
    result = await fetcher.fetch("https://example.com", max_chars=100)
    assert result.truncated is True
    assert "x" * 50 in result.text
    assert "<external-content" in result.text


@pytest.mark.asyncio
@patch("genesis.web.fetch._HAS_SCRAPLING", False)
async def test_fetch_noise_stripping(fetcher: WebFetcher):
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=_make_httpx_response(SIMPLE_HTML))
    fetcher._client = mock_client
    result = await fetcher.fetch("https://example.com")
    assert "alert" not in result.text
    assert "Navigation" not in result.text
    assert "Footer stuff" not in result.text
    assert "color:red" not in result.text


# ── Scrapling path tests ─────────────────────────────────────────


@pytest.mark.asyncio
@patch("genesis.web.fetch._HAS_SCRAPLING", True)
async def test_scrapling_html_extracts_text(fetcher: WebFetcher):
    mock_resp = _make_scrapling_response(SIMPLE_HTML.encode())
    with patch("genesis.web.fetch._ScraplingFetcher") as mock_fetcher:
        mock_fetcher.get = AsyncMock(return_value=mock_resp)
        result = await fetcher.fetch("https://example.com")
    assert result.title == "Test Page"
    assert "Hello World" in result.text
    assert result.status_code == 200
    assert result.error is None
    mock_fetcher.get.assert_awaited_once()
    call_kwargs = mock_fetcher.get.call_args
    assert call_kwargs.kwargs.get("impersonate") == "chrome" or call_kwargs[1].get("impersonate") == "chrome"


@pytest.mark.asyncio
@patch("genesis.web.fetch._HAS_SCRAPLING", True)
async def test_scrapling_status_error(fetcher: WebFetcher):
    mock_resp = _make_scrapling_response(b"", status=403)
    with patch("genesis.web.fetch._ScraplingFetcher") as mock_fetcher:
        mock_fetcher.get = AsyncMock(return_value=mock_resp)
        result = await fetcher.fetch("https://blocked.example.com")
    assert result.error is not None
    assert result.status_code == 403


@pytest.mark.asyncio
@patch("genesis.web.fetch._HAS_SCRAPLING", True)
async def test_scrapling_connection_error(fetcher: WebFetcher):
    with patch("genesis.web.fetch._ScraplingFetcher") as mock_fetcher:
        mock_fetcher.get = AsyncMock(side_effect=ConnectionError("refused"))
        result = await fetcher.fetch("https://down.example.com")
    assert result.error is not None
    assert result.text == ""


@pytest.mark.asyncio
@patch("genesis.web.fetch._HAS_SCRAPLING", True)
async def test_scrapling_plain_text(fetcher: WebFetcher):
    mock_resp = _make_scrapling_response(b"Just plain text", content_type="text/plain")
    with patch("genesis.web.fetch._ScraplingFetcher") as mock_fetcher:
        mock_fetcher.get = AsyncMock(return_value=mock_resp)
        result = await fetcher.fetch("https://example.com/plain")
    assert "Just plain text" in result.text
    assert "<external-content" in result.text


@pytest.mark.asyncio
@patch("genesis.web.fetch._HAS_SCRAPLING", True)
async def test_scrapling_unsupported_content_type(fetcher: WebFetcher):
    mock_resp = _make_scrapling_response(b"\x00\x01", content_type="application/pdf")
    with patch("genesis.web.fetch._ScraplingFetcher") as mock_fetcher:
        mock_fetcher.get = AsyncMock(return_value=mock_resp)
        result = await fetcher.fetch("https://example.com/doc.pdf")
    assert result.error is not None
    assert "Unsupported" in result.error
