"""Stdlib HTTP routing for Guardian's configured health target."""

from __future__ import annotations

import ipaddress
import urllib.parse
import urllib.request


def opener_for_url(url: str) -> urllib.request.OpenerDirector:
    """Bypass environment proxies only for numeric loopback destinations."""
    try:
        hostname = urllib.parse.urlsplit(url).hostname
        loopback = hostname is not None and ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = False
    if loopback:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()
