"""A4: CC tool spans — the PostToolUse hook + the server-side ingest.

The hook (cc_span_hook.py) runs in the CC subprocess and drops JSONL records;
the ingest (span_ingest) drains them into otel_spans. Tests the hook via
subprocess (faithful: real stdin/env/file path), the ingest in-process, and the
full hook -> ingest -> otel_spans path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables
from genesis.observability.span_ingest import ingest_pending_spans

_HOOK = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "cc_span_hook.py"


def _run_hook(payload: dict, *, incoming: Path, trace_id: str | None = "T1",
              parent: str | None = "P1", session: str | None = "S1") -> None:
    env = {**os.environ, "GENESIS_SPANS_INCOMING_DIR": str(incoming)}
    for k, v in (("GENESIS_TRACE_ID", trace_id), ("GENESIS_PARENT_SPAN_ID", parent),
                 ("GENESIS_SESSION_ID", session), ("GENESIS_CC_SESSION", "1")):
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(payload), text=True, env=env, timeout=10, check=True,
    )


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


class TestHook:

    def test_writes_span_record_for_bash(self, tmp_path) -> None:
        _run_hook(
            {"tool_name": "Bash", "session_id": "S1",
             "tool_input": {"command": "ls -la /etc"}},
            incoming=tmp_path,
        )
        recs = _read_jsonl(tmp_path / "S1.jsonl")
        assert len(recs) == 1
        r = recs[0]
        assert r["trace_id"] == "T1"
        assert r["parent_span_id"] == "P1"
        assert r["name"] == "cc.tool.Bash"
        assert r["kind"] == "tool"
        assert r["status"] == "ok"
        assert r["duration_us"] is None
        assert r["start_unix_us"] == r["end_unix_us"]
        assert r["attributes"]["command"] == "ls -la /etc"
        assert r["span_id"]  # minted

    def test_no_trace_id_is_noop(self, tmp_path) -> None:
        _run_hook(
            {"tool_name": "Bash", "session_id": "S1", "tool_input": {"command": "ls"}},
            incoming=tmp_path, trace_id=None,
        )
        # Foreground / untraced session → nothing written.
        assert list(tmp_path.glob("*.jsonl")) == []

    def test_skip_tool_writes_nothing(self, tmp_path) -> None:
        _run_hook(
            {"tool_name": "TodoWrite", "session_id": "S1", "tool_input": {}},
            incoming=tmp_path,
        )
        assert list(tmp_path.glob("*.jsonl")) == []

    def test_multiple_tools_append(self, tmp_path) -> None:
        _run_hook({"tool_name": "Bash", "session_id": "S1",
                   "tool_input": {"command": "echo hi"}}, incoming=tmp_path)
        _run_hook({"tool_name": "Read", "session_id": "S1",
                   "tool_input": {"file_path": "/a/b.py"}}, incoming=tmp_path)
        recs = _read_jsonl(tmp_path / "S1.jsonl")
        assert [r["name"] for r in recs] == ["cc.tool.Bash", "cc.tool.Read"]
        assert recs[1]["attributes"]["file_path"] == "/a/b.py"


@pytest.fixture()
async def db(tmp_path):
    db_path = tmp_path / "spans.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await create_all_tables(conn)
        yield conn


async def _spans(conn) -> list[dict]:
    conn.row_factory = aiosqlite.Row
    cur = await conn.execute("SELECT * FROM otel_spans ORDER BY start_unix_us")
    return [dict(r) for r in await cur.fetchall()]


class TestIngest:

    @pytest.mark.asyncio
    async def test_empty_dir_returns_zero(self, db, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("GENESIS_SPANS_INCOMING_DIR", str(tmp_path / "incoming"))
        assert await ingest_pending_spans(db) == 0

    @pytest.mark.asyncio
    async def test_ingests_valid_skips_corrupt(self, db, tmp_path, monkeypatch) -> None:
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        monkeypatch.setenv("GENESIS_SPANS_INCOMING_DIR", str(incoming))
        good = {"span_id": "a", "trace_id": "T", "parent_span_id": "P",
                "name": "cc.tool.Bash", "kind": "tool", "status": "ok",
                "start_unix_us": 100, "end_unix_us": 100, "duration_us": None,
                "session_id": "S", "attributes": {"command": "ls"}}
        good2 = {**good, "span_id": "b", "name": "cc.tool.Read",
                 "attributes": {"file_path": "/x"}}
        bad_missing = {"trace_id": "T"}  # no span_id/name/start → dropped
        lines = [
            json.dumps(good),
            "{ this is not json",          # corrupt → skipped
            "",                            # blank → skipped
            json.dumps(bad_missing),       # malformed record → dropped
            json.dumps(good2),
        ]
        (incoming / "S.jsonl").write_text("\n".join(lines) + "\n")

        n = await ingest_pending_spans(db)
        assert n == 2
        rows = await _spans(db)
        assert {r["span_id"] for r in rows} == {"a", "b"}
        bash = next(r for r in rows if r["span_id"] == "a")
        assert bash["process"] == "cc-hook"
        assert bash["parent_span_id"] == "P"
        assert bash["call_site"] is None  # LLM block NULL for tool spans
        assert json.loads(bash["attributes_json"])["command"] == "ls"
        # File consumed.
        assert list(incoming.glob("*.jsonl")) == []
        assert list(incoming.glob("*.processing")) == []

    @pytest.mark.asyncio
    async def test_idempotent_dupe_span_id(self, db, tmp_path, monkeypatch) -> None:
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        monkeypatch.setenv("GENESIS_SPANS_INCOMING_DIR", str(incoming))
        rec = {"span_id": "dup", "trace_id": "T", "name": "cc.tool.Bash",
               "kind": "tool", "status": "ok", "start_unix_us": 1,
               "attributes": {}}
        (incoming / "f1.jsonl").write_text(json.dumps(rec) + "\n")
        await ingest_pending_spans(db)
        # Same span_id arrives again → INSERT OR IGNORE, no dupe row.
        (incoming / "f2.jsonl").write_text(json.dumps(rec) + "\n")
        await ingest_pending_spans(db)
        rows = await _spans(db)
        assert len([r for r in rows if r["span_id"] == "dup"]) == 1


class TestHookToIngestE2E:

    @pytest.mark.asyncio
    async def test_hook_then_ingest_lands_in_otel_spans(
        self, db, tmp_path, monkeypatch
    ) -> None:
        incoming = tmp_path / "incoming"
        # Hook drops two tool records (subprocess), ingest drains them.
        _run_hook({"tool_name": "Bash", "session_id": "S1",
                   "tool_input": {"command": "pytest"}}, incoming=incoming)
        _run_hook({"tool_name": "Grep", "session_id": "S1",
                   "tool_input": {"pattern": "TODO"}}, incoming=incoming)

        monkeypatch.setenv("GENESIS_SPANS_INCOMING_DIR", str(incoming))
        n = await ingest_pending_spans(db)
        assert n == 2

        rows = await _spans(db)
        # Both tool spans share the dispatch trace + parent (cross-process stitch).
        assert {r["trace_id"] for r in rows} == {"T1"}
        assert {r["parent_span_id"] for r in rows} == {"P1"}
        assert {r["name"] for r in rows} == {"cc.tool.Bash", "cc.tool.Grep"}
        assert all(r["kind"] == "tool" and r["process"] == "cc-hook" for r in rows)
