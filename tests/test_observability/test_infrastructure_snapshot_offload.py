"""MW-0 A2: the infrastructure snapshot must enumerate CC slots OFF the event
loop. `enumerate_cc_slots` is a ~1s synchronous /proc scan; running it inline on
the snapshot collector (which fires on the shared server loop) starved recall.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import genesis.observability.service_status as service_status
import genesis.observability.snapshots.infrastructure  # noqa: F401 — ensure loaded

# The `snapshots` package re-exports the `infrastructure` FUNCTION, shadowing the
# submodule attribute — fetch the real module object from sys.modules to patch it.
infra_mod = sys.modules["genesis.observability.snapshots.infrastructure"]


@pytest.mark.asyncio
async def test_infrastructure_offloads_cc_slot_enumeration(monkeypatch):
    # Neutralize the network probes so the collector is hermetic + fast.
    monkeypatch.setattr(
        infra_mod,
        "probe_qdrant",
        AsyncMock(return_value=SimpleNamespace(status="healthy", latency_ms=1.0)),
    )
    monkeypatch.setattr(service_status, "probe_qdrant_collections", AsyncMock(return_value={}))

    import genesis.observability.cc_slots as cc_slots_mod

    sentinel_slots = [{"slot": "1", "pid": 1, "rss_mb": 1.0, "status": "healthy"}]
    enum = MagicMock(return_value=sentinel_slots)
    monkeypatch.setattr(cc_slots_mod, "enumerate_cc_slots", enum)

    dispatched = []
    real_to_thread = infra_mod.asyncio.to_thread

    async def spy(fn, *a, **k):
        dispatched.append(fn)
        return await real_to_thread(fn, *a, **k)

    monkeypatch.setattr(infra_mod.asyncio, "to_thread", spy)

    # db/scheduler/state_machine all None → axis updates no-op, no DB queries.
    infra = await infra_mod.infrastructure(None, None, None, None)

    assert enum in dispatched  # enumeration dispatched through to_thread
    enum.assert_called_once()
    assert infra["cc_slots"] == sentinel_slots


def _neutralize_network(monkeypatch):
    monkeypatch.setattr(
        infra_mod,
        "probe_qdrant",
        AsyncMock(return_value=SimpleNamespace(status="healthy", latency_ms=1.0)),
    )
    monkeypatch.setattr(service_status, "probe_qdrant_collections", AsyncMock(return_value={}))


@pytest.mark.asyncio
async def test_infrastructure_offloads_host_metrics_and_container_memory(monkeypatch):
    """CPU/disk/cc_tmp and container-memory /proc reads must be dispatched through
    asyncio.to_thread — not run inline on the event loop."""
    _neutralize_network(monkeypatch)

    dispatched = []
    real_to_thread = infra_mod.asyncio.to_thread

    async def spy(fn, *a, **k):
        dispatched.append(fn)
        return await real_to_thread(fn, *a, **k)

    monkeypatch.setattr(infra_mod.asyncio, "to_thread", spy)
    infra = await infra_mod.infrastructure(None, None, None, None)

    assert infra_mod._collect_host_metrics in dispatched
    assert infra_mod._collect_container_memory in dispatched
    # The offloaded results still land under their original keys.
    assert "cpu" in infra and "disk" in infra and "cc_tmp" in infra
    assert "container_memory" in infra


@pytest.mark.asyncio
async def test_host_metrics_run_off_the_main_thread(monkeypatch):
    """RED guard: reverting the offload runs _collect_host_metrics inline on the
    main (loop) thread → this fails. With the offload it runs on a worker thread."""
    import threading

    _neutralize_network(monkeypatch)
    main = threading.main_thread()
    seen: dict = {}
    real = infra_mod._collect_host_metrics

    def _record_thread():
        seen["thread"] = threading.current_thread()
        return real()

    monkeypatch.setattr(infra_mod, "_collect_host_metrics", _record_thread)
    await infra_mod.infrastructure(None, None, None, None)

    assert seen.get("thread") is not None, "_collect_host_metrics was never called"
    assert seen["thread"] is not main, "host-metrics collection must run off the loop thread"


def test_cpu_delta_baseline_persists_across_calls(monkeypatch):
    """The module-global delta baseline (_last_cpu_reading) must persist across
    calls so the offloaded collector still computes a real delta: first call
    establishes the baseline (used_pct None), second computes a numeric delta
    against it. (Baseline persistence is thread-agnostic — the lock-guarded global
    is what makes it safe once the collector runs on worker threads.)"""
    monkeypatch.setattr(infra_mod, "_last_cpu_reading", None)
    first = infra_mod._collect_host_metrics()
    assert first["cpu"]["used_pct"] is None  # baseline call
    second = infra_mod._collect_host_metrics()
    assert isinstance(second["cpu"]["used_pct"], float)  # delta computed, not re-baselined
