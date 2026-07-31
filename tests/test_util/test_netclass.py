"""netclass — LAN/WAN classification: literals, resolution, cache, fallbacks."""

from __future__ import annotations

import asyncio

import pytest

from genesis.util import netclass
from genesis.util.netclass import LOCAL, WAN, NetClassifier

# ── IP-literal fast-path (no DNS) ──────────────────────────────────────


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # RFC1918 /8 (e.g. a LAN Ollama host)
        "172.16.5.4",  # RFC1918 /12
        "192.168.1.10",  # RFC1918 /16 (e.g. a LAN LM Studio host)
        "100.64.0.1",  # CGNAT / Tailscale mesh v4
        "169.254.1.1",  # link-local
        "::1",  # v6 loopback
        "fc00::1",  # v6 ULA
        "fd00::1",  # v6 ULA (Tailscale-style fd.. mesh, under fc00::/7)
        "fe80::1",  # v6 link-local
        "[::1]",  # bracketed v6 (URL form)
    ],
)
def test_local_ip_literals(ip):
    assert NetClassifier().classify_host(ip) == LOCAL


@pytest.mark.parametrize(
    "ip",
    ["8.8.8.8", "1.1.1.1", "9.9.9.9", "2606:4700:4700::1111", "203.0.113.7"],
)
def test_wan_ip_literals(ip):
    assert NetClassifier().classify_host(ip) == WAN


def test_ip_literal_class_returns_none_for_hostname():
    assert netclass.ip_literal_class("example.com") is None
    assert netclass.ip_literal_class("cc-peer.local") is None


# ── URL parsing ────────────────────────────────────────────────────────


def test_classify_url_none_is_wan():
    # None base URL == native `claude` endpoint == Anthropic public API == WAN.
    assert NetClassifier().classify_url(None) == WAN


def test_classify_url_empty_is_wan():
    assert NetClassifier().classify_url("") == WAN


def test_classify_url_lan_literal():
    assert NetClassifier().classify_url("http://192.168.1.10:1234/v1") == LOCAL


def test_classify_url_wan_literal():
    assert NetClassifier().classify_url("https://8.8.8.8:443") == WAN


def test_classify_url_bare_hostport_no_scheme():
    # `host:port` with no scheme still yields the host (LAN literal here).
    assert NetClassifier().classify_url("10.0.0.5:8080") == LOCAL


# ── Hostname resolution (injected resolver — no real DNS) ───────────────


def test_hostname_resolves_local():
    nc = NetClassifier(resolver=lambda h: ["192.168.1.10"])
    assert nc.classify_host("nas.local") == LOCAL


def test_hostname_resolves_wan():
    nc = NetClassifier(resolver=lambda h: ["203.0.113.9"])
    assert nc.classify_host("api.example.com") == WAN


def test_mixed_addrs_default_to_wan():
    # ALL-local rule: any WAN address in the set → WAN (ambiguity → conservative).
    nc = NetClassifier(resolver=lambda h: ["192.168.1.10", "203.0.113.9"])
    assert nc.classify_host("mixed.example") == WAN


def test_empty_resolution_is_wan():
    nc = NetClassifier(resolver=lambda h: [])
    assert nc.classify_host("void.example") == WAN


# ── Cache: freshness + last-known-good survival ────────────────────────


def test_cache_avoids_second_resolution_while_fresh():
    calls = {"n": 0}

    def _res(h):
        calls["n"] += 1
        return ["192.168.1.10"]

    clock = {"t": 1000.0}
    nc = NetClassifier(resolver=_res, clock=lambda: clock["t"], cache_ttl_s=300)
    assert nc.classify_host("nas.local") == LOCAL
    assert nc.classify_host("nas.local") == LOCAL  # within TTL → cached
    assert calls["n"] == 1


def test_cache_re_resolves_after_ttl():
    calls = {"n": 0}

    def _res(h):
        calls["n"] += 1
        return ["192.168.1.10"]

    clock = {"t": 1000.0}
    nc = NetClassifier(resolver=_res, clock=lambda: clock["t"], cache_ttl_s=300)
    assert nc.classify_host("nas.local") == LOCAL
    clock["t"] = 1000.0 + 301  # past TTL
    assert nc.classify_host("nas.local") == LOCAL
    assert calls["n"] == 2


def test_last_known_good_survives_resolver_failure():
    # A LAN host classified once must STAY local when DNS later dies (outage).
    state = {"fail": False}

    def _res(h):
        if state["fail"]:
            raise OSError("name resolution failed")
        return ["192.168.1.10"]

    clock = {"t": 1000.0}
    nc = NetClassifier(resolver=_res, clock=lambda: clock["t"], cache_ttl_s=1)
    assert nc.classify_host("nas.local") == LOCAL  # warm the cache
    state["fail"] = True
    clock["t"] = 1000.0 + 100  # past TTL so it re-resolves → fails → falls back to LKG
    assert nc.classify_host("nas.local") == LOCAL


def test_unresolvable_no_cache_is_wan():
    def _res(h):
        raise OSError("dead")

    nc = NetClassifier(resolver=_res)
    assert nc.classify_host("never-seen.example") == WAN


# ── Async path ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_literal_and_none():
    nc = NetClassifier()
    assert await nc.classify_url_async(None) == WAN
    assert await nc.classify_url_async("http://192.168.1.10:1234") == LOCAL
    assert await nc.classify_host_async("8.8.8.8") == WAN


@pytest.mark.asyncio
async def test_async_resolver_injected():
    async def _ares(h):
        return ["10.0.0.9"]

    nc = NetClassifier(async_resolver=_ares)
    assert await nc.classify_host_async("box.local") == LOCAL


@pytest.mark.asyncio
async def test_async_resolver_failure_falls_back_to_wan():
    async def _ares(h):
        raise OSError("dead")

    nc = NetClassifier(async_resolver=_ares)
    assert await nc.classify_host_async("never.example") == WAN


def test_default_classifier_is_singleton():
    assert netclass.default_classifier() is netclass.default_classifier()


# ── Bounded resolution (dead resolver must not stall the preflight) ─────


@pytest.mark.asyncio
async def test_async_resolution_bounded_by_timeout_no_cache_is_wan():
    # A resolver that hangs (dead DNS during an outage) must NOT hang the
    # preflight — it times out and falls to the conservative WAN verdict.
    async def _hang(h):
        await asyncio.sleep(30)
        return ["192.168.1.10"]

    nc = NetClassifier(async_resolver=_hang, resolve_timeout_s=0.05)
    assert await nc.classify_host_async("cold.example") == WAN


@pytest.mark.asyncio
async def test_async_resolution_timeout_falls_back_to_last_known_good():
    state = {"hang": False}

    async def _res(h):
        if state["hang"]:
            await asyncio.sleep(30)
            return []
        return ["192.168.1.10"]

    # cache_ttl_s=0 forces a re-resolve on the second call.
    nc = NetClassifier(async_resolver=_res, resolve_timeout_s=0.05, cache_ttl_s=0)
    assert await nc.classify_host_async("nas.local") == LOCAL  # warm the cache
    state["hang"] = True
    assert await nc.classify_host_async("nas.local") == LOCAL  # timeout → LKG
