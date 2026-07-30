"""LAN vs WAN endpoint classification — stdlib-only, sync + async.

Answers one question: *does reaching this host require the public internet?*
An endpoint is ``"local"`` (reachable on the LAN / mesh, survives a WAN outage)
or ``"wan"`` (needs the internet). Used by the CC network preflight (PR-3): a
hard-OFFLINE state parks only ``wan`` dispatches — a LAN CC peer keeps working.

Design (encode the *principle*, not a hardcoded host list):
- **IP-literal fast-path.** A literal address classifies directly from its range
  with no DNS — the common case (roster peers carry literal or public URLs, the
  native ``claude`` endpoint carries none → WAN by rule).
- **Hostname resolution + last-known-good cache.** A hostname is resolved
  (``getaddrinfo``) and classified from its addresses. The classification is
  cached so it *survives DNS loss*: during an outage a previously-seen LAN host
  keeps its ``local`` verdict instead of flipping to WAN just because the
  resolver is unreachable.
- **Conservative fallback.** An unresolvable host with no cached verdict →
  ``"wan"``. Rationale: mis-parking a working LAN peer (false ``wan``) breaks
  legitimate local work, but failing to park a WAN host (false ``local``) only
  degrades to the pre-preflight behavior (the dispatch hangs, as it does today).
  We resolve ambiguity toward ``wan`` *only* when we have no signal at all.

LAN set (RFC-grounded, so it generalizes to any install):
loopback (127/8, ::1), RFC1918 (10/8, 172.16/12, 192.168/16),
CGNAT / Tailscale (100.64/10), link-local (169.254/16, fe80::/10),
IPv6 ULA (fc00::/7 — covers Tailscale's fd7a:… mesh addresses).

Async note: in-loop callers MUST use the ``*_async`` variants — they resolve via
``loop.getaddrinfo`` so a slow/dead resolver never blocks the event loop (a bare
``socket.getaddrinfo`` would). The sync variants exist for stdlib-sync callers.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from collections.abc import Callable
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

LOCAL = "local"
WAN = "wan"

# RFC-grounded LAN ranges. Defined explicitly rather than via ipaddress'
# ``.is_private`` because that property's membership (notably CGNAT 100.64/10)
# has shifted across CPython versions — an explicit table is version-stable and
# self-documenting, and the plan calls for encoding the principle directly.
_LOCAL_NETS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),  # loopback
    ipaddress.ip_network("10.0.0.0/8"),  # RFC1918
    ipaddress.ip_network("172.16.0.0/12"),  # RFC1918
    ipaddress.ip_network("192.168.0.0/16"),  # RFC1918
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT / Tailscale v4
    ipaddress.ip_network("169.254.0.0/16"),  # link-local v4
    ipaddress.ip_network("::1/128"),  # loopback v6
    ipaddress.ip_network("fc00::/7"),  # ULA v6 (incl. Tailscale mesh)
    ipaddress.ip_network("fe80::/10"),  # link-local v6
)

_DEFAULT_CACHE_TTL_S = 300


def _classify_ip_obj(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """Classify a parsed IP address object as ``local`` or ``wan``."""
    for net in _LOCAL_NETS:
        # Version guard: ``in`` across families raises TypeError otherwise.
        if ip.version == net.version and ip in net:
            return LOCAL
    return WAN


def ip_literal_class(host: str) -> str | None:
    """Classify ``host`` if it is an IP literal, else ``None``.

    Accepts bracketed IPv6 (``[::1]``) as it appears in URLs.
    """
    candidate = host.strip()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    # IPv6 zone id (``fe80::1%eth0``) — strip before parsing.
    candidate = candidate.split("%", 1)[0]
    try:
        return _classify_ip_obj(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _classify_addrs(addrs: list[str]) -> str:
    """LOCAL iff every resolved address is LOCAL (empty → WAN).

    All-local (not any-local) so an ambiguous/mixed result defaults toward WAN.
    Real LAN hosts resolve to purely-local addresses, so this never mis-parks
    them; there is no realistic mixed LAN+WAN endpoint in this use.
    """
    if not addrs:
        return WAN
    return LOCAL if all(ip_literal_class(a) == LOCAL for a in addrs) else WAN


class NetClassifier:
    """Endpoint classifier with a last-known-good resolution cache.

    Injectable clock/resolvers make it fully deterministic under test. The cache
    is per-instance; :func:`default_classifier` returns a shared process-wide
    instance so its cache warms across CC dispatches.
    """

    def __init__(
        self,
        *,
        cache_ttl_s: int = _DEFAULT_CACHE_TTL_S,
        clock: Callable[[], float] | None = None,
        resolver: Callable[[str], list[str]] | None = None,
        async_resolver: Callable[[str], object] | None = None,
    ):
        self._ttl = cache_ttl_s
        self._clock = clock or time.monotonic
        self._resolver = resolver
        self._async_resolver = async_resolver
        # host -> (classification, resolved_at_monotonic)
        self._cache: dict[str, tuple[str, float]] = {}

    # -- resolution seams (overridable for tests) ---------------------------

    def _resolve_sync(self, host: str) -> list[str]:
        if self._resolver is not None:
            return self._resolver(host)
        infos = socket.getaddrinfo(host, None)
        return [info[4][0] for info in infos]

    async def _resolve_async(self, host: str) -> list[str]:
        if self._async_resolver is not None:
            return await self._async_resolver(host)  # type: ignore[misc]
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, None)
        return [info[4][0] for info in infos]

    # -- cache helpers ------------------------------------------------------

    def _cached_fresh(self, host: str) -> str | None:
        entry = self._cache.get(host)
        if entry is not None and (self._clock() - entry[1]) < self._ttl:
            return entry[0]
        return None

    def _last_known_good(self, host: str) -> str | None:
        entry = self._cache.get(host)
        return entry[0] if entry is not None else None

    def _store(self, host: str, cls: str) -> None:
        self._cache[host] = (cls, self._clock())

    # -- public API ---------------------------------------------------------

    def classify_host(self, host: str) -> str:
        """Classify a host (IP literal or name) — synchronous."""
        if not host:
            return WAN
        literal = ip_literal_class(host)
        if literal is not None:
            return literal
        fresh = self._cached_fresh(host)
        if fresh is not None:
            return fresh
        try:
            addrs = self._resolve_sync(host)
        except OSError:
            # Resolver unreachable (e.g. DNS dead in an outage) — keep the last
            # known verdict so a LAN host stays LAN; else conservative WAN.
            lkg = self._last_known_good(host)
            if lkg is not None:
                return lkg
            return WAN
        cls = _classify_addrs(addrs)
        self._store(host, cls)
        return cls

    async def classify_host_async(self, host: str) -> str:
        """Classify a host (IP literal or name) — async, loop-safe resolution."""
        if not host:
            return WAN
        literal = ip_literal_class(host)
        if literal is not None:
            return literal
        fresh = self._cached_fresh(host)
        if fresh is not None:
            return fresh
        try:
            addrs = await self._resolve_async(host)
        except OSError:
            lkg = self._last_known_good(host)
            if lkg is not None:
                return lkg
            return WAN
        cls = _classify_addrs(addrs)
        self._store(host, cls)
        return cls

    def classify_url(self, url: str | None) -> str:
        """Classify a URL by its host. ``None``/empty/host-less → WAN.

        ``None`` is the native-``claude`` endpoint (no base URL → Anthropic's
        public API), which is always WAN by rule.
        """
        host = _host_of(url)
        return WAN if host is None else self.classify_host(host)

    async def classify_url_async(self, url: str | None) -> str:
        """Async variant of :meth:`classify_url`."""
        host = _host_of(url)
        return WAN if host is None else await self.classify_host_async(host)


def _host_of(url: str | None) -> str | None:
    """Extract a hostname from a URL. ``None`` when absent/unparseable.

    Tolerates bare ``host:port`` (no scheme) by retrying with a dummy scheme,
    since ``urlsplit`` only populates ``hostname`` when a scheme is present.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if host is None and "//" not in url:
            host = urlsplit(f"//{url}").hostname
    except ValueError:
        return None
    return host or None


_default: NetClassifier | None = None


def default_classifier() -> NetClassifier:
    """Shared process-wide classifier (cache warms across CC dispatches)."""
    global _default
    if _default is None:
        _default = NetClassifier()
    return _default
