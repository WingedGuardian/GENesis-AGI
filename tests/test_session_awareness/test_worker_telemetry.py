"""Arbiter neural-monitor telemetry — call_site_last_run recording.

The worker records one ``ambient_arbiter`` row per arbiter ATTEMPT
(judged verdicts with a non-empty candidate set — an empty set
short-circuits judge_candidates without spawning CC). The write goes
through ``record_last_run_detached`` (own short-lived RW connection);
the retrieval connection stays read-only, telemetry failure must never
affect the verdict, and ``telemetry_recorded`` must be True only when
the row demonstrably landed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables
from genesis.session_awareness import worker as worker_mod
from genesis.session_awareness.arbiter import ARBITER_MODEL
from genesis.session_awareness.worker import run_worker

from .conftest import seed_theme

SID = "worker-telemetry-1"


async def _real_schema_db(tmp_path: Path) -> Path:
    """Temp file DB with the REAL schema — no hand-copied DDL to drift."""
    db_path = tmp_path / "g.db"
    conn = await aiosqlite.connect(str(db_path))
    await create_all_tables(conn)
    await conn.commit()
    await conn.close()
    return db_path


def _last_run_row(db: Path) -> sqlite3.Row | None:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT * FROM call_site_last_run WHERE call_site_id='ambient_arbiter'"
    ).fetchone()
    con.close()
    return row


async def _run(tmp_path, *, db: Path, arbiter_verdict: dict, candidates: list):
    sessions, state = tmp_path / "s", tmp_path / "sa"
    seed_theme(sessions, SID)
    with (
        patch.object(
            worker_mod, "rank_candidates", new=AsyncMock(return_value=candidates),
        ),
        patch(
            "genesis.session_awareness.arbiter.judge_candidates",
            new=AsyncMock(return_value=arbiter_verdict),
        ),
    ):
        return await run_worker(
            SID, sessions_root=sessions, state_root=state,
            db_path=db, qdrant_url="http://127.0.0.1:1",
        )


@pytest.mark.asyncio
async def test_judged_run_records_last_run_row(tmp_path):
    db = await _real_schema_db(tmp_path)
    result = await _run(
        tmp_path, db=db,
        arbiter_verdict={"arbiter": "ok", "picks": [1], "prompt_version": "v1"},
        candidates=[{"memory_id": "m1", "score": 0.9, "lanes": ["vector"]}],
    )
    assert result["status"] == "judged"
    assert result["telemetry_recorded"] is True
    assert isinstance(result["arbiter_latency_ms"], int)
    row = _last_run_row(db)
    assert row is not None
    assert row["provider_used"] == "cc"
    assert row["model_id"] == ARBITER_MODEL
    assert row["success"] == 1
    assert "arbiter=ok" in row["response_text"]
    assert "picks=1" in row["response_text"]
    assert "candidates=1" in row["response_text"]
    assert "lat_ms=" in row["response_text"]


@pytest.mark.asyncio
async def test_arbiter_failure_records_red_row(tmp_path):
    db = await _real_schema_db(tmp_path)
    result = await _run(
        tmp_path, db=db,
        arbiter_verdict={
            "arbiter": "timeout", "picks": [], "prompt_version": "v1",
        },
        candidates=[{"memory_id": "m1", "score": 0.9, "lanes": ["vector"]}],
    )
    assert result["telemetry_recorded"] is True
    row = _last_run_row(db)
    assert row["success"] == 0
    assert "arbiter=timeout" in row["response_text"]


@pytest.mark.asyncio
async def test_no_run_paths_do_not_record(tmp_path):
    db = await _real_schema_db(tmp_path)

    # no_theme: no arbiter, no row
    sessions, state = tmp_path / "s0", tmp_path / "sa0"
    result = await run_worker(SID, sessions_root=sessions, state_root=state)
    assert result["status"] == "no_theme"
    assert _last_run_row(db) is None

    # --no-arbiter: candidates ranked but arbiter never invoked
    sessions, state = tmp_path / "s1", tmp_path / "sa1"
    seed_theme(sessions, SID)
    with patch.object(
        worker_mod, "rank_candidates",
        new=AsyncMock(return_value=[{"memory_id": "m1", "score": 0.9}]),
    ):
        result = await run_worker(
            SID, no_arbiter=True, sessions_root=sessions, state_root=state,
            db_path=db, qdrant_url="http://127.0.0.1:1",
        )
    assert result["status"] == "no_arbiter"
    assert "telemetry_recorded" not in result
    assert _last_run_row(db) is None

    # Empty candidate set: judge_candidates short-circuits, no CC spawned
    result = await _run(
        tmp_path, db=db,
        arbiter_verdict={"arbiter": "ok", "picks": [], "prompt_version": "v1"},
        candidates=[],
    )
    assert result["status"] == "judged"
    assert "telemetry_recorded" not in result
    assert _last_run_row(db) is None


@pytest.mark.asyncio
async def test_telemetry_failure_never_breaks_verdict(tmp_path):
    """A dead telemetry path degrades to telemetry_recorded=False —
    the verdict still writes and the shadow log still appends."""
    db = await _real_schema_db(tmp_path)
    with patch(
        "genesis.db.connection.get_raw_db",
        side_effect=RuntimeError("db exploded"),
    ):
        result = await _run(
            tmp_path, db=db,
            arbiter_verdict={"arbiter": "ok", "picks": [], "prompt_version": "v1"},
            candidates=[{"memory_id": "m1", "score": 0.9, "lanes": ["vector"]}],
        )
    assert result["status"] == "judged"
    assert result["telemetry_recorded"] is False
    assert _last_run_row(db) is None
    # Verdict file + shadow log both written despite the telemetry failure
    verdict_file = tmp_path / "s" / SID / "ambient_verdict.json"
    assert verdict_file.exists()
    log = (tmp_path / "sa" / "shadow_log.jsonl").read_text()
    assert '"telemetry_recorded": false' in log


@pytest.mark.asyncio
async def test_swallowed_insert_failure_reports_false(tmp_path):
    """record_last_run swallows INSERT errors internally — the flag must
    still come back False, not a false-positive True. A schemaless DB
    (missing call_site_last_run, e.g. migration-lagged install) is the
    concrete reachable case."""
    db_path = tmp_path / "empty.db"
    sqlite3.connect(str(db_path)).close()  # no tables at all
    result = await _run(
        tmp_path, db=db_path,
        arbiter_verdict={"arbiter": "ok", "picks": [], "prompt_version": "v1"},
        candidates=[{"memory_id": "m1", "score": 0.9, "lanes": ["vector"]}],
    )
    assert result["status"] == "judged"
    assert result["telemetry_recorded"] is False


@pytest.mark.asyncio
async def test_retrieval_rows_untouched_by_telemetry(tmp_path):
    """The RW telemetry connection writes ONLY call_site_last_run — rows
    carrying retrieved_count (real schema: observations) stay untouched."""
    db = await _real_schema_db(tmp_path)
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO observations (id, source, type, content, priority,"
        " retrieved_count, created_at)"
        " VALUES ('o1', 'test', 'note', 'x', 'low', 7, '2026-07-10T00:00:00Z')"
    )
    con.commit()
    con.close()

    await _run(
        tmp_path, db=db,
        arbiter_verdict={"arbiter": "ok", "picks": [1], "prompt_version": "v1"},
        candidates=[{"memory_id": "m1", "score": 0.9, "lanes": ["vector"]}],
    )
    con = sqlite3.connect(str(db))
    count = con.execute(
        "SELECT retrieved_count FROM observations WHERE id='o1'"
    ).fetchone()[0]
    con.close()
    assert count == 7
    assert _last_run_row(db) is not None
