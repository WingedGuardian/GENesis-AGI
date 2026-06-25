"""Create the ``otel_spans`` table — homegrown OpenTelemetry-convention tracing.

A trace is one logical Genesis operation (a reflection cycle, an ego cycle, a
dispatched CC session, an LLM call) decomposed into parent/child spans with
timing, status, and attributes. This is the data layer the trace-waterfall
dashboard renders — answering "what did the agents actually do?". There is NO
OpenTelemetry SDK; the field semantics follow OTel conventions (real trace/span
ids, ``kind``/``status`` vocab) so a ``to_otel()`` export adapter is trivial
later, but nothing here depends on the SDK.

DARK on creation: this migration only lays the table. No code writes or reads it
until later phases (the span writer + ``route_call`` instrumentation + the CC
PostToolUse hook land separately).

**Dual clocks (deliberate).** ``start_unix_us``/``end_unix_us`` are wall-clock
epoch microseconds — the cross-process ORDERING axis (comparable across the
genesis-server runtime, the MCP-server children, and the CC subprocess + its
hooks, which is where spans originate). ``duration_us`` is derived from a
*monotonic* clock within the span's owning process — immune to NTP steps, so a
bar's LENGTH is correct even if wall-clock jumps. Point-in-time spans (a CC tool
invocation, captured only on PostToolUse) write ``end_unix_us = start_unix_us``
and ``duration_us = NULL`` honestly.

**Denormalized LLM block** (``call_site``/``provider``/``model_id``/tokens/cost,
populated only for ``kind='llm'``): written from the SAME ``RoutingResult`` object
that feeds ``cost_events`` in the same call, so no value-drift is possible at
write time. ``cost_events`` remains AUTHORITATIVE for billing/budget — these span
columns are an at-a-glance observability snapshot, not a financial ledger (and
free-tier calls write no ``cost_events`` row at all, so the span is the only place
their model/provider is visible).

``kind`` is intentionally NOT a CHECK constraint — new span kinds in later phases
must not require a migration to relax it; the Python ``SpanKind`` enum validates.

Idempotent (``IF NOT EXISTS``). Fresh installs get the same DDL via
``db/schema/_tables.py``; this migration covers existing installs.
"""

from __future__ import annotations

import aiosqlite

_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS otel_spans (
        span_id         TEXT PRIMARY KEY,
        trace_id        TEXT NOT NULL,
        parent_span_id  TEXT,
        name            TEXT NOT NULL,
        kind            TEXT NOT NULL,
        status          TEXT NOT NULL DEFAULT 'ok'
                            CHECK (status IN ('ok', 'error')),
        status_message  TEXT,
        start_unix_us   INTEGER NOT NULL,
        end_unix_us     INTEGER,
        duration_us     INTEGER,
        session_id      TEXT,
        process         TEXT,
        call_site       TEXT,
        provider        TEXT,
        model_id        TEXT,
        input_tokens    INTEGER,
        output_tokens   INTEGER,
        cost_usd        REAL,
        cost_known      INTEGER,
        attributes_json TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )
"""

_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_otel_spans_trace ON otel_spans(trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_otel_spans_parent ON otel_spans(parent_span_id)",
    "CREATE INDEX IF NOT EXISTS idx_otel_spans_start ON otel_spans(start_unix_us)",
    "CREATE INDEX IF NOT EXISTS idx_otel_spans_session ON otel_spans(session_id)",
    # list_recent_traces() only scans roots ordered by start time; a partial
    # index keeps that cheap as the table grows to 6-figure rows.
    "CREATE INDEX IF NOT EXISTS idx_otel_spans_roots "
    "ON otel_spans(start_unix_us) WHERE parent_span_id IS NULL",
)


async def up(db: aiosqlite.Connection) -> None:
    # NOTE: must NOT call db.commit()/BEGIN — the runner owns the transaction.
    await db.execute(_TABLE_DDL)
    for stmt in _INDEX_DDL:
        await db.execute(stmt)


async def down(db: aiosqlite.Connection) -> None:
    """Drop the table (and its indexes) — development/testing only."""
    await db.execute("DROP TABLE IF EXISTS otel_spans")
