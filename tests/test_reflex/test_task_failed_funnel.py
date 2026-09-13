"""The task.failed funnel — the REAL producer through the REAL consumer.

`tests/test_reflex/test_ingest.py` exercises the ingestor with hand-built
events, and its fixture sets `error_type` — a field the real `task.failed`
producer never set. That test was green from day one while the real event
could never enqueue anything: `surplus/dispatch.py` emitted only
`task_id`/`task_type`, so `ReflexIngestor.handle_event` dropped it at the
`if not error_type: return` admission gate. A hand-written intermediate is
the bug's hiding place (genesis-development skill, fixture rule).

So this file drives `_handle_failure` — the actual emit site — through a real
`GenesisEventBus` into the actual `ReflexIngestor.handle_event`, the same
shape `tests/test_runtime/test_job_failure_funnel.py` uses for `job.failed`
(which is why job.failed worked all along and task.failed didn't).
"""

from __future__ import annotations

import types

import pytest

from genesis.observability.events import GenesisEventBus
from genesis.observability.types import Severity
from genesis.reflex.ingest import ReflexIngestor
from genesis.surplus.dispatch import _handle_failure


def _boom() -> ValueError:
    try:
        raise ValueError("kaboom")
    except ValueError as exc:
        return exc


class _QueueStub:
    async def mark_failed(self, task_id, *, reason):
        self.marked = (task_id, reason)


def _sched(bus) -> types.SimpleNamespace:
    """Minimal DispatchContext stand-in with only what _handle_failure reads.

    `_db=None` makes maybe_observe_failure's guarded lookup raise and be
    swallowed, and no GenesisRuntime exists so the autonomy-correction block
    is likewise swallowed — both by their own try/except, not by this test.
    """
    return types.SimpleNamespace(_queue=_QueueStub(), _event_bus=bus, _db=None)


def _task() -> types.SimpleNamespace:
    return types.SimpleNamespace(id="t-123", task_type="research")


def _live_ingestor(bus) -> ReflexIngestor:
    """A real ingestor, bus-subscribed exactly as start() subscribes it,
    without the drain worker (the admission gate under test is bus-side)."""
    ing = ReflexIngestor(db=None)
    ing._enabled = True
    bus.subscribe(ing.handle_event, min_severity=Severity.ERROR)
    return ing


@pytest.mark.asyncio
async def test_executor_exception_reaches_the_reflex_queue():
    """The whole admission contract at once: severity clears the subscriber
    floor AND the payload carries error_type, so the event survives
    `handle_event`'s admission gate and lands in the reflex queue.

    VERIFY-RED (run before the fix): with the emit not routed through
    failure_details, the event arrived at handle_event and was dropped at the
    `if not error_type` gate — this test failed with an empty queue.
    """
    bus = GenesisEventBus()
    ing = _live_ingestor(bus)
    sched = _sched(bus)

    await _handle_failure(sched, _task(), "executor_exception", emit_event=True, exc=_boom())

    assert ing._queue.qsize() == 1, (
        "task.failed did not survive the reflex admission gate — either the "
        "severity fell below the subscriber floor or the payload lacks error_type"
    )
    payload = ing._queue.get_nowait()
    assert payload["error_type"] == "ValueError"
    assert payload["error_frames"], "frames empty — fingerprinting has nothing to key on"
    assert payload["task_name"] == "research"
    # and the queue-side bookkeeping still happened
    assert sched._queue.marked == ("t-123", "executor_exception")


@pytest.mark.asyncio
async def test_semantic_failure_does_not_emit_at_all():
    """The result.success=False path keeps its emit_event=False contract:
    nothing reaches the bus, so the reflex lane split (exception-only) holds
    at the producer, not just at the consumer's guard."""
    bus = GenesisEventBus()
    ing = _live_ingestor(bus)
    sched = _sched(bus)

    await _handle_failure(sched, _task(), "unknown", emit_event=False)

    assert ing._queue.qsize() == 0
