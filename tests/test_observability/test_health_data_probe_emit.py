"""Integration: HealthDataService drives the probe-transition tracker and emits
one activity event per healthy<->unhealthy crossing, with a whole-infra storm
guard and emit-failure isolation."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from genesis.observability.health_data import HealthDataService
from genesis.observability.probe_transitions import ProbeTransitionTracker


def _bus() -> AsyncMock:
    bus = AsyncMock()
    bus.emit = AsyncMock()
    return bus


def _svc(bus) -> HealthDataService:
    """Service with a zero-warmup tracker so crossings emit immediately in tests
    (production uses the 90s startup grace to swallow the restart transient)."""
    svc = HealthDataService(event_bus=bus)
    svc._probe_tracker = ProbeTransitionTracker(warmup=timedelta(0))
    return svc


@pytest.mark.asyncio
async def test_healthy_to_down_emits_one_event():
    bus = _bus()
    svc = _svc(bus)
    # First pass seeds (no emit); second pass crosses to unhealthy → one emit.
    await svc._emit_probe_transitions({"genesis.db": {"status": "healthy"}})
    assert bus.emit.await_count == 0
    await svc._emit_probe_transitions({"genesis.db": {"status": "down"}})
    assert bus.emit.await_count == 1
    args, kwargs = bus.emit.await_args
    # subsystem, severity, event_type, message positional; details as kwargs.
    assert args[2] == "probe_transition"
    assert kwargs["probe"] == "genesis.db"
    assert kwargs["from"] == "healthy"
    assert kwargs["to"] == "down"


@pytest.mark.asyncio
async def test_no_bus_is_noop():
    svc = HealthDataService(event_bus=None)
    # Must not raise despite no bus.
    await svc._emit_probe_transitions({"genesis.db": {"status": "healthy"}})
    await svc._emit_probe_transitions({"genesis.db": {"status": "down"}})


@pytest.mark.asyncio
async def test_whole_infra_error_is_storm_guarded():
    """A transient snapshot error (_or_error injects top-level status=error) must
    NOT be read as every probe going down at once."""
    bus = _bus()
    svc = _svc(bus)
    # Seed everyone healthy.
    await svc._emit_probe_transitions(
        {p: {"status": "healthy"} for p in ("genesis.db", "qdrant", "guardian")}
    )
    assert bus.emit.await_count == 0
    # Whole-section failure → skip entirely, no N-probe false outage.
    await svc._emit_probe_transitions({"status": "error", "error": "boom"})
    assert bus.emit.await_count == 0
    # And the skip did not corrupt state: a real recovery reading is still healthy
    # (no spurious down→healthy), so the next genuine down still emits exactly one.
    await svc._emit_probe_transitions({"genesis.db": {"status": "down"}})
    assert bus.emit.await_count == 1


@pytest.mark.asyncio
async def test_non_dict_infra_is_ignored():
    bus = _bus()
    svc = _svc(bus)
    await svc._emit_probe_transitions(["not", "a", "dict"])
    await svc._emit_probe_transitions(None)
    assert bus.emit.await_count == 0


@pytest.mark.asyncio
async def test_untracked_probe_never_emits():
    """cpu/disk/cc_slots are deliberately outside _TRACKED_PROBES."""
    bus = _bus()
    svc = _svc(bus)
    await svc._emit_probe_transitions({"cpu": {"status": "healthy"}})
    await svc._emit_probe_transitions({"cpu": {"status": "down"}})
    assert bus.emit.await_count == 0


@pytest.mark.asyncio
async def test_missing_or_falsy_status_skipped():
    bus = _bus()
    svc = _svc(bus)
    # disk has no status field on success; entry without status must be skipped.
    await svc._emit_probe_transitions({"genesis.db": {"latency_ms": 1.2}})
    await svc._emit_probe_transitions({"genesis.db": {"status": ""}})
    assert bus.emit.await_count == 0


@pytest.mark.asyncio
async def test_emit_failure_does_not_propagate():
    """A raising event bus must never poison the snapshot pass."""
    bus = _bus()
    bus.emit = AsyncMock(side_effect=RuntimeError("bus down"))
    svc = _svc(bus)
    await svc._emit_probe_transitions({"qdrant": {"status": "healthy"}})
    # Crossing triggers an emit that raises — must be swallowed.
    await svc._emit_probe_transitions({"qdrant": {"status": "error"}})
    assert bus.emit.await_count == 1  # attempted once, exception swallowed


@pytest.mark.asyncio
async def test_recovery_is_info_and_hard_down_is_error():
    from genesis.observability.types import Severity

    bus = _bus()
    svc = _svc(bus)
    await svc._emit_probe_transitions({"qdrant": {"status": "healthy"}})
    await svc._emit_probe_transitions({"qdrant": {"status": "down"}})
    assert bus.emit.await_args.args[1] == Severity.ERROR
    await svc._emit_probe_transitions({"qdrant": {"status": "healthy"}})
    assert bus.emit.await_args.args[1] == Severity.INFO
