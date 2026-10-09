"""Early transport boundary tests; no peer authority is supplied by these fixtures."""

import asyncio
import logging
import socket
from unittest.mock import AsyncMock

import httpx
import pytest

from genesis.peers import research_fetch as fetch
from genesis.web.private import BODY_LIMIT, bounded_body, private_web
from genesis.web.search import WebSearcher


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://example.com:444",
        "https://@example.com",
        "https://user@example.com",
        "https://example.com/#",
        "https://example.com/a b",
        "https://example.com/\n",
        "https://[fe80::1%25eth0]",
        "",
        "https://example.com/" + "x" * 8192,
    ],
)
def test_invalid_urls(url):
    with pytest.raises(fetch.ResearchFetchRefused, match="^Research fetch refused$"):
        fetch.canonical_url(url)


@pytest.mark.parametrize(
    "url", ["https://example.com/a?q=x", "https://bücher.example", "https://[2606:4700:4700::1111]"]
)
async def test_authority_pin(url, monkeypatch):
    original = httpx.URL(url)
    captured = []

    async def capture(self, request):
        captured.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", capture)
    async with fetch.PinnedTransport("1.1.1.1") as transport:
        await transport.handle_async_request(
            httpx.Request("GET", original, extensions={"timeout": {"connect": 1}})
        )
    request = captured[0]
    assert request.url.host == "1.1.1.1"
    assert request.headers["host"].encode("ascii") == original.netloc
    assert request.extensions["sni_hostname"].encode("ascii") == original.raw_host
    assert request.extensions["timeout"] == {"connect": 1}


def test_pathless_query_at_is_not_userinfo():
    assert str(fetch.canonical_url("https://example.com?q=a@b")) == "https://example.com?q=a@b"


def answer(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))


@pytest.mark.parametrize(
    "addresses",
    [
        [],
        ["127.0.0.1"],
        ["100.64.0.1"],
        ["224.0.0.1"],
        ["::1"],
        ["::ffff:127.0.0.1"],
        ["1.1.1.1", "127.0.0.1"],
    ],
)
async def test_all_dns_candidates_checked(addresses, monkeypatch):
    lookup = AsyncMock(return_value=[answer(a) for a in addresses])
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
    with pytest.raises(fetch.ResearchFetchRefused):
        await fetch.vetted_address("example.com")
    assert lookup.await_count == 1


@pytest.mark.parametrize(
    "address,expected",
    [
        ("1.1.1.1", "1.1.1.1"),
        ("::ffff:1.1.1.1", "1.1.1.1"),
        ("2606:4700:4700::1111", "2606:4700:4700::1111"),
    ],
)
async def test_public_dns(address, expected, monkeypatch):
    monkeypatch.setattr(
        asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=[answer(address)])
    )
    assert await fetch.vetted_address("example.com") == expected


class Stream(httpx.AsyncByteStream):
    def __init__(self, size):
        self.size = size
        self.read = False

    async def __aiter__(self):
        self.read = True
        for start in range(0, self.size, 4096):
            yield b"x" * min(4096, self.size - start)


@pytest.mark.parametrize("size", [BODY_LIMIT - 1, BODY_LIMIT, BODY_LIMIT + 1])
async def test_stream_cap(size):
    response = httpx.Response(200, stream=Stream(size), headers={"content-length": "1"})
    if size > BODY_LIMIT:
        with pytest.raises(ValueError):
            await bounded_body(response)
    else:
        assert len(await bounded_body(response)) == size


async def test_compression_refused_before_read():
    stream = Stream(BODY_LIMIT * 2)
    with pytest.raises(ValueError):
        await bounded_body(httpx.Response(200, stream=stream, headers={"content-encoding": "gzip"}))
    assert not stream.read


async def test_dns_deadline(monkeypatch):
    async def stalled(host):
        await asyncio.sleep(1)

    monkeypatch.setattr(fetch, "vetted_address", stalled)
    with pytest.raises(fetch.ResearchFetchRefused):
        await fetch.fetch_public("https://example.com", timeout_s=0.01)


@pytest.mark.parametrize("redirects", [0, 1, 2, 3, 4])
async def test_redirect_budget_and_cookie_isolation(monkeypatch, redirects):
    requests = []
    lookup = AsyncMock(return_value="1.1.1.1")
    monkeypatch.setattr(fetch, "vetted_address", lookup)

    async def respond(self, request):
        requests.append(request)
        if len(requests) <= redirects:
            return httpx.Response(
                302, headers={"location": "/next", "set-cookie": "tracking=present"}
            )
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"public text")

    monkeypatch.setattr(fetch.PinnedTransport, "handle_async_request", respond)
    if redirects > 3:
        with pytest.raises(fetch.ResearchFetchRefused):
            await fetch.fetch_public("https://example.com/start")
        assert len(requests) == 4
    else:
        result = await fetch.fetch_public("https://example.com/start")
        assert result["content"] == "public text"
        assert result["original_url"] == "https://example.com/start"
        assert result["final_url"] == (
            "https://example.com/next" if redirects else "https://example.com/start"
        )
    assert lookup.await_count == len(requests)
    assert all(
        "cookie" not in request.headers and "authorization" not in request.headers
        for request in requests
    )


@pytest.mark.parametrize(
    "location",
    [
        "http://example.com",
        "https://example.com:444",
        "https://@example.com",
        "/bad path",
        "/bad\npath",
        "https://example.com/#fragment",
    ],
)
async def test_invalid_redirect_never_reaches_next_transport(monkeypatch, location):
    monkeypatch.setattr(fetch, "vetted_address", AsyncMock(return_value="1.1.1.1"))
    transport = AsyncMock(return_value=httpx.Response(302, headers={"location": location}))
    monkeypatch.setattr(fetch.PinnedTransport, "handle_async_request", transport)
    with pytest.raises(fetch.ResearchFetchRefused):
        await fetch.fetch_public("https://example.com")
    assert transport.await_count == 1


async def test_html_extraction_order(monkeypatch):
    monkeypatch.setattr(fetch, "vetted_address", AsyncMock(return_value="1.1.1.1"))
    monkeypatch.setattr(
        fetch.PinnedTransport,
        "handle_async_request",
        AsyncMock(
            return_value=httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"<title>Public title</title><p>Public body</p><script>ignored</script>",
            )
        ),
    )
    result = await fetch.fetch_public("https://example.com")
    assert result["title"] == "Public title"
    assert "Public body" in result["content"]
    assert "ignored" not in result["content"]


async def test_private_logging_is_task_local(caplog):
    caplog.set_level(logging.DEBUG, logger="httpx")
    ready = asyncio.Event()
    done = asyncio.Event()
    log = logging.getLogger("httpx")
    assert not log.disabled
    assert log.isEnabledFor(logging.INFO), (
        log.level,
        log.getEffectiveLevel(),
        logging.root.manager.disable,
    )
    assert log.propagate

    async def private():
        with private_web():
            log.info("private input")
            ready.set()
            await done.wait()

    task = asyncio.create_task(private())
    await ready.wait()
    log.info("owner positive control")
    done.set()
    await task
    log.info("context restored")
    assert "private input" not in caplog.text
    assert "owner positive control" in caplog.text
    assert "context restored" in caplog.text


@pytest.mark.parametrize(
    "name",
    [
        "httpx",
        "httpcore.connection",
        "httpcore.http11",
        "httpcore.http2",
        "httpcore.proxy",
        "httpcore.socks",
    ],
)
@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR])
def test_private_log_population_and_exception_reset(caplog, name, level):
    caplog.set_level(logging.DEBUG, logger=name)
    logger = logging.getLogger(name)
    with pytest.raises(asyncio.CancelledError), private_web():
        logger.log(level, "private marker")
        raise asyncio.CancelledError
    logger.log(level, "owner marker")
    assert "private marker" not in caplog.text
    assert "owner marker" in caplog.text


async def test_body_deadline_closes_client(monkeypatch):
    class Stalled(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            await asyncio.sleep(1)
            yield b"body"

        async def aclose(self):
            self.closed = True

    stream = Stalled()
    monkeypatch.setattr(fetch, "vetted_address", AsyncMock(return_value="1.1.1.1"))
    monkeypatch.setattr(
        fetch.PinnedTransport,
        "handle_async_request",
        AsyncMock(
            return_value=httpx.Response(200, headers={"content-type": "text/plain"}, stream=stream)
        ),
    )
    with pytest.raises(fetch.ResearchFetchRefused):
        await fetch.fetch_public("https://example.com", timeout_s=0.01)
    assert stream.closed


async def test_private_search_stream_and_failure_privacy(monkeypatch, caplog):
    monkeypatch.delenv("API_KEY_BRAVE", raising=False)
    caplog.set_level(logging.DEBUG, logger="httpx")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, stream=Stream(BODY_LIMIT + 1))

    searcher = WebSearcher()
    await searcher._client.aclose()
    searcher._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        result = await searcher.search("private query marker", private_observability=True)
    finally:
        await searcher._client.aclose()
    assert result.error == "Private search unavailable"
    assert "private query marker" not in caplog.text
    assert "localhost" not in caplog.text
    assert requests[0].headers["accept-encoding"] == "identity"
