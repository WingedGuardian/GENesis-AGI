"""Tests for the span primitives (spans.py + span_writer.py) — A1, dark.

Covers ContextVar nesting/rooting, exception capture, cross-process parent
injection, ContextVar propagation across asyncio.gather, the kill switch, and
the batched writer (persist, LLM denorm fields, INSERT OR IGNORE, prune).
"""

from __future__ import annotations

import asyncio
import contextlib

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables
from genesis.observability.span_writer import SpanWriter
from genesis.observability.spans import (
    SpanKind,
    current_trace_context,
    is_enabled,
    set_writer,
    start_span,
)


@pytest.fixture()
async def wired(tmp_path):
    """Temp DB with otel_spans + a wired SpanWriter; capture enabled."""
    db_path = tmp_path / "spans.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await create_all_tables(conn)
        writer = SpanWriter()
        writer.set_db(conn, process="test")
        set_writer(writer, enabled=True)
        try:
            yield writer, conn
        finally:
            set_writer(None)


async def _drain(writer: SpanWriter) -> None:
    """Deterministically flush the writer (await the scheduled task + direct)."""
    if writer._flush_task is not None:
        with contextlib.suppress(Exception):
            await writer._flush_task
    await writer._flush_to_db()


async def _rows(conn) -> list[dict]:
    conn.row_factory = aiosqlite.Row
    cur = await conn.execute("SELECT * FROM otel_spans ORDER BY start_unix_us")
    return [dict(r) for r in await cur.fetchall()]


class TestContextVarSemantics:

    @pytest.mark.asyncio
    async def test_root_mints_trace_id(self, wired) -> None:
        with start_span("op", SpanKind.OPERATION) as s:
            assert s.parent_span_id is None
            assert s.trace_id and s.span_id
            assert s.trace_id != s.span_id

    @pytest.mark.asyncio
    async def test_child_nests_under_parent(self, wired) -> None:
        with (
            start_span("parent", SpanKind.OPERATION) as p,
            start_span("child", SpanKind.LLM) as c,
        ):
            assert c.trace_id == p.trace_id
            assert c.parent_span_id == p.span_id
            assert c.span_id != p.span_id

    @pytest.mark.asyncio
    async def test_sibling_roots_have_distinct_traces(self, wired) -> None:
        with start_span("a") as a:
            tid_a = a.trace_id
        with start_span("b") as b:
            tid_b = b.trace_id
        assert tid_a != tid_b

    @pytest.mark.asyncio
    async def test_contextvar_restored_after_block(self, wired) -> None:
        assert current_trace_context() is None
        with start_span("op") as s:
            assert current_trace_context() == (s.trace_id, s.span_id)
        assert current_trace_context() is None

    @pytest.mark.asyncio
    async def test_explicit_parent_trace_injection(self, wired) -> None:
        # Cross-process handoff: explicit args win over the (absent) ContextVar.
        with start_span("child", SpanKind.TOOL, trace_id="T1", parent_span_id="P1") as c:
            assert c.trace_id == "T1"
            assert c.parent_span_id == "P1"

    @pytest.mark.asyncio
    async def test_gather_propagates_context(self, wired) -> None:
        async def child(label: str) -> tuple[str, str | None]:
            with start_span(label, SpanKind.LLM) as c:
                await asyncio.sleep(0)
                return c.trace_id, c.parent_span_id

        with start_span("parent", SpanKind.OPERATION) as p:
            results = await asyncio.gather(child("c1"), child("c2"))

        for tid, pid in results:
            assert tid == p.trace_id
            assert pid == p.span_id


class TestExceptionCapture:

    @pytest.mark.asyncio
    async def test_exception_sets_error_status_and_reraises(self, wired) -> None:
        captured = {}
        with pytest.raises(ValueError), start_span("op", SpanKind.OPERATION) as s:
            captured["s"] = s
            raise ValueError("boom")
        assert captured["s"].status == "error"
        assert "boom" in captured["s"].status_message


class TestWriterPersistence:

    @pytest.mark.asyncio
    async def test_span_persisted(self, wired) -> None:
        writer, conn = wired
        with start_span("parent", SpanKind.OPERATION), start_span("child", SpanKind.LLM):
            pass
        await _drain(writer)
        rows = await _rows(conn)
        assert len(rows) == 2
        parent = next(r for r in rows if r["name"] == "parent")
        child = next(r for r in rows if r["name"] == "child")
        assert parent["parent_span_id"] is None
        assert child["parent_span_id"] == parent["span_id"]
        assert child["trace_id"] == parent["trace_id"]
        assert parent["process"] == "test"
        assert parent["start_unix_us"] is not None
        assert parent["duration_us"] is not None  # has end → duration computed

    @pytest.mark.asyncio
    async def test_llm_fields_persisted(self, wired) -> None:
        writer, conn = wired
        with start_span("llm.call", SpanKind.LLM) as s:
            s.set_llm_fields(
                call_site="3_micro_reflection", provider="openrouter-haiku",
                model_id="anthropic/claude-haiku", input_tokens=10,
                output_tokens=20, cost_usd=0.0, cost_known=True,
            )
            s.set_attr("attempts", 1)
        await _drain(writer)
        rows = await _rows(conn)
        assert len(rows) == 1
        r = rows[0]
        assert r["call_site"] == "3_micro_reflection"
        assert r["provider"] == "openrouter-haiku"
        assert r["input_tokens"] == 10 and r["output_tokens"] == 20
        assert r["cost_usd"] == 0.0
        assert r["cost_known"] == 1
        assert '"attempts": 1' in r["attributes_json"]

    @pytest.mark.asyncio
    async def test_error_span_persisted(self, wired) -> None:
        writer, conn = wired
        with contextlib.suppress(ValueError), start_span("op", SpanKind.OPERATION):
            raise ValueError("kaboom")
        await _drain(writer)
        rows = await _rows(conn)
        assert rows[0]["status"] == "error"
        assert "kaboom" in rows[0]["status_message"]

    @pytest.mark.asyncio
    async def test_insert_or_ignore_dupe(self, wired) -> None:
        writer, conn = wired
        # Two rows with the same span_id — the second must be ignored, not raise.
        from genesis.observability.spans import Span

        sp = Span(span_id="dup", trace_id="t", parent_span_id=None, name="n",
                  kind="llm", start_unix_us=1, _t0_mono=0.0)
        writer.record(sp)
        writer.record(sp)
        await _drain(writer)
        rows = await _rows(conn)
        assert len([r for r in rows if r["span_id"] == "dup"]) == 1

    @pytest.mark.asyncio
    async def test_prune_removes_old(self, wired) -> None:
        writer, conn = wired
        from genesis.observability.spans import Span

        old = Span(span_id="old", trace_id="t", parent_span_id=None, name="n",
                   kind="llm", start_unix_us=1, _t0_mono=0.0)  # epoch ~1970
        import time
        new = Span(span_id="new", trace_id="t", parent_span_id=None, name="n",
                   kind="llm", start_unix_us=int(time.time() * 1_000_000),
                   _t0_mono=0.0)
        writer.record(old)
        writer.record(new)
        await _drain(writer)
        removed = await writer.prune(older_than_days=1)
        assert removed == 1
        rows = await _rows(conn)
        assert {r["span_id"] for r in rows} == {"new"}


class TestKillSwitch:

    @pytest.mark.asyncio
    async def test_disabled_yields_null_span(self, wired) -> None:
        writer, conn = wired
        set_writer(writer, enabled=False)
        assert is_enabled() is False
        with start_span("op", SpanKind.OPERATION) as s:
            # Null span — mutators are safe no-ops, ids are None.
            assert s.span_id is None
            s.set_llm_fields(provider="x")
            s.set_attr("k", "v")
            s.set_status_error("nope")
        await _drain(writer)
        assert await _rows(conn) == []

    @pytest.mark.asyncio
    async def test_env_disable(self, wired, monkeypatch) -> None:
        writer, conn = wired
        monkeypatch.setenv("GENESIS_SPANS_DISABLED", "1")
        set_writer(writer, enabled=True)  # env overrides the enabled=True arg
        assert is_enabled() is False
        with start_span("op"):
            pass
        await _drain(writer)
        assert await _rows(conn) == []

    @pytest.mark.asyncio
    async def test_no_writer_is_noop(self, tmp_path) -> None:
        set_writer(None)
        try:
            with start_span("op") as s:
                assert s.span_id is None  # null span, no crash
        finally:
            set_writer(None)

    @pytest.mark.asyncio
    async def test_record_without_db_is_noop(self) -> None:
        from genesis.observability.spans import Span

        writer = SpanWriter()  # no set_db
        sp = Span(span_id="x", trace_id="t", parent_span_id=None, name="n",
                  kind="llm", start_unix_us=1, _t0_mono=0.0)
        writer.record(sp)  # must not raise
        assert writer._write_batch == []
