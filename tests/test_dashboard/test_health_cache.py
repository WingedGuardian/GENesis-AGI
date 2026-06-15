"""Tests for the /api/genesis/health snapshot cache (TTL).

The full snapshot is ~2s; the route caches it briefly so frequent polls are
served instantly and don't recompute every request. These tests exercise the
undecorated coroutine (`__wrapped__`) directly so we avoid the cross-thread
event-loop machinery of _async_route and just verify the cache logic.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.routes import health as health_route


def _fake_runtime(counter: dict):
    async def _snapshot():
        counter["n"] += 1
        return {"infrastructure": {"genesis.db": {"status": "healthy"}}, "timestamp": "t"}

    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.health_data = MagicMock()
    rt.health_data.snapshot = _snapshot
    return rt


@pytest.mark.asyncio
async def test_cache_hit_within_ttl_computes_once():
    health_route._snapshot_cache = None
    health_route._snapshot_cache_ts = 0.0
    calls = {"n": 0}
    rt = _fake_runtime(calls)
    app = Flask(__name__)

    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = rt
        with app.test_request_context("/api/genesis/health"):
            await health_route.health_snapshot.__wrapped__()
            await health_route.health_snapshot.__wrapped__()

    # Two requests within the TTL → snapshot() computed exactly once.
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_cache_recomputes_after_ttl():
    health_route._snapshot_cache = None
    health_route._snapshot_cache_ts = 0.0
    calls = {"n": 0}
    rt = _fake_runtime(calls)
    app = Flask(__name__)

    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = rt
        with app.test_request_context("/api/genesis/health"):
            await health_route.health_snapshot.__wrapped__()
            # Force the cache to look stale (older than the TTL).
            health_route._snapshot_cache_ts -= health_route._SNAPSHOT_CACHE_TTL_S + 1
            await health_route.health_snapshot.__wrapped__()

    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_cache_does_not_taint_on_per_request_mutation():
    """Bridge/status added per request must not pollute the cached snapshot."""
    health_route._snapshot_cache = None
    health_route._snapshot_cache_ts = 0.0
    calls = {"n": 0}
    rt = _fake_runtime(calls)
    app = Flask(__name__)

    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = rt
        with app.test_request_context("/api/genesis/health"):
            await health_route.health_snapshot.__wrapped__()
            # The cached snapshot should not carry the per-request "bridge"/"status"
            assert "bridge" not in health_route._snapshot_cache
            assert "status" not in health_route._snapshot_cache
