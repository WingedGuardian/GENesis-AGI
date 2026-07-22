"""Worker tests — verdict plumbing in-process, entry script as subprocess."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from genesis.session_awareness import worker as worker_mod
from genesis.session_awareness.slots import try_acquire_slot
from genesis.session_awareness.worker import run_worker

from .conftest import DIM, seed_theme

REPO_DIR = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_DIR / "scripts" / "ambient_awareness_worker.py"

SID = "worker-test-1"


def _seed_theme(sessions_root: Path, *, ema=None) -> None:
    # Shared helper (conftest.seed_theme) owns the theme recipe and the
    # NOW-relative updated_at discipline — see its docstring.
    seed_theme(sessions_root, SID, ema=ema)


def _tmp_db(tmp_path: Path) -> Path:
    db = tmp_path / "g.db"
    sqlite3.connect(str(db)).close()
    return db


def _verdict(sessions_root: Path) -> dict:
    return json.loads((sessions_root / SID / "ambient_verdict.json").read_text())


@pytest.mark.asyncio
async def test_no_theme_verdict(tmp_path):
    sessions, state = tmp_path / "s", tmp_path / "sa"
    result = await run_worker(SID, sessions_root=sessions, state_root=state)
    assert result["status"] == "no_theme"
    assert _verdict(sessions)["status"] == "no_theme"
    log_lines = (state / "shadow_log.jsonl").read_text().splitlines()
    assert json.loads(log_lines[0])["session_id"] == SID


@pytest.mark.asyncio
async def test_ranked_verdict_and_entity_query(tmp_path):
    sessions, state = tmp_path / "s", tmp_path / "sa"
    _seed_theme(sessions)
    fake = [{"memory_id": "m1", "score": 0.9, "lanes": ["vector"]}]
    with patch.object(
        worker_mod, "rank_candidates", new=AsyncMock(return_value=fake),
    ) as rc:
        result = await run_worker(
            SID,
            no_arbiter=True,
            sessions_root=sessions,
            state_root=state,
            db_path=_tmp_db(tmp_path),
            qdrant_url="http://127.0.0.1:1",
        )
    assert result["status"] == "no_arbiter"
    assert result["candidates"] == fake
    assert result["theme"]["ema_turns"] == 4
    assert result["theme"]["stability"] == 1.0
    # Entity query = top-weight ledger entries, best first
    assert result["entity_query"].split()[:2] == ["genesis", "voice"]
    kwargs = rc.call_args.kwargs
    assert kwargs["ema"] == [1.0] + [0.0] * (DIM - 1)
    v = _verdict(sessions)
    assert v["candidates"] == fake


@pytest.mark.asyncio
async def test_slots_busy_fail_closed(tmp_path, monkeypatch):
    sessions, state = tmp_path / "s", tmp_path / "sa"
    _seed_theme(sessions)
    monkeypatch.setattr(
        "genesis.session_awareness.slots.ACQUIRE_TIMEOUT_S", 0.1,
    )
    held = [try_acquire_slot(state / "locks") for _ in range(2)]
    assert all(held)
    result = await run_worker(SID, sessions_root=sessions, state_root=state)
    assert result["status"] == "slots_busy"
    for h in held:
        h.release()


@pytest.mark.asyncio
async def test_error_recorded_and_slot_released(tmp_path):
    sessions, state = tmp_path / "s", tmp_path / "sa"
    _seed_theme(sessions)
    with patch.object(
        worker_mod, "rank_candidates",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        result = await run_worker(
            SID, sessions_root=sessions, state_root=state,
            db_path=_tmp_db(tmp_path), qdrant_url="http://127.0.0.1:1",
        )
    assert result["status"] == "error"
    assert "boom" in result["error"]
    # Slot must be free again after the failure
    a, b = try_acquire_slot(state / "locks"), try_acquire_slot(state / "locks")
    assert a is not None and b is not None
    a.release()
    b.release()


@pytest.mark.asyncio
async def test_shadow_log_cap_is_counted_not_silent(tmp_path, monkeypatch):
    sessions, state = tmp_path / "s", tmp_path / "sa"
    state.mkdir(parents=True)
    (state / "shadow_log.jsonl").write_text('{"old": true}\n')
    monkeypatch.setattr(worker_mod, "SHADOW_LOG_MAX_BYTES", 5)
    result = await run_worker(SID, sessions_root=sessions, state_root=state)
    assert result["status"] == "no_theme"
    assert result["shadow_log_skipped"] is True
    assert _verdict(sessions)["shadow_log_skipped"] is True
    # Log untouched past the cap
    assert (state / "shadow_log.jsonl").read_text() == '{"old": true}\n'


def test_entry_script_subprocess(tmp_path):
    """The spawned form end-to-end: argparse → asyncio → verdict file."""
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--session-id", SID, "--no-arbiter"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(REPO_DIR),
    )
    assert proc.returncode == 0, proc.stderr
    verdict_path = tmp_path / ".genesis" / "sessions" / SID / "ambient_verdict.json"
    assert json.loads(verdict_path.read_text())["status"] == "no_theme"


@pytest.mark.asyncio
async def test_arbiter_path_records_picks(tmp_path):
    """Default (arbiter) path: verdict carries picks + resolved memory ids."""
    sessions, state = tmp_path / "s", tmp_path / "sa"
    _seed_theme(sessions)
    fake = [
        {"memory_id": "m1", "score": 0.9, "lanes": ["vector"]},
        {"memory_id": "m2", "score": 0.7, "lanes": ["decision"]},
    ]
    fake_verdict = {"arbiter": "ok", "picks": [2], "prompt_version": "v1"}
    with (
        patch.object(worker_mod, "rank_candidates", new=AsyncMock(return_value=fake)),
        patch(
            "genesis.session_awareness.arbiter.judge_candidates",
            new=AsyncMock(return_value=fake_verdict),
        ) as judge,
    ):
        result = await run_worker(
            SID, sessions_root=sessions, state_root=state,
            db_path=_tmp_db(tmp_path), qdrant_url="http://127.0.0.1:1",
        )
    assert result["status"] == "judged"
    assert result["arbiter"] == "ok"
    assert result["picked_memory_ids"] == ["m2"]
    judge.assert_awaited_once()
    v = _verdict(sessions)
    assert v["picked_memory_ids"] == ["m2"]


@pytest.mark.asyncio
async def test_entity_candidates_count_replaces_dead_shadow_field(tmp_path):
    """Live-mode entity-lane observability: the verdict reports how many
    surfaced candidates carry the entity lane. 0 is a real signal (a dead
    live entity lane), distinct from a healthy one — the old shadow-only
    ``entity_shadow`` field was always [] post-E4b flip, so a broken and a
    healthy live lane looked identical. That field is gone; ``entity_candidates``
    replaces it."""
    sessions, state = tmp_path / "s", tmp_path / "sa"
    _seed_theme(sessions)
    fake = [
        {"memory_id": "m1", "score": 0.9, "lanes": ["vector"]},
        {"memory_id": "m2", "score": 0.8, "lanes": ["entity"]},
        {"memory_id": "m3", "score": 0.7, "lanes": ["decision", "entity"]},
    ]
    with patch.object(
        worker_mod, "rank_candidates", new=AsyncMock(return_value=fake),
    ):
        result = await run_worker(
            SID,
            no_arbiter=True,
            sessions_root=sessions,
            state_root=state,
            db_path=_tmp_db(tmp_path),
            qdrant_url="http://127.0.0.1:1",
        )
    # Two of the three surfaced candidates carry the entity lane.
    assert result["entity_candidates"] == 2
    assert "entity_shadow" not in result
    v = _verdict(sessions)
    assert v["entity_candidates"] == 2
    assert "entity_shadow" not in v


@pytest.mark.asyncio
async def test_entity_candidates_zero_when_no_entity_lane(tmp_path):
    """A candidate set with no entity-tagged rows reports 0 — the dead-lane
    signal the old always-[] field could never surface."""
    sessions, state = tmp_path / "s", tmp_path / "sa"
    _seed_theme(sessions)
    fake = [{"memory_id": "m1", "score": 0.9, "lanes": ["vector"]}]
    with patch.object(
        worker_mod, "rank_candidates", new=AsyncMock(return_value=fake),
    ):
        result = await run_worker(
            SID,
            no_arbiter=True,
            sessions_root=sessions,
            state_root=state,
            db_path=_tmp_db(tmp_path),
            qdrant_url="http://127.0.0.1:1",
        )
    assert result["entity_candidates"] == 0
