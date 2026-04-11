"""Tests for awareness loop dropped-tick observability.

APScheduler can silently skip tick jobs when `max_instances=1` is reached
or when a run is missed past the misfire grace time. These paths used to
be invisible — if the awareness tick is dropped, signal collection,
reflection, and Sentinel checks all skip for that cycle. The listener in
AwarenessLoop.start() converts these into ERROR events on the event bus.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from apscheduler.events import EVENT_JOB_MAX_INSTANCES, EVENT_JOB_MISSED

from genesis.awareness.loop import AwarenessLoop


@pytest.mark.asyncio
async def test_max_instances_emits_error_event(db):
    bus = AsyncMock()
    loop = AwarenessLoop(db, [], event_bus=bus)

    await loop._emit_tick_drop_event(EVENT_JOB_MAX_INSTANCES)

    bus.emit.assert_awaited_once()
    args = bus.emit.await_args.args
    # args = (subsystem, severity, event_type, message)
    assert args[2] == "tick.max_instances"
    assert "max_instances" in args[3]


@pytest.mark.asyncio
async def test_missed_emits_error_event(db):
    bus = AsyncMock()
    loop = AwarenessLoop(db, [], event_bus=bus)

    await loop._emit_tick_drop_event(EVENT_JOB_MISSED)

    bus.emit.assert_awaited_once()
    args = bus.emit.await_args.args
    assert args[2] == "tick.missed"


@pytest.mark.asyncio
async def test_no_event_bus_is_noop(db):
    """When event_bus is None, emit is skipped silently."""
    loop = AwarenessLoop(db, [], event_bus=None)
    # Should not raise.
    await loop._emit_tick_drop_event(EVENT_JOB_MISSED)


def test_scheduler_event_listener_filters_by_job_id(db):
    """Listener ignores events for jobs other than awareness_tick."""
    loop = AwarenessLoop(db, [], event_bus=AsyncMock())
    # No event loop reference set — hand-off should not crash on a no-op.
    loop._tick_event_loop = None
    event = SimpleNamespace(job_id="some_other_job", code=EVENT_JOB_MISSED)
    # Should not raise.
    loop._on_scheduler_job_event(event)
