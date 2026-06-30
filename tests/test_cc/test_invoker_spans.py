"""A3: CCInvoker injects the active trace context into the child CC env.

This is the cross-process contract: when a span is active, _build_env stamps
GENESIS_TRACE_ID + GENESIS_PARENT_SPAN_ID so the CC PostToolUse hook (A4) can
nest the dispatched session's tool spans under the dispatching operation.
"""

from __future__ import annotations

import contextlib
import json

import aiosqlite
import pytest

from genesis.cc.invoker import CCInvoker
from genesis.cc.types import CCInvocation, CCOutput
from genesis.db.schema import create_all_tables
from genesis.observability import spans as spans_mod
from genesis.observability.span_writer import SpanWriter
from genesis.observability.spans import SpanKind, start_span


@pytest.fixture
async def capture_on(tmp_path):
    db_path = tmp_path / "s.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await create_all_tables(conn)
        writer = SpanWriter()
        writer.set_db(conn, process="test")
        spans_mod.set_writer(writer, enabled=True)
        try:
            yield
        finally:
            spans_mod.set_writer(None)


@pytest.mark.asyncio
async def test_build_env_injects_trace_when_span_active(capture_on) -> None:
    invoker = CCInvoker(claude_path="/usr/bin/claude")
    inv = CCInvocation(prompt="hi")
    with start_span("cc.session", SpanKind.CC_SESSION) as sp:
        env = invoker._build_env(inv)
    assert env["GENESIS_TRACE_ID"] == sp.trace_id
    assert env["GENESIS_PARENT_SPAN_ID"] == sp.span_id
    # The existing session signal is still set (we only added to it).
    assert env["GENESIS_CC_SESSION"] == "1"


@pytest.mark.asyncio
async def test_build_env_omits_trace_vars_outside_span(capture_on) -> None:
    invoker = CCInvoker(claude_path="/usr/bin/claude")
    inv = CCInvocation(prompt="hi")
    env = invoker._build_env(inv)  # no active span
    assert "GENESIS_TRACE_ID" not in env
    assert "GENESIS_PARENT_SPAN_ID" not in env


@pytest.mark.asyncio
async def test_build_env_omits_trace_vars_when_capture_disabled() -> None:
    spans_mod.set_writer(None)  # capture off
    try:
        invoker = CCInvoker(claude_path="/usr/bin/claude")
        inv = CCInvocation(prompt="hi")
        with start_span("cc.session", SpanKind.CC_SESSION):
            env = invoker._build_env(inv)
        assert "GENESIS_TRACE_ID" not in env
        assert "GENESIS_PARENT_SPAN_ID" not in env
    finally:
        spans_mod.set_writer(None)


@pytest.mark.asyncio
async def test_run_records_cc_session_span_with_output_cost(tmp_path, monkeypatch) -> None:
    """run() captures the CCOutput cost/tokens/model on the cc.session span."""
    db_path = tmp_path / "s.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await create_all_tables(conn)
        writer = SpanWriter()
        writer.set_db(conn, process="test")
        spans_mod.set_writer(writer, enabled=True)
        try:
            invoker = CCInvoker(claude_path="/usr/bin/claude")
            fake = CCOutput(
                session_id="s", text="ok", model_used="opus", cost_usd=0.42,
                input_tokens=100, output_tokens=50, duration_ms=1000, exit_code=0,
            )

            async def _fake_inner(_inv):
                return fake

            monkeypatch.setattr(invoker, "_run_inner", _fake_inner)
            out = await invoker.run(CCInvocation(prompt="hi"))
            # run() stamps the selected roster model onto the output (native here).
            assert out.roster_model == "claude"
            assert (out.session_id, out.cost_usd, out.model_used) == (
                fake.session_id, fake.cost_usd, fake.model_used,
            )

            if writer._flush_task is not None:
                with contextlib.suppress(Exception):
                    await writer._flush_task
            await writer._flush_to_db()

            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT * FROM otel_spans WHERE kind='cc_session'"
            )
            rows = [dict(r) for r in await cur.fetchall()]
            assert len(rows) == 1
            attrs = json.loads(rows[0]["attributes_json"])
            assert attrs["cost_usd"] == 0.42
            assert attrs["model_used"] == "opus"
            assert attrs["input_tokens"] == 100
            assert attrs["output_tokens"] == 50
        finally:
            spans_mod.set_writer(None)
