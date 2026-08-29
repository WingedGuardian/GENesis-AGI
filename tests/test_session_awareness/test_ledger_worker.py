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


async def test_cursor_never_regresses_on_out_of_order_runs(
    tmp_path, sessions_root, shadow_mode, db_path, transcript
):
    """Two compactions can spawn workers that complete out of order (the
    flock serializes, but not in spawn order). A later-spawned worker with
    a HIGHER end-byte finishing first must not have its cursor progress
    clobbered by the earlier worker's smaller end-byte."""
    fake = _verdict_claude(tmp_path, [])
    end = transcript.stat().st_size
    # worker B (spawned second, higher end-byte) completes first
    await lw.run_ledger_worker(SID, str(transcript), end, claude_path=fake, db_path=db_path)
    assert _cursor(sessions_root)["last_byte"] == end
    # worker A (spawned first, lower end-byte) completes after
    outcome = await lw.run_ledger_worker(
        SID, str(transcript), end - 50, claude_path=fake, db_path=db_path
    )
    assert outcome["status"] == "empty_delta"
    assert _cursor(sessions_root)["last_byte"] == end  # monotonic — never regresses


# ── backfill mode ────────────────────────────────────────────────────────


async def test_backfill_windows_newest_capped_cursor_untouched(
    tmp_path, sessions_root, shadow_mode, db_path
):
    t = tmp_path / f"{SID}.jsonl"
    entries = []
    for i in range(45):
        entries.append(_typed(f"please handle work item number {i} today", f"u-{i}"))
    t.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    fake = _verdict_claude(tmp_path, [dict(AGREEMENT, quote="please handle work item")])
    outcome = await lw.run_backfill(
        SID, str(t), turns_per_window=20, max_windows=2, claude_path=fake, db_path=db_path
    )
    assert outcome["status"] == "ok"
    assert outcome["windows"] == 2  # 45 turns → 3 windows → newest 2 kept
    runs = await _runs(db_path)
    assert len(runs) == 2
    assert all(r["trigger"] == "backfill" for r in runs)
    assert all("backfill_window" in (r["detail"] or "") for r in runs)
    assert _cursor(sessions_root) is None  # NEVER touched by backfill


async def test_backfill_cross_window_dedup(
    tmp_path, sessions_root, shadow_mode, db_path, transcript
):
    """The same agreement proposed in two backfill windows dedups via
    the accumulated priors (duplicate_of on the second)."""
    fake = _verdict_claude(tmp_path, [AGREEMENT])
    outcome = await lw.run_backfill(
        SID,
        str(transcript),
        turns_per_window=1,
        max_windows=2,
        claude_path=fake,
        db_path=db_path,
    )
    assert outcome["status"] == "ok"
    events = await _events(db_path)
    assert len(events) == 2
    dups = [e["duplicate_of"] for e in events]
    assert dups.count(None) == 1
    assert dups.count(events[0]["id"]) == 1 or dups.count(events[1]["id"]) == 1


async def test_backfill_partial_failure_reported_honestly(
    tmp_path, sessions_root, shadow_mode, db_path
):
    """A backfill where some windows fail must not report top-level ok
    (Codex P2): callers need to know the tuning data is incomplete."""
    t = tmp_path / f"{SID}.jsonl"
    entries = [_typed(f"please handle work item number {i} today", f"u-{i}") for i in range(4)]
    t.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    # fake claude alternates: first call OK, second call garbage
    fake = _fake_claude(
        tmp_path,
        f"""
        import json, sys
        from pathlib import Path
        sys.stdin.read()
        flag = Path({str(tmp_path / "called")!r})
        if flag.exists():
            print("garbage — unparseable")
        else:
            flag.write_text("1")
            print(json.dumps({{"result": '{{"agreements": [], "pivots": []}}'}}))
        """,
    )
    outcome = await lw.run_backfill(
        SID, str(t), turns_per_window=2, max_windows=2, claude_path=fake, db_path=db_path
    )
    assert outcome["status"] == "partial"
    assert sorted(outcome["outcomes"]) == ["failed", "ok"]


async def test_backfill_respects_mode_off(
    tmp_path, sessions_root, monkeypatch, db_path, transcript
):
    monkeypatch.setattr(lw, "effective_mode", lambda: "off")
    outcome = await lw.run_backfill(
        SID, str(transcript), claude_path="/nonexistent", db_path=db_path
    )
    assert outcome["status"] == "skipped_off"
    assert await _runs(db_path) == []


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


# ── live promotion ───────────────────────────────────────────────────────
#
# Everything above runs under the `shadow_mode` fixture, so until this section
# existed NOTHING exercised the live branch. Mutation-measured before it was
# written: changing `if recorded and mode == "live":` to `if recorded:` — a
# one-token edit that makes the shadow lane write to the live ledger — left the
# suite fully green. A mode gate that nothing holds is not a gate.

M87 = importlib.import_module(
    "genesis.db.migrations.0087_session_ledger_ambient_extractor"
)


@pytest.fixture
def live_mode(monkeypatch):
    monkeypatch.setattr(lw, "effective_mode", lambda: "live")


@pytest.fixture
async def live_db_path(tmp_path) -> Path:
    """Like `db_path`, plus the migration that admits the extractor's provenance."""
    path = tmp_path / "genesis-live.db"
    shadow_crud._tables_verified = False
    async with aiosqlite.connect(str(path)) as db:
        await M58.up(db)
        await M59.up(db)
        await M87.up(db)
        await db.commit()
    yield path
    shadow_crud._tables_verified = False


async def _ledger(db_path: Path) -> list[dict]:
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM session_ledger ORDER BY created_at")
        return [dict(r) for r in await cur.fetchall()]


async def _run_once(tmp_path, transcript, db_path, agreements, *, trigger="manual",
                    pivots=()):
    fake = _verdict_claude(tmp_path, agreements, pivots)
    end = transcript.stat().st_size
    return await lw.run_ledger_worker(
        SID, str(transcript), end, trigger=trigger, claude_path=fake,
        db_path=db_path,
    )


async def test_shadow_mode_writes_nothing_to_the_live_ledger(
    tmp_path, sessions_root, shadow_mode, live_db_path, transcript
):
    """The gate itself. This is the test whose absence let a one-token edit turn
    the shadow lane into the live lane with CI green."""
    out = await _run_once(tmp_path, transcript, live_db_path, [AGREEMENT])
    assert out["status"] == "ok"
    assert out.get("promoted", 0) == 0
    assert await _ledger(live_db_path) == []


async def test_live_mode_promotes_a_qualifying_agreement(
    tmp_path, sessions_root, live_mode, live_db_path, transcript
):
    out = await _run_once(tmp_path, transcript, live_db_path, [AGREEMENT])
    assert out["promoted"] == 1

    (row,) = await _ledger(live_db_path)
    assert row["text"] == AGREEMENT["text"]
    assert row["added_by"] == "ambient_ledger_extractor"
    assert row["status"] == "open"
    # The filter DEMANDS a verified quote; the row must then be able to show it.
    # Without this the live leak invariant fails on every promotion, and a human
    # reading an autonomously-added row has no source for it.
    assert row["evidence"], "promoted row carries no evidence"


async def test_the_mirror_is_actually_refreshed(
    tmp_path, sessions_root, live_mode, live_db_path, transcript
):
    """`charter.md` must exist after a promotion.

    It did not. The connection carried no row factory, so `crud.get`'s
    `dict(row)` raised, `refresh_mirror`'s best-effort `except` swallowed it,
    and the run reported success while the mirror never updated — for every
    promotion, always.
    """
    await _run_once(tmp_path, transcript, live_db_path, [AGREEMENT])
    assert (sessions_root / SID / "charter.md").exists(), (
        "charter.md was not written — the mirror refresh failed silently"
    )


async def test_backfill_never_promotes(
    tmp_path, sessions_root, live_mode, live_db_path, transcript
):
    """Backfill replays historical windows; those agreements belong to sessions
    that have already ended."""
    out = await _run_once(
        tmp_path, transcript, live_db_path, [AGREEMENT], trigger="backfill"
    )
    assert out["promoted"] == 0
    assert await _ledger(live_db_path) == []


async def test_an_unverified_quote_is_not_promoted(
    tmp_path, sessions_root, live_mode, live_db_path, transcript
):
    """The quote must be findable in the transcript, or the row is a paraphrase
    of something that may never have been said."""
    bogus = dict(AGREEMENT, quote="a sentence that appears nowhere in the delta")
    out = await _run_once(tmp_path, transcript, live_db_path, [bogus])
    assert out["promoted"] == 0
    assert await _ledger(live_db_path) == []


async def test_a_pivot_is_not_promoted(
    tmp_path, sessions_root, live_mode, live_db_path, transcript
):
    """A pivot is a change of direction, not a commitment — it reads badly as a
    checkbox someone has to close."""
    pivot = {"turn": 1, "text": "switch to the other approach", "quote": "yes, do that"}
    out = await _run_once(tmp_path, transcript, live_db_path, [], pivots=[pivot])
    assert out["promoted"] == 0
    assert await _ledger(live_db_path) == []


async def test_a_second_run_does_not_promote_the_same_agreement_twice(
    tmp_path, sessions_root, live_mode, live_db_path, transcript
):
    """Idempotency across runs is NOT inherited from the shadow cursor.

    The cursor's re-cover guarantee is a shadow guarantee; promotion needs its
    own, which is why the filter gates on `duplicate_of` and on not matching an
    existing ledger row.
    """
    first = await _run_once(tmp_path, transcript, live_db_path, [AGREEMENT])
    assert first["promoted"] == 1

    cursor_path = sessions_root / SID / lw.CURSOR_FILENAME
    cursor_path.write_text(json.dumps({"last_byte": 0}))

    await _run_once(tmp_path, transcript, live_db_path, [AGREEMENT])
    assert len(await _ledger(live_db_path)) == 1, (
        "the same agreement was promoted twice"
    )


async def test_promotion_is_skipped_when_the_match_context_is_unreadable(
    tmp_path, sessions_root, live_mode, live_db_path, transcript, monkeypatch
):
    """Fail CLOSED. Both novelty signals come from that read, so a failure makes
    everything look promotable — including duplicates of rows already written."""

    async def _unreadable(_db_path, _sid):
        return [], [], False

    monkeypatch.setattr(lw, "_load_match_context", _unreadable)
    out = await _run_once(tmp_path, transcript, live_db_path, [AGREEMENT])
    assert out["promoted"] == 0
    assert await _ledger(live_db_path) == []


async def test_the_cap_bounds_a_single_run(
    tmp_path, sessions_root, live_mode, live_db_path, transcript, monkeypatch
):
    """And what it drops is LOGGED, never silently truncated."""
    monkeypatch.setattr(lw, "PROMOTION_CAP", 2)
    # Texts must be genuinely DIFFERENT, not merely numbered. A first version
    # used "distinct agreement number {i}", which the duplicate detector
    # correctly collapsed to one — near-identical strings ARE duplicates, and
    # the fixture was testing the deduper rather than the cap.
    many = [
        dict(AGREEMENT, text=t)
        for t in (
            "wire the rollback lever before the widget refactor ships",
            "move the nightly export off the shared scheduler entirely",
            "stop vendoring the parser and depend on it upstream instead",
            "give the ingest queue its own retention policy",
            "replace the polling loop with an event subscription",
        )
    ]
    out = await _run_once(tmp_path, transcript, live_db_path, many)
    assert out["promoted"] == 2
    assert len(await _ledger(live_db_path)) == 2
