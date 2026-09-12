"""Tests for tracked_task failure observation — emit payload + default bus.

The reflex arc (PR1) extended ``tracked_task``:

- ``task.failed`` events now carry ``error_type`` and ``error_frames``
  (normalized, line-number-free traceback tail) alongside the legacy
  ``task_name``/``error`` fields
- a module-level default event bus makes ALL tracked_task sites emit
  without per-call-site ``event_bus=`` plumbing (explicit arg still wins)

Frame tests use synthetic tracebacks with controlled filenames — the test
file's own path must never be an input (repo dir names differ per install).
"""

from __future__ import annotations

import asyncio

import pytest

from genesis.util.tasks import (
    normalized_frames,
    set_default_event_bus,
    tracked_task,
)


class FakeBus:
    def __init__(self):
        self.events: list[dict] = []

    async def emit(self, subsystem, severity, event_type, message, **details):
        self.events.append(
            {
                "subsystem": subsystem,
                "severity": severity,
                "event_type": event_type,
                "message": message,
                "details": details,
            }
        )


@pytest.fixture(autouse=True)
def _reset_default_bus():
    """The default bus is process-global — never leak it across tests."""
    set_default_event_bus(None)
    yield
    set_default_event_bus(None)


def _raise_at(filename: str, funcname: str, exc_type: type[Exception] = KeyError) -> Exception:
    """Raise inside a function compiled at a controlled fake filename.

    The helper's own frame is stripped from the traceback so assertions see
    ONLY the synthetic frames — this test file's path is install-specific
    (contains ``/genesis/`` here, not on CI) and must never be an input.
    """
    src = f"def {funcname}():\n    raise __EXC__('boom-4217')\n"
    ns: dict = {"__EXC__": exc_type}
    exec(compile(src, filename, "exec"), ns)  # noqa: S102 — test-only synthetic frames
    try:
        ns[funcname]()
    except Exception as exc:  # noqa: BLE001
        assert exc.__traceback__ is not None
        return exc.with_traceback(exc.__traceback__.tb_next)
    raise AssertionError("did not raise")


class TestNormalizedFrames:
    def test_relpath_from_genesis_segment(self):
        exc = _raise_at("/install/src/genesis/memory/sync.py", "_apply_delta")
        assert normalized_frames(exc) == ["memory/sync.py:_apply_delta"]

    def test_no_line_numbers_anywhere(self):
        exc = _raise_at("/install/src/genesis/memory/sync.py", "_apply_delta")
        for frame in normalized_frames(exc):
            assert frame.count(":") == 1  # exactly relpath:funcname, no :lineno

    def test_non_genesis_frames_dropped_when_genesis_present(self):
        # outer frame in stdlib-ish path, inner in genesis → only genesis kept
        def outer():
            ns: dict = {}
            src = "def inner():\n    raise ValueError('x')\n"
            exec(compile(src, "/install/src/genesis/routing/router.py", "exec"), ns)  # noqa: S102
            ns["inner"]()

        try:
            outer()
        except ValueError as exc:
            frames = normalized_frames(exc)
        # this test function's own frame (repo path, install-specific) may or
        # may not carry a /genesis/ segment; the synthetic genesis frame must
        # be the LAST (deepest) either way
        assert frames[-1] == "routing/router.py:inner"

    def test_all_foreign_frames_kept_as_basenames(self):
        exc = _raise_at("/usr/lib/python3.12/threading.py", "run")
        assert normalized_frames(exc) == ["threading.py:run"]

    def test_last_three_only(self):
        src = (
            "def f1():\n    raise KeyError('k')\n"
            "def f2():\n    f1()\n"
            "def f3():\n    f2()\n"
            "def f4():\n    f3()\n"
        )
        ns: dict = {}
        exec(compile(src, "/install/src/genesis/ego/session.py", "exec"), ns)  # noqa: S102
        try:
            ns["f4"]()
        except KeyError as exc:
            frames = normalized_frames(exc)
        # call chain f4→f3→f2→f1 = 4 genesis frames; keep the DEEPEST three
        assert frames == [
            "ego/session.py:f3",
            "ego/session.py:f2",
            "ego/session.py:f1",
        ]

    def test_no_traceback_yields_empty(self):
        assert normalized_frames(KeyError("bare")) == []


async def _fail(exc: Exception):
    raise exc


async def _settle():
    # done-callback runs via call_soon; emit_sync relays via create_task —
    # two scheduling hops need real loop turns
    for _ in range(5):
        await asyncio.sleep(0)


async def _fail_at(filename: str, funcname: str):
    """Coroutine raising inside a function compiled at a fake filename."""
    src = f"def {funcname}():\n    raise KeyError('boom-4217')\n"
    ns: dict = {}
    exec(compile(src, filename, "exec"), ns)  # noqa: S102 — test-only synthetic frames
    ns[funcname]()


class TestEmitPayload:
    async def test_explicit_bus_emits_extended_payload(self):
        bus = FakeBus()
        task = tracked_task(
            _fail_at("/install/src/genesis/memory/sync.py", "_apply_delta"),
            name="mem-sync",
            event_bus=bus,
        )
        with pytest.raises(KeyError):
            await task
        await _settle()

        assert len(bus.events) == 1
        ev = bus.events[0]
        assert ev["event_type"] == "task.failed"
        d = ev["details"]
        assert d["task_name"] == "mem-sync"  # legacy field intact
        assert "boom-4217" in d["error"]  # legacy field intact
        assert d["error_type"] == "KeyError"
        # deepest frame = the synthetic raise site; earlier frames (this test
        # file, the coroutine) may or may not survive the genesis-path filter
        # depending on where the repo lives per install — assert the invariant
        assert d["error_frames"][-1] == "memory/sync.py:_apply_delta"

    async def test_default_bus_fallback(self):
        bus = FakeBus()
        set_default_event_bus(bus)
        task = tracked_task(_fail(ValueError("v")), name="no-arg-site")
        with pytest.raises(ValueError):
            await task
        await _settle()
        assert len(bus.events) == 1
        assert bus.events[0]["details"]["task_name"] == "no-arg-site"

    async def test_explicit_arg_wins_over_default(self):
        default = FakeBus()
        explicit = FakeBus()
        set_default_event_bus(default)
        task = tracked_task(_fail(ValueError("v")), name="t", event_bus=explicit)
        with pytest.raises(ValueError):
            await task
        await _settle()
        assert len(explicit.events) == 1
        assert default.events == []

    async def test_no_bus_anywhere_no_crash(self):
        task = tracked_task(_fail(ValueError("v")), name="t")
        with pytest.raises(ValueError):
            await task
        await _settle()  # nothing to assert — absence of exception is the test

    async def test_default_bus_late_binding(self):
        # a task CREATED before the default bus is wired still emits if it
        # FAILS after wiring — the callback reads the global at fire time
        bus = FakeBus()
        gate: asyncio.Event = asyncio.Event()

        async def fail_after_gate():
            await gate.wait()
            raise ValueError("late")

        task = tracked_task(fail_after_gate(), name="early-task")
        set_default_event_bus(bus)
        gate.set()
        with pytest.raises(ValueError):
            await task
        await _settle()
        assert len(bus.events) == 1

    async def test_success_emits_nothing(self):
        bus = FakeBus()
        set_default_event_bus(bus)

        async def ok():
            return 42

        task = tracked_task(ok(), name="fine")
        assert await task == 42
        await _settle()
        assert bus.events == []

    async def test_cancelled_emits_nothing(self):
        bus = FakeBus()
        set_default_event_bus(bus)

        async def forever():
            await asyncio.sleep(3600)

        task = tracked_task(forever(), name="cancel-me")
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await _settle()
        assert bus.events == []
