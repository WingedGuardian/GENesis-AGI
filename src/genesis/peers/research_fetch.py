"""Bounded public HTTPS reads with a single, fully vetted DNS answer per hop."""

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpx

from genesis.web.fetch import _extract_html
from genesis.web.private import bounded_body, private_web

URL_LIMIT = 8192
_REDIRECTS = {301, 302, 303, 307, 308}


class ResearchFetchRefused(ValueError):
    """Constant public failure, never an upstream URL or exception message."""


def canonical_url(value: str) -> httpx.URL:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > URL_LIMIT:
        raise ResearchFetchRefused("Research fetch refused")
    if any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ResearchFetchRefused("Research fetch refused")
    try:
        url = httpx.URL(value)
        authority = urlsplit(value).netloc
    except (ValueError, httpx.InvalidURL):
        raise ResearchFetchRefused("Research fetch refused") from None
    # Raw authority also catches an empty userinfo component (https://@host).
    if (
        url.scheme != "https"
        or url.port not in (None, 443)
        or not url.host
        or "@" in authority
        or url.userinfo
        or "#" in value
        or "%" in url.host
    ):
        raise ResearchFetchRefused("Research fetch refused")
    return url


async def vetted_address(host: str) -> str:
    answers = await asyncio.get_running_loop().getaddrinfo(
        host,
        443,
        type=socket.SOCK_STREAM,
        proto=socket.IPPROTO_TCP,
    )
    if not answers:
        raise ResearchFetchRefused("Research fetch refused")
    addresses = []
    for family, socktype, protocol, _, address in answers:
        if family not in (socket.AF_INET, socket.AF_INET6) or socktype != socket.SOCK_STREAM:
            raise ResearchFetchRefused("Research fetch refused")
        if protocol != socket.IPPROTO_TCP:
            raise ResearchFetchRefused("Research fetch refused")
        ip = ipaddress.ip_address(address[0])
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise ResearchFetchRefused("Research fetch refused")
        addresses.append(str(ip))
    return addresses[0]


class PinnedTransport(httpx.AsyncHTTPTransport):
    def __init__(self, verified_ip: str) -> None:
        self.verified_ip = verified_ip
        super().__init__(trust_env=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        original = request.url
        request.url = original.copy_with(host=self.verified_ip)
        request.headers["Host"] = original.netloc.decode("ascii")
        request.extensions = dict(
            request.extensions, sni_hostname=original.raw_host.decode("ascii")
        )
        return await super().handle_async_request(request)


async def fetch_public(url: str, *, timeout_s: float = 20.0) -> dict[str, str]:
    """No proxy, cookie reuse, fallback, browser or caller-selected headers."""
    try:
        with private_web():
            async with asyncio.timeout(min(timeout_s, 20.0)) as deadline:
                current = canonical_url(url)
                original = str(current)
                for hop in range(4):
                    address = await vetted_address(current.host)
                    async with (
                        httpx.AsyncClient(
                            transport=PinnedTransport(address),
                            trust_env=False,
                            follow_redirects=False,
                        ) as client,
                        client.stream(
                            "GET",
                            current,
                            headers={
                                "Accept-Encoding": "identity",
                                "Accept": "text/html,text/plain,application/json",
                                "User-Agent": "Genesis-Peer-Research/1",
                            },
                        ) as response,
                    ):
                        if response.status_code in _REDIRECTS:
                            location = response.headers.get("location", "")
                            if (
                                hop == 3
                                or not location
                                or len(location.encode("utf-8")) > URL_LIMIT
                                or any(ord(c) <= 32 or ord(c) == 127 for c in location)
                            ):
                                raise ResearchFetchRefused("Research fetch refused")
                            reference = httpx.URL(location)
                            if reference.scheme:
                                canonical_url(location)
                            elif location.startswith("//"):
                                canonical_url("https:" + location)
                            current = canonical_url(str(current.join(location)))
                            continue
                        response.raise_for_status()
                        kind = (
                            response.headers.get("content-type", "")
                            .split(";", 1)[0]
                            .strip()
                            .lower()
                        )
                        if kind not in {
                            "text/html",
                            "application/xhtml+xml",
                            "text/plain",
                            "application/json",
                        }:
                            raise ResearchFetchRefused("Research fetch refused")
                        text = (await bounded_body(response)).decode("utf-8", errors="replace")
                        content, title = (
                            _extract_html(text)
                            if kind in {"text/html", "application/xhtml+xml"}
                            else (text, "")
                        )
                        if (
                            deadline.when() is None
                            or asyncio.get_running_loop().time() >= deadline.when()
                        ):
                            raise ResearchFetchRefused("Research fetch refused")
                        return {
                            "original_url": original,
                            "final_url": str(current),
                            "title": title,
                            "content": content,
                        }
    except (ValueError, OSError, httpx.HTTPError, TimeoutError):
        raise ResearchFetchRefused("Research fetch refused") from None
    raise ResearchFetchRefused("Research fetch refused")
