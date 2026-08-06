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
