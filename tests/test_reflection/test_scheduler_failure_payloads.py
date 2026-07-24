"""Emitter-level proof that job failures carry a diagnosable payload.

``tests/test_observability/test_failure_details.py`` covers the pure payload
builder. This file covers the WIRING: that a real scheduler failure actually
reaches the event bus with ``error_type`` + frames, and that a semantic failure
does not — the distinction downstream classification depends on.

``weekly_assessment.failed`` is the motivating case: live data shows it emitted
for BOTH a genuine ``TypeError`` in Genesis code AND a provider 429 quota block,
so the two must be distinguishable without parsing the message.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables, seed_data
from genesis.perception.types import ReflectionResult
from genesis.reflection.scheduler import ReflectionScheduler

# A real provider quota response, shaped as observed live.
_QUOTA_REASON = (
    '{"type":"result","subtype":"success","is_error":true,'
    '"api_error_status":429,"result":"You\'ve hit your weekly limit"}'
)


class _RecordingBus:
    """Captures emitted events (the bus dispatches listeners inline)."""

    def __init__(self) -> None:
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

    def failures(self) -> list[dict]:
        return [e for e in self.events if e["event_type"].endswith(".failed")]


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await seed_data(conn)
        yield conn


@pytest.fixture
def bus():
    return _RecordingBus()


def _scheduler(db, bus, bridge):
    return ReflectionScheduler(bridge=bridge, stability_monitor=None, db=db, event_bus=bus)


@pytest.fixture(autouse=True)
def _stub_runtime(monkeypatch):
    """record_job_failure reaches a real GenesisRuntime singleton; capture instead."""
    calls: list[dict] = []
    rt = MagicMock()
    # MUST be explicitly False — a bare MagicMock attribute is truthy, and
    # _run_assessment returns early when the runtime reports paused.
    rt.paused = False
    rt.record_job_failure = MagicMock(
        side_effect=lambda name, error, error_type=None: calls.append(
            {"name": name, "error": error, "error_type": error_type}
        )
    )
    rt.record_job_success = MagicMock()

    import genesis.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod.GenesisRuntime, "instance", classmethod(lambda cls: rt))
    rt.calls = calls
    return rt


class TestExceptionPath:
    @pytest.mark.asyncio
    async def test_exception_failure_carries_type_and_frames(self, db, bus, _stub_runtime):
        bridge = AsyncMock()
        bridge.run_weekly_assessment = AsyncMock(
            side_effect=TypeError("float() argument must be a string or a real number")
        )
        await _scheduler(db, bus, bridge)._run_assessment()

        failures = bus.failures()
        assert len(failures) == 1
        details = failures[0]["details"]
        assert failures[0]["event_type"] == "weekly_assessment.failed"
        assert details["error_type"] == "TypeError"
        assert details["error_frames"], "an internal defect must carry frames to diagnose"
        assert "error_reason" not in details

    @pytest.mark.asyncio
    async def test_exception_type_reaches_job_health(self, db, bus, _stub_runtime):
        """job_health.last_error recorded "" for months — the type must survive."""
        bridge = AsyncMock()
        bridge.run_weekly_assessment = AsyncMock(side_effect=ValueError())
        await _scheduler(db, bus, bridge)._run_assessment()

        call = _stub_runtime.calls[0]
        assert call["error_type"] == "ValueError"
        assert call["error"].startswith("ValueError:"), "blank str(exc) must still be diagnosable"


class TestMessageDetail:
    @pytest.mark.asyncio
    async def test_exc_only_never_renders_none(self, db, bus, _stub_runtime):
        """Passing only *exc* (no *error*) must not render "failed: None" — the
        message and job_health.last_error both come from one detail string."""
        sched = _scheduler(db, bus, AsyncMock())
        await sched._record_job_result("weekly_assessment", exc=TypeError("bad float"))

        failures = bus.failures()
        assert len(failures) == 1
        assert "None" not in failures[0]["message"]
        assert "TypeError: bad float" in failures[0]["message"]
        # Both sinks agree.
        assert _stub_runtime.calls[0]["error"] == "TypeError: bad float"


class TestSemanticPath:
    @pytest.mark.asyncio
    async def test_quota_block_carries_no_error_type(self, db, bus, _stub_runtime):
        """An external blocker must not look like an internal defect."""
        bridge = AsyncMock()
        bridge.run_weekly_assessment = AsyncMock(
            return_value=ReflectionResult(success=False, reason=_QUOTA_REASON)
        )
        await _scheduler(db, bus, bridge)._run_assessment()

        failures = bus.failures()
        assert len(failures) == 1
        details = failures[0]["details"]
        assert "error_type" not in details
        assert "error_frames" not in details
        assert "429" in details["error_reason"]
        assert _stub_runtime.calls[0]["error_type"] is None

    @pytest.mark.asyncio
    async def test_same_event_type_both_dispositions(self, db, bus, _stub_runtime):
        """The core finding: one event_type carries both kinds, so an event-type
        allowlist cannot classify them — only the payload can."""
        bridge = AsyncMock()
        bridge.run_weekly_assessment = AsyncMock(side_effect=TypeError("boom"))
        await _scheduler(db, bus, bridge)._run_assessment()
        bridge.run_weekly_assessment = AsyncMock(
            return_value=ReflectionResult(success=False, reason=_QUOTA_REASON)
        )
        await _scheduler(db, bus, bridge)._run_assessment()

        failures = bus.failures()
        assert [f["event_type"] for f in failures] == [
            "weekly_assessment.failed",
            "weekly_assessment.failed",
        ]
        assert "error_type" in failures[0]["details"]
        assert "error_type" not in failures[1]["details"]


class TestSuccessPathUnaffected:
    @pytest.mark.asyncio
    async def test_success_emits_no_failure_event(self, db, bus, _stub_runtime):
        bridge = AsyncMock()
        bridge.run_weekly_assessment = AsyncMock(
            return_value=ReflectionResult(success=True, reason="done")
        )
        await _scheduler(db, bus, bridge)._run_assessment()

        assert bus.failures() == []
        assert _stub_runtime.calls == []
