"""A5: read APIs (span_reader), config loader, and the settings domain."""

from __future__ import annotations

import json

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables
from genesis.observability.span_reader import (
    flatten_tree,
    get_session_spans,
    get_trace,
    list_recent_traces,
)


@pytest.fixture()
async def db(tmp_path):
    db_path = tmp_path / "spans.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await create_all_tables(conn)
        yield conn


async def _ins(db, span_id, trace_id, *, parent=None, name="n", kind="internal",
               start=0, session_id=None, attrs=None, status="ok") -> None:
    await db.execute(
        "INSERT INTO otel_spans (span_id, trace_id, parent_span_id, name, kind, "
        "status, start_unix_us, session_id, attributes_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (span_id, trace_id, parent, name, kind, status, start, session_id,
         json.dumps(attrs) if attrs else None),
    )
    await db.commit()


class TestGetTrace:

    @pytest.mark.asyncio
    async def test_builds_parent_child_tree(self, db) -> None:
        await _ins(db, "r", "T", parent=None, name="reflection.cycle",
                   kind="operation", start=1)
        await _ins(db, "a", "T", parent="r", name="llm.call", kind="llm", start=2)
        await _ins(db, "b", "T", parent="r", name="cc.session",
                   kind="cc_session", start=3)
        await _ins(db, "g", "T", parent="a", name="cc.tool.Bash",
                   kind="tool", start=4)

        trace = await get_trace(db, "T")
        assert trace is not None
        assert trace["span_count"] == 4
        assert len(trace["roots"]) == 1
        root = trace["roots"][0]
        assert root["span_id"] == "r"
        # children ordered by start_unix_us
        assert [c["span_id"] for c in root["children"]] == ["a", "b"]
        assert [c["span_id"] for c in root["children"][0]["children"]] == ["g"]

    @pytest.mark.asyncio
    async def test_orphan_surfaces_as_root(self, db) -> None:
        # parent not present in this trace → the span is treated as a root.
        await _ins(db, "x", "T2", parent="missing-parent", start=1)
        trace = await get_trace(db, "T2")
        assert [r["span_id"] for r in trace["roots"]] == ["x"]

    @pytest.mark.asyncio
    async def test_unknown_trace_is_none(self, db) -> None:
        assert await get_trace(db, "nope") is None

    @pytest.mark.asyncio
    async def test_attributes_hydrated(self, db) -> None:
        await _ins(db, "r", "T", attrs={"depth": "deep", "n": 3})
        trace = await get_trace(db, "T")
        assert trace["roots"][0]["attributes"] == {"depth": "deep", "n": 3}


class TestListRecentTraces:

    @pytest.mark.asyncio
    async def test_newest_first_with_counts(self, db) -> None:
        await _ins(db, "r1", "T1", parent=None, start=10)
        await _ins(db, "r1c", "T1", parent="r1", start=11)  # child → T1 count 2
        await _ins(db, "r2", "T2", parent=None, start=20)

        recent = await list_recent_traces(db, limit=10)
        assert [r["trace_id"] for r in recent] == ["T2", "T1"]  # newest first
        counts = {r["trace_id"]: r["span_count"] for r in recent}
        assert counts == {"T2": 1, "T1": 2}

    @pytest.mark.asyncio
    async def test_empty(self, db) -> None:
        assert await list_recent_traces(db) == []


class TestSessionSpans:

    @pytest.mark.asyncio
    async def test_returns_session_spans_flat(self, db) -> None:
        await _ins(db, "a", "T1", start=1, session_id="S")
        await _ins(db, "b", "T2", start=2, session_id="S")
        await _ins(db, "c", "T3", start=3, session_id="other")
        spans = await get_session_spans(db, "S")
        assert [s["span_id"] for s in spans] == ["a", "b"]


class TestFlatten:

    @pytest.mark.asyncio
    async def test_depth_first_with_depth(self, db) -> None:
        await _ins(db, "r", "T", parent=None, start=1)
        await _ins(db, "a", "T", parent="r", start=2)
        await _ins(db, "g", "T", parent="a", start=3)
        await _ins(db, "b", "T", parent="r", start=4)
        flat = flatten_tree(await get_trace(db, "T"))
        assert [(s["span_id"], s["depth"]) for s in flat] == [
            ("r", 0), ("a", 1), ("g", 2), ("b", 1),
        ]


class TestSpanConfig:

    def test_reads_repo_config(self) -> None:
        from genesis.observability.span_config import load_spans_config

        enabled, retention = load_spans_config()
        assert enabled is True
        assert retention == 14

    def test_defaults_on_bad_file(self, tmp_path, monkeypatch) -> None:
        from genesis.observability import span_config

        bad = tmp_path / "observability.yaml"
        bad.write_text("spans: {retention_days: not-an-int")  # malformed
        monkeypatch.setattr(span_config, "_config_path", lambda: bad)
        assert span_config.load_spans_config() == (True, 14)


class TestSettingsDomain:

    def test_domain_registered(self) -> None:
        from genesis.mcp.health.settings import _DOMAIN_REGISTRY

        assert "observability" in _DOMAIN_REGISTRY
        assert _DOMAIN_REGISTRY["observability"].needs_restart is True

    def test_validator_accepts_good(self) -> None:
        from genesis.mcp.health.settings import _validate_observability

        assert _validate_observability(
            {"spans": {"enabled": False, "retention_days": 7}}
        ) == []

    def test_validator_rejects_bad(self) -> None:
        from genesis.mcp.health.settings import _validate_observability

        assert _validate_observability({"spans": {"retention_days": 0}})
        assert _validate_observability({"spans": {"enabled": "yes"}})
        assert _validate_observability({"spans": {"bogus": 1}})
        assert _validate_observability({"other": 1})
