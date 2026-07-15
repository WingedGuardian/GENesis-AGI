"""Detached ledger shadow worker: end-to-end runs against a tmp DB with
migrations applied and a fake claude binary (arbiter test lineage).

The invariants under test: shadow rows land atomically with the run row;
the cursor advances ONLY on recorded ok/empty_delta outcomes (failures
re-cover their window); off/disabled modes leave zero trace; the live
session_ledger is NEVER written.
"""

from __future__ import annotations

import fcntl
import importlib
import json
import textwrap
from pathlib import Path

import aiosqlite
import pytest

from genesis.db.crud import session_ledger_shadow as shadow_crud
from genesis.session_awareness import ledger_worker as lw

M58 = importlib.import_module("genesis.db.migrations.0058_session_charters")
M59 = importlib.import_module("genesis.db.migrations.0059_session_ledger_shadow")

SID = "aaaabbbb-cccc-dddd-eeee-ffff00001111"


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def sessions_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "sessions"
    monkeypatch.setattr(lw, "_sessions_root", lambda: root)
    return root


@pytest.fixture
def shadow_mode(monkeypatch):
    monkeypatch.setattr(lw, "effective_mode", lambda: "shadow")


@pytest.fixture
async def db_path(tmp_path) -> Path:
    path = tmp_path / "genesis.db"
    shadow_crud._tables_verified = False
    async with aiosqlite.connect(str(path)) as db:
        await M58.up(db)
        await M59.up(db)
        await db.commit()
    yield path
    shadow_crud._tables_verified = False


def _typed(text: str, ref: str) -> dict:
    return {
        "type": "user",
        "isSidechain": False,
        "uuid": ref,
        "message": {"role": "user", "content": text},
        "timestamp": "2026-07-14T12:00:00.000Z",
    }


def _assistant(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        "timestamp": "2026-07-14T11:59:00.000Z",
    }


@pytest.fixture
def transcript(tmp_path) -> Path:
    t = tmp_path / f"{SID}.jsonl"
    entries = [
        _assistant("I propose we ship the widget refactor with a rollback lever."),
        _typed("yes, do that — and wire the rollback lever first", "u-agree"),
        _typed("also what's the weather like?", "u-noise"),
    ]
    t.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return t


def _fake_claude(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake_claude.py"
    script.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body))
    script.chmod(0o755)
    return str(script)


def _verdict_claude(tmp_path: Path, agreements, pivots=()) -> str:
    inner = json.dumps({"agreements": list(agreements), "pivots": list(pivots)})
    return _fake_claude(
        tmp_path,
        f"""
        import json, sys
        sys.stdin.read()
        print(json.dumps({{"result": {json.dumps(inner)}}}))
        """,
    )


AGREEMENT = {
    "turn": 1,
    "text": "wire the rollback lever before the widget refactor ships",
    "quote": "yes, do that",
}


async def _runs(db_path: Path) -> list[dict]:
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        return await shadow_crud.list_runs(db)


async def _events(db_path: Path) -> list[dict]:
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        return await shadow_crud.list_events(db)


def _cursor(sessions_root: Path) -> dict | None:
    path = sessions_root / SID / lw.CURSOR_FILENAME
    return json.loads(path.read_text()) if path.exists() else None


# ── happy path ───────────────────────────────────────────────────────────


async def test_happy_path_records_run_events_cursor(
    tmp_path, sessions_root, shadow_mode, db_path, transcript
):
    fake = _verdict_claude(tmp_path, [AGREEMENT])
    end = transcript.stat().st_size
    outcome = await lw.run_ledger_worker(
        SID, str(transcript), end, trigger="manual", claude_path=fake, db_path=db_path
    )
    assert outcome["status"] == "ok"
    assert outcome["n_proposals"] == 1

    (run,) = await _runs(db_path)
    assert run["status"] == "ok"
    assert run["trigger"] == "manual"
    assert run["mode"] == "shadow"
    assert run["n_user_turns"] == 2
    assert run["n_proposals"] == 1
    assert run["start_byte"] == 0 and run["end_byte"] == end

    (ev,) = await _events(db_path)
    assert ev["kind"] == "agreement"
    assert ev["turn_ref"] == "u-agree"
    assert ev["quote_verified"] == 1
    assert ev["match_kind"] == "none"  # empty live ledger

    assert _cursor(sessions_root)["last_byte"] == end

    # THE shadow invariant: the live ledger was never written
    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute("SELECT COUNT(*) FROM session_ledger")
        assert (await cur.fetchone())[0] == 0


async def test_second_run_marks_duplicates(
    tmp_path, sessions_root, shadow_mode, db_path, transcript
):
    """A re-covered window (crash-recovery semantics) self-dedups."""
    fake = _verdict_claude(tmp_path, [AGREEMENT])
    end = transcript.stat().st_size
    await lw.run_ledger_worker(SID, str(transcript), end, claude_path=fake, db_path=db_path)
    # simulate a cursor loss → the window is re-covered
    (sessions_root / SID / lw.CURSOR_FILENAME).unlink()
    await lw.run_ledger_worker(SID, str(transcript), end, claude_path=fake, db_path=db_path)
    events = await _events(db_path)
    assert len(events) == 2
    first, second = events
    assert first["duplicate_of"] is None
    assert second["duplicate_of"] == first["id"]


async def test_agreement_matching_against_live_ledger(
    tmp_path, sessions_root, shadow_mode, db_path, transcript
):
    from genesis.db.crud.session_charters import ledger_add

    async with aiosqlite.connect(str(db_path)) as db:
        await ledger_add(
            db,
            session_id=SID,
            text="wire the rollback lever before the widget refactor ships",
        )
    fake = _verdict_claude(tmp_path, [AGREEMENT])
    end = transcript.stat().st_size
    await lw.run_ledger_worker(SID, str(transcript), end, claude_path=fake, db_path=db_path)
    (ev,) = await _events(db_path)
    assert ev["match_kind"] == "exact"
    assert ev["matched_item_id"]


# ── failure paths (cursor must survive) ──────────────────────────────────


async def test_failed_subprocess_preserves_cursor(
    tmp_path, sessions_root, shadow_mode, db_path, transcript
):
    fake = _fake_claude(tmp_path, "import sys\nsys.stdin.read()\nsys.exit(3)\n")
    outcome = await lw.run_ledger_worker(
        SID, str(transcript), transcript.stat().st_size, claude_path=fake, db_path=db_path
    )
    assert outcome["status"] == "failed"
    (run,) = await _runs(db_path)
    assert run["status"] == "failed"
    assert "exit_3" in (run["detail"] or "")
    assert _cursor(sessions_root) is None


async def test_unparseable_output_fails_closed(
    tmp_path, sessions_root, shadow_mode, db_path, transcript
):
    fake = _fake_claude(tmp_path, "import sys\nsys.stdin.read()\nprint('no envelope')\n")
    outcome = await lw.run_ledger_worker(
        SID, str(transcript), transcript.stat().st_size, claude_path=fake, db_path=db_path
    )
    assert outcome["status"] == "failed"
    (run,) = await _runs(db_path)
    assert run["status"] == "failed"
    assert "unparseable" in (run["detail"] or "")
    assert await _events(db_path) == []
    assert _cursor(sessions_root) is None


async def test_missing_transcript_fails_recorded(tmp_path, sessions_root, shadow_mode, db_path):
    outcome = await lw.run_ledger_worker(
        SID, str(tmp_path / "gone.jsonl"), 1000, claude_path="/nonexistent", db_path=db_path
    )
    assert outcome["status"] == "failed"
    (run,) = await _runs(db_path)
    assert "transcript_unreadable" in (run["detail"] or "")
    assert _cursor(sessions_root) is None


async def test_pre_migration_db_preserves_cursor(tmp_path, sessions_root, shadow_mode, transcript):
    """Worktree hook against an un-migrated main DB: the run's shadow write
    no-ops and the cursor must survive so the delta re-covers later."""
    bare = tmp_path / "bare.db"
    async with aiosqlite.connect(str(bare)) as db:
        await db.execute("CREATE TABLE placeholder (x INTEGER)")
        await db.commit()
    shadow_crud._tables_verified = False
    fake = _verdict_claude(tmp_path, [AGREEMENT])
    outcome = await lw.run_ledger_worker(
        SID, str(transcript), transcript.stat().st_size, claude_path=fake, db_path=bare
    )
    assert outcome["status"] == "failed"
    assert outcome["recorded"] is False
    assert _cursor(sessions_root) is None


# ── skip paths (zero trace) ──────────────────────────────────────────────


async def test_mode_off_leaves_zero_trace(
    tmp_path, sessions_root, monkeypatch, db_path, transcript
):
    monkeypatch.setattr(lw, "effective_mode", lambda: "off")
    outcome = await lw.run_ledger_worker(
        SID, str(transcript), transcript.stat().st_size, claude_path="/nonexistent", db_path=db_path
    )
    assert outcome["status"] == "skipped_off"
    assert await _runs(db_path) == []
    assert _cursor(sessions_root) is None
    assert not (sessions_root / SID).exists()


async def test_env_kill_switch(
    tmp_path, sessions_root, shadow_mode, monkeypatch, db_path, transcript
):
    monkeypatch.setenv("GENESIS_LEDGER_SHADOW_DISABLED", "1")
    outcome = await lw.run_ledger_worker(
        SID, str(transcript), transcript.stat().st_size, claude_path="/nonexistent", db_path=db_path
    )
    assert outcome["status"] == "skipped_disabled"
    assert await _runs(db_path) == []


# ── concurrency + windows ────────────────────────────────────────────────


async def test_lock_busy_records_and_preserves_cursor(
    tmp_path, sessions_root, shadow_mode, db_path, transcript
):
    session_dir = sessions_root / SID
    session_dir.mkdir(parents=True)
    holder = (session_dir / lw.LOCK_FILENAME).open("w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        outcome = await lw.run_ledger_worker(
            SID,
            str(transcript),
            transcript.stat().st_size,
            claude_path="/nonexistent",
            db_path=db_path,
        )
    finally:
        holder.close()
    assert outcome["status"] == "lock_busy"
    (run,) = await _runs(db_path)
    assert run["status"] == "lock_busy"
    assert _cursor(sessions_root) is None


async def test_empty_delta_advances_cursor(
    tmp_path, sessions_root, shadow_mode, db_path, transcript
):
    """No new bytes since the cursor → empty_delta, cursor still advances
    (the window is legitimately consumed)."""
    end = transcript.stat().st_size
    session_dir = sessions_root / SID
    session_dir.mkdir(parents=True)
    (session_dir / lw.CURSOR_FILENAME).write_text(
        json.dumps({"last_byte": end, "last_run_ts": None, "runs": 1})
    )
    outcome = await lw.run_ledger_worker(
        SID, str(transcript), end, claude_path="/nonexistent", db_path=db_path
    )
    assert outcome["status"] == "empty_delta"
    (run,) = await _runs(db_path)
    assert run["status"] == "empty_delta"
    assert _cursor(sessions_root)["runs"] == 2


async def test_cursor_beyond_eof_resets(tmp_path, sessions_root, shadow_mode, db_path, transcript):
    session_dir = sessions_root / SID
    session_dir.mkdir(parents=True)
    (session_dir / lw.CURSOR_FILENAME).write_text(
        json.dumps({"last_byte": 10**9, "last_run_ts": None, "runs": 3})
    )
    fake = _verdict_claude(tmp_path, [])
    end = transcript.stat().st_size
    outcome = await lw.run_ledger_worker(
        SID, str(transcript), end, claude_path=fake, db_path=db_path
    )
    assert outcome["status"] == "ok"
    (run,) = await _runs(db_path)
    assert run["start_byte"] == 0
    assert "cursor_beyond_eof_reset" in (run["detail"] or "")
    assert _cursor(sessions_root)["last_byte"] == end


async def test_telemetry_row_recorded(tmp_path, sessions_root, shadow_mode, db_path, transcript):
    """The neural-monitor call_site_last_run row lands when the table exists."""
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS call_site_last_run ("
            " call_site_id TEXT PRIMARY KEY, last_run_at TEXT, provider_used TEXT,"
            " model_id TEXT, response_text TEXT, input_tokens INTEGER,"
            " output_tokens INTEGER, success INTEGER, updated_at TEXT)"
        )
        await db.commit()
    fake = _verdict_claude(tmp_path, [AGREEMENT])
    await lw.run_ledger_worker(
        SID, str(transcript), transcript.stat().st_size, claude_path=fake, db_path=db_path
    )
    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute("SELECT call_site_id, model_id, success FROM call_site_last_run")
        rows = await cur.fetchall()
    assert rows and rows[0][0] == "ambient_ledger_extractor"
