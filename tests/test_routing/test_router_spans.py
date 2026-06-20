"""A2: route_call LLM-span instrumentation.

Every route_call emits one `llm` span populated from the returned RoutingResult.
Includes the critical-path proof: a span fault (writer raising) must NEVER break
routing — span capture is best-effort and off the hot path.
"""

from __future__ import annotations

import contextlib
import json

import aiosqlite
import pytest

from genesis.observability import spans as spans_mod
from genesis.observability.span_writer import SpanWriter
from genesis.routing.circuit_breaker import CircuitBreakerRegistry
from genesis.routing.cost_tracker import CostTracker
from genesis.routing.degradation import DegradationTracker
from genesis.routing.router import Router
from genesis.routing.types import CallResult

from .conftest import MockDelegate

_MSG = [{"role": "user", "content": "hi"}]


@pytest.fixture
async def wired_router(sample_config, sample_providers, db):
    """Router whose cost_tracker + a SpanWriter share one DB (with otel_spans)."""
    writer = SpanWriter()
    writer.set_db(db, process="test")
    spans_mod.set_writer(writer, enabled=True)
    delegate = MockDelegate()
    router = Router(
        config=sample_config,
        breakers=CircuitBreakerRegistry(sample_providers),
        cost_tracker=CostTracker(db),
        degradation=DegradationTracker(),
        delegate=delegate,
    )
    try:
        yield router, writer, db, delegate
    finally:
        spans_mod.set_writer(None)


async def _drain(writer: SpanWriter) -> None:
    if writer._flush_task is not None:
        with contextlib.suppress(Exception):
            await writer._flush_task
    await writer._flush_to_db()


async def _spans(db) -> list[dict]:
    db.row_factory = aiosqlite.Row
    cur = await db.execute("SELECT * FROM otel_spans ORDER BY start_unix_us")
    return [dict(r) for r in await cur.fetchall()]


@pytest.mark.asyncio
async def test_success_emits_llm_span(wired_router) -> None:
    router, writer, db, _ = wired_router
    result = await router.route_call("test_mixed", _MSG)
    assert result.success is True
    await _drain(writer)

    rows = await _spans(db)
    assert len(rows) == 1
    s = rows[0]
    assert s["name"] == "llm.call"
    assert s["kind"] == "llm"
    assert s["status"] == "ok"
    assert s["call_site"] == "test_mixed"
    assert s["provider"] == result.provider_used == "free-1"
    assert s["model_id"] == result.model_id
    # Single source: span cost matches the RoutingResult that feeds cost_events.
    assert s["cost_usd"] == result.cost_usd
    assert s["cost_known"] == 1  # measured cost (distinguishes a real $0 free call)
    assert s["parent_span_id"] is None  # root (no enclosing operation span)
    attrs = json.loads(s["attributes_json"])
    assert attrs["attempts"] == result.attempts
    assert attrs["fallback_used"] is False


@pytest.mark.asyncio
async def test_fallback_recorded_in_span(wired_router) -> None:
    router, writer, db, delegate = wired_router
    # free-1 fails → falls back to free-2
    delegate.responses = {
        "free-1": CallResult(success=False, error="down", status_code=503),
    }
    result = await router.route_call("test_mixed", _MSG)
    assert result.success is True
    assert result.provider_used == "free-2"
    await _drain(writer)

    s = (await _spans(db))[0]
    assert s["provider"] == "free-2"
    attrs = json.loads(s["attributes_json"])
    assert attrs["fallback_used"] is True


@pytest.mark.asyncio
async def test_all_exhausted_emits_error_span(wired_router) -> None:
    router, writer, db, delegate = wired_router
    delegate.responses = {
        "free-1": CallResult(success=False, error="down", status_code=503),
        "free-2": CallResult(success=False, error="down", status_code=503),
        "paid-1": CallResult(success=False, error="down", status_code=503),
    }
    result = await router.route_call("test_mixed", _MSG)
    assert result.success is False
    await _drain(writer)

    s = (await _spans(db))[0]
    assert s["status"] == "error"
    assert "exhausted" in (s["status_message"] or "")
    attrs = json.loads(s["attributes_json"])
    assert attrs["attempts"] >= 1


@pytest.mark.asyncio
async def test_degradation_skip_is_not_run_span(wired_router, monkeypatch) -> None:
    router, writer, db, _ = wired_router
    # Force a deliberate shed — returns success=False with attempts==0.
    monkeypatch.setattr(router.degradation, "should_skip", lambda _cs: True)
    result = await router.route_call("test_mixed", _MSG)
    assert result.success is False
    assert result.attempts == 0
    await _drain(writer)

    s = (await _spans(db))[0]
    # A shed is NOT an error — status stays ok, tagged not_run with the reason.
    assert s["status"] == "ok"
    attrs = json.loads(s["attributes_json"])
    assert attrs["not_run"] is True
    assert "Degradation" in (attrs["reason"] or "")


class TestCriticalPathSafety:
    """Span faults must never affect routing — the hot-path guarantee."""

    @pytest.mark.asyncio
    async def test_writer_record_raising_does_not_break_routing(
        self, sample_config, sample_providers, db
    ) -> None:
        class _BoomWriter:
            def record(self, span):
                raise RuntimeError("span boom")

        spans_mod.set_writer(_BoomWriter(), enabled=True)
        try:
            router = Router(
                config=sample_config,
                breakers=CircuitBreakerRegistry(sample_providers),
                cost_tracker=CostTracker(db),
                degradation=DegradationTracker(),
                delegate=MockDelegate(),
            )
            result = await router.route_call("test_mixed", _MSG)
            # Routing succeeds despite the span writer raising.
            assert result.success is True
            assert result.provider_used == "free-1"
        finally:
            spans_mod.set_writer(None)

    @pytest.mark.asyncio
    async def test_disabled_capture_emits_no_spans(self, wired_router) -> None:
        router, writer, db, _ = wired_router
        spans_mod.set_writer(writer, enabled=False)
        result = await router.route_call("test_mixed", _MSG)
        assert result.success is True
        await _drain(writer)
        assert await _spans(db) == []


@pytest.mark.asyncio
async def test_operation_span_nests_route_call(wired_router) -> None:
    """A3 composition: an enclosing operation span parents the LLM span — one trace.

    This is the structure a real reflection/ego cycle produces (the cycle opens
    an `operation` span; the LLM calls it makes nest under it).
    """
    from genesis.observability.spans import SpanKind, start_span

    router, writer, db, _ = wired_router
    with start_span("reflection.cycle", SpanKind.OPERATION):
        result = await router.route_call("test_mixed", _MSG)
    assert result.success is True
    await _drain(writer)

    rows = await _spans(db)
    op = next(r for r in rows if r["kind"] == "operation")
    llm = next(r for r in rows if r["kind"] == "llm")
    assert op["parent_span_id"] is None  # root of the trace
    assert llm["parent_span_id"] == op["span_id"]  # LLM nests under the cycle
    assert llm["trace_id"] == op["trace_id"]  # one trace
