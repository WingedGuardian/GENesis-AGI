"""The reflex funnel — record_job_failure emits a throttled job.failed event.

Background-job exceptions were, for months, written only to the job_health table
and never to the event bus, so the reflex arc (a bus subscriber) could not see
the single largest class of internal Genesis defects. record_job_failure now
emits a job.failed event when — and only when — an exception caused the failure,
sharing the existing streak-onset/hourly-heartbeat throttle.

These tests pin the four behaviours that keep the funnel safe: it fires on the
exception path, it stays silent on the semantic path and when a caller opts out,
it respects the throttle, and it never raises (a broken emit must never stop the
job_health record).
"""

from __future__ import annotations

import asyncio
import types

import pytest

from genesis.observability.events import GenesisEventBus
from genesis.observability.types import Severity
from genesis.runtime._job_health import record_job_failure


def _make_rt(bus):
    """A minimal runtime stand-in with just the attributes the funnel reads."""
    return types.SimpleNamespace(
        _job_health={},
        _event_bus=bus,
        _job_retry_registry=None,
        _db=None,  # _append_job_run_event no-ops without a DB
        _persist_job_health=lambda *a, **k: None,
    )


async def _collect(bus):
    seen: list = []

    async def _handler(ev):
        seen.append(ev)

    bus.subscribe(_handler, min_severity=Severity.ERROR)
    return seen


def _boom() -> ValueError:
    try:
        raise ValueError("kaboom")
    except ValueError as exc:
        return exc


@pytest.mark.asyncio
async def test_exception_path_emits_job_failed_with_type_and_frames():
    bus = GenesisEventBus()
    seen = await _collect(bus)
    rt = _make_rt(bus)

    record_job_failure(rt, "prune_job", exc=_boom())
    await asyncio.sleep(0.05)  # let emit_sync's scheduled task run

    assert len(seen) == 1
    ev = seen[0]
    assert ev.event_type == "job.failed"
    assert ev.details["task_name"] == "prune_job"
    assert ev.details["error_type"] == "ValueError"
    assert ev.details["error_frames"]  # non-empty
    # job_health carries the typed error too
    assert rt._job_health["prune_job"]["error_type"] == "ValueError"


@pytest.mark.asyncio
async def test_semantic_path_does_not_emit():
    bus = GenesisEventBus()
    seen = await _collect(bus)
    rt = _make_rt(bus)

    record_job_failure(rt, "quota_job", "429 weekly limit")  # no exc
    await asyncio.sleep(0.05)

    assert seen == []
    # ...but a semantic failure still records to job_health (no error_type)
    assert rt._job_health["quota_job"]["error_type"] is None


@pytest.mark.asyncio
async def test_emit_event_false_suppresses_funnel():
    """Callers that emit their own domain .failed opt out to avoid double-count."""
    bus = GenesisEventBus()
    seen = await _collect(bus)
    rt = _make_rt(bus)

    record_job_failure(rt, "sched_job", exc=_boom(), emit_event=False)
    await asyncio.sleep(0.05)

    assert seen == []


@pytest.mark.asyncio
async def test_throttle_suppresses_repeat_within_the_hour():
    bus = GenesisEventBus()
    seen = await _collect(bus)
    rt = _make_rt(bus)

    record_job_failure(rt, "flaky_job", exc=_boom())  # streak onset → emits
    record_job_failure(rt, "flaky_job", exc=_boom())  # same hour → throttled
    await asyncio.sleep(0.05)

    assert len(seen) == 1


@pytest.mark.asyncio
async def test_bare_runtime_without_event_bus_never_raises():
    """A runtime built via __new__ has no _event_bus attribute — must not raise."""
    rt = types.SimpleNamespace(
        _job_health={},
        _job_retry_registry=None,
        _db=None,
        _persist_job_health=lambda *a, **k: None,
    )  # note: NO _event_bus attribute at all

    # Must not raise; job_health still recorded.
    record_job_failure(rt, "bare_job", exc=_boom())
    assert rt._job_health["bare_job"]["error_type"] == "ValueError"


def test_job_failed_is_reflex_owned_so_ego_defers_it():
    """The ego must NOT spin a reactive cycle per job failure — reflex owns it."""
    from genesis.runtime.init.ego import _is_reflex_owned_event

    assert _is_reflex_owned_event("job.failed") is True
    assert _is_reflex_owned_event("task.failed") is True
    # a normal ERROR event is still routed to the ego
    assert _is_reflex_owned_event("some.other_error") is False
