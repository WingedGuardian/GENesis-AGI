"""CCInvoker network preflight (PR-3) — fail fast pre-spawn on a WAN outage.

The preflight raises :class:`CCNetworkOfflineError` *before* the subprocess is
spawned when the sentinel reports a fresh OFFLINE state, the parking lever is
``live``, and the endpoint is WAN — turning a 45-55min hang into a <1s raise.
Everything else falls through to a normal dispatch (fail-safe). netclass runs
for real against IP literals / ``None`` so no test touches DNS.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from genesis.cc.exceptions import CCNetworkOfflineError
from genesis.cc.invoker import CCInvoker
from genesis.cc.types import CCInvocation

# A LAN CC peer (RFC1918 literal — no DNS) vs the native/WAN endpoints.
_LAN_URL = "http://192.168.1.10:1234"
_WAN_URL = "https://8.8.8.8:443"


@pytest.fixture
def invoker():
    return CCInvoker(claude_path="/usr/bin/claude")


def _patch_decision(monkeypatch, value):
    monkeypatch.setattr(
        "genesis.resilience.network_config.parking_decision",
        lambda now=None: value,
    )


@pytest.mark.asyncio
async def test_preflight_parks_wan_native_endpoint(invoker, monkeypatch):
    # base_url None == native `claude` == WAN → parked in live mode.
    _patch_decision(monkeypatch, "park")
    with pytest.raises(CCNetworkOfflineError):
        await invoker._network_preflight(CCInvocation(prompt="x"))


@pytest.mark.asyncio
async def test_preflight_parks_wan_literal_endpoint(invoker, monkeypatch):
    _patch_decision(monkeypatch, "park")
    inv = CCInvocation(prompt="x", anthropic_base_url=_WAN_URL)
    with pytest.raises(CCNetworkOfflineError):
        await invoker._network_preflight(inv)


@pytest.mark.asyncio
async def test_preflight_allows_lan_peer_during_outage(invoker, monkeypatch):
    # A LAN CC peer stays reachable through a WAN outage — must NOT park.
    _patch_decision(monkeypatch, "park")
    inv = CCInvocation(prompt="x", anthropic_base_url=_LAN_URL)
    await invoker._network_preflight(inv)  # no raise


@pytest.mark.asyncio
async def test_preflight_shadow_does_not_park(invoker, monkeypatch):
    _patch_decision(monkeypatch, "shadow")
    await invoker._network_preflight(CCInvocation(prompt="x"))  # WAN, but shadow


@pytest.mark.asyncio
async def test_preflight_normal_does_not_park(invoker, monkeypatch):
    _patch_decision(monkeypatch, "normal")
    await invoker._network_preflight(CCInvocation(prompt="x"))


@pytest.mark.asyncio
async def test_preflight_off_does_not_park(invoker, monkeypatch):
    _patch_decision(monkeypatch, "off")
    await invoker._network_preflight(CCInvocation(prompt="x"))


@pytest.mark.asyncio
async def test_preflight_failsafe_on_gate_error(invoker, monkeypatch):
    # Any error inside the gate must fall through to a normal dispatch.
    def _boom(now=None):
        raise RuntimeError("gate broke")

    monkeypatch.setattr("genesis.resilience.network_config.parking_decision", _boom)
    await invoker._network_preflight(CCInvocation(prompt="x"))  # swallowed


@pytest.mark.asyncio
async def test_run_raises_before_spawning_subprocess(invoker, monkeypatch):
    # The load-bearing property: run() short-circuits BEFORE _run_inner (which
    # spawns the subprocess). A dead-network dispatch fails in <1s, not 7200s.
    _patch_decision(monkeypatch, "park")
    monkeypatch.setattr(
        "genesis.cc.invoker.roster.apply_active",
        lambda inv: (inv, "claude"),
    )
    spawn_spy = AsyncMock()
    monkeypatch.setattr(invoker, "_run_inner", spawn_spy)

    with pytest.raises(CCNetworkOfflineError):
        await invoker.run(CCInvocation(prompt="x"))
    spawn_spy.assert_not_awaited()  # never reached the subprocess path


@pytest.mark.asyncio
async def test_run_streaming_raises_before_spawning_subprocess(invoker, monkeypatch):
    _patch_decision(monkeypatch, "park")
    monkeypatch.setattr(
        "genesis.cc.invoker.roster.apply_active",
        lambda inv: (inv, "claude"),
    )
    spawn_spy = AsyncMock()
    monkeypatch.setattr(invoker, "_run_streaming_inner", spawn_spy)

    with pytest.raises(CCNetworkOfflineError):
        await invoker.run_streaming(CCInvocation(prompt="x"))
    spawn_spy.assert_not_awaited()
