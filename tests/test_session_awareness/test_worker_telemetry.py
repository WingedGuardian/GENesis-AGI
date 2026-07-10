"""Arbiter neural-monitor telemetry — call_site_last_run recording.

The worker records one ``ambient_arbiter`` row per real arbiter run
(judged verdicts only, and only when candidates existed — an empty set
short-circuits judge_candidates without spawning CC). The write goes
through its own short-lived RW connection; the retrieval connection
stays read-only, and telemetry failure must never affect the verdict.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from genesis.session_awareness import worker as worker_mod
from genesis.session_awareness.arbiter import ARBITER_MODEL
from genesis.session_awareness.statefiles import empty_state, save_state
from genesis.session_awareness.worker import run_worker

DIM = 8
SID = "worker-telemetry-1"

_TABLE_DDL = """
    CREATE TABLE call_site_last_run (
        call_site_id TEXT PRIMARY KEY,
        last_run_at TEXT NOT NULL,
        provider_used TEXT,
        model_id TEXT,
        response_text TEXT,
        input_tokens INTEGER,
        output_tokens INTEGER,
        success INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL
    )
"""


def _seed_theme(sessions_root: Path) -> None:
    s = empty_state(SID)
    s["ema"] = [1.0] + [0.0] * (DIM - 1)
    s["ema_turns"] = 4
    s["ring"] = [s["ema"]] * 3
    s["entities"] = {"genesis": 2.0}
    s["updated_at"] = datetime.now(UTC).isoformat()
    save_state(SID, s, base=sessions_root)


def _tmp_db(tmp_path: Path) -> Path:
    db = tmp_path / "g.db"
    con = sqlite3.connect(str(db))
    con.execute(_TABLE_DDL)
    con.commit()
    con.close()
    return db


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
    _seed_theme(sessions)
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
    db = _tmp_db(tmp_path)
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
    db = _tmp_db(tmp_path)
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
    db = _tmp_db(tmp_path)

    # no_theme: no arbiter, no row
    sessions, state = tmp_path / "s0", tmp_path / "sa0"
    result = await run_worker(SID, sessions_root=sessions, state_root=state)
    assert result["status"] == "no_theme"
    assert _last_run_row(db) is None

    # --no-arbiter: candidates ranked but arbiter never invoked
    sessions, state = tmp_path / "s1", tmp_path / "sa1"
    _seed_theme(sessions)
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
    db = _tmp_db(tmp_path)
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
async def test_retrieval_rows_untouched_by_telemetry(tmp_path):
    """The RW telemetry connection writes ONLY call_site_last_run — the
    zero-write invariant on memory rows (retrieved_count) holds."""
    db = _tmp_db(tmp_path)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE memory_metadata (id TEXT PRIMARY KEY, retrieved_count INTEGER)"
    )
    con.execute("INSERT INTO memory_metadata VALUES ('m1', 7)")
    con.commit()
    con.close()

    await _run(
        tmp_path, db=db,
        arbiter_verdict={"arbiter": "ok", "picks": [1], "prompt_version": "v1"},
        candidates=[{"memory_id": "m1", "score": 0.9, "lanes": ["vector"]}],
    )
    con = sqlite3.connect(str(db))
    count = con.execute(
        "SELECT retrieved_count FROM memory_metadata WHERE id='m1'"
    ).fetchone()[0]
    con.close()
    assert count == 7
    assert _last_run_row(db) is not None
