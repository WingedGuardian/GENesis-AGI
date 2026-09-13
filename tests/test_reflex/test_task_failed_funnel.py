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


@pytest.fixture(autouse=True)
def _no_runtime_leak():
    """Preserve and restore the process-global GenesisRuntime singleton.

    `_handle_failure`'s autonomy-correction block calls
    `GenesisRuntime.instance()`, which CONSTRUCTS AND STORES a blank singleton
    when none exists (`runtime/_core.py` `instance`) rather than raising. An
    earlier version of this file's docstring asserted the opposite, so these
    tests would have leaked a blank runtime into every test that ran after
    them in the same process — making those collection-order dependent
    (Codex P3, #1941). Restoring the exact prior value keeps the leak local
    whether or not a real runtime already exists.
    """
    from genesis.runtime._core import GenesisRuntime

    saved = GenesisRuntime._instance
    # Cleared, not merely saved: `_sched`'s docstring below claims the
    # autonomy-correction block does nothing, which is true only when no
    # singleton pre-exists. A runtime left by an earlier test in the same
    # process carries a real `_autonomy_manager`, and the block would then
    # perform a real `record_correction` write — swallowed, so the test still
    # passes, but it is a stray write and a collection-order dependency. This
    # makes the claim unconditional.
    GenesisRuntime._instance = None
    try:
        yield
    finally:
        GenesisRuntime._instance = saved


def _sched(bus) -> types.SimpleNamespace:
    """Minimal DispatchContext stand-in with only what _handle_failure reads.

    `_db=None` makes maybe_observe_failure's guarded lookup raise and be
    swallowed by its own try/except. The autonomy-correction block does NOT
    raise — `GenesisRuntime.instance()` lazily builds a blank singleton, whose
    `_autonomy_manager` is None, so the block simply does nothing; the
    `_no_runtime_leak` fixture above contains the singleton it leaves behind.
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
