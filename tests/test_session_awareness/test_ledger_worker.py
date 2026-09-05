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
M95 = importlib.import_module(
    "genesis.db.migrations.20260904231054_session_ledger_ambient_extractor"
)

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
    """The schema a real install has once this release's migrations have run.

    The ambient-extractor migration is included even though these tests exercise SHADOW mode: migrations
    run in order, so a DB carrying 0059 without it is not a state any install
    reaches. Stopping at 0059 made the fixture describe a shape that cannot
    exist, and the first writer to depend on its column failed here while
    being correct everywhere real.

    Mode is what keeps these tests shadow-only, not the absence of a column.
    """
    path = tmp_path / "genesis.db"
    shadow_crud._tables_verified = False
    async with aiosqlite.connect(str(path)) as db:
        await M58.up(db)
        await M59.up(db)
        await M95.up(db)
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

M90 = M95   # historical alias for the ambient-extractor migration module


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
        await M90.up(db)
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


# ── retryable promotion (the sweep) ──────────────────────────────────────
#
# Promotion reads its candidates back from the shadow store, not from one
# run's in-memory events — the shadow row IS the retry state. These tests
# replay the two review findings that forced that design: a failed live write
# silently losing the agreement forever (cursor already advanced, re-covered
# window marked duplicate), and a foreground row landing between observation
# and promotion producing a duplicate.


async def _fabricate_run(
    db_path, events, *, prompt_version=None, trigger="manual", run_id="fab-run-1",
    mode="live",
):
    """One recorded run + events, exactly as `_record_run` would write them.

    `mode` defaults to "live" because the sweep only considers events proposed
    UNDER the live promise — a shadow-stamped fixture is silently ineligible,
    which would make these tests pass for the wrong reason.
    """
    async with aiosqlite.connect(str(db_path)) as db:
        await shadow_crud.record_run(
            db,
            run_id=run_id,
            session_id=SID,
            started_at="2026-09-01T00:00:00+00:00",
            finished_at="2026-09-01T00:00:05+00:00",
            start_byte=0,
            end_byte=100,
            trigger=trigger,
            status="ok",
            mode=mode,
            prompt_version=prompt_version or lw.PROMPT_VERSION,
            events=events,
        )


def _fab_event(text, *, ev_id="fab-ev-1", quote_verified=True):
    return {
        "id": ev_id,
        "observed_at": "2026-09-01T00:00:01+00:00",
        "kind": "agreement",
        "text": text,
        "turn_ref": "u-agree",
        "quote_preview": "yes, do that",
        "quote_verified": quote_verified,
        "match_kind": "none",
    }


async def _shadow_event_rows(db_path):
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM session_ledger_shadow_events ORDER BY observed_at"
        )
        return [dict(r) for r in await cur.fetchall()]


async def test_a_failed_promotion_is_retried_by_the_next_run(
    tmp_path, sessions_root, live_mode, live_db_path, transcript, monkeypatch
):
    """The cursor-coupling P1, replayed end-to-end.

    Run 1's live write fails after the shadow write; the cursor has advanced
    (correctly — the shadow guarantee held). Run 2 re-covers the window, so
    its fresh proposals are marked duplicates of run 1's — the exact state the
    finding said made the agreement unrecoverable. The sweep recovers it: run
    1's ROOT event is still qualifying and unpromoted.
    """
    import genesis.db.crud.session_charters as charters_crud

    real_ledger_add = charters_crud.ledger_add

    async def _always_fails(*a, **kw):
        raise RuntimeError("simulated live-write failure")

    monkeypatch.setattr(charters_crud, "ledger_add", _always_fails)
    first = await _run_once(tmp_path, transcript, live_db_path, [AGREEMENT])
    # Two SEPARATE claims that this assertion used to conflate. The shadow
    # write surviving is about the ROW; the run's status is about whether every
    # write this run attempted succeeded — and one of them did not.
    #
    # Reporting `ok` here is what let a sweep whose every live promotion raised
    # look identical to a clean one, to the neural monitor AND to
    # scripts/ledger_shadow_worker.py, which prints to stderr only on
    # failed/timeout and therefore stayed silent about it.
    assert first["status"] == "failed", (
        "a run whose live writes all failed is not an `ok` run"
    )
    assert first["promotion_failed_rows"] >= 1
    assert len(await _events(live_db_path)) >= 1, (
        "the shadow write must survive a live failure — that is the guarantee, "
        "and it is about the ROW, not about the status"
    )
    assert first["promoted"] == 0
    assert await _ledger(live_db_path) == []
    assert _cursor(sessions_root)["last_byte"] > 0, (
        "cursor advance is deliberately decoupled from promotion outcome"
    )

    # Run 2: live write healthy again; re-cover the same window.
    monkeypatch.setattr(charters_crud, "ledger_add", real_ledger_add)
    (sessions_root / SID / lw.CURSOR_FILENAME).write_text(json.dumps({"last_byte": 0}))
    second = await _run_once(tmp_path, transcript, live_db_path, [AGREEMENT])
    assert second["promoted"] == 1, "the sweep did not retry the failed promotion"

    (row,) = await _ledger(live_db_path)
    assert row["text"] == AGREEMENT["text"]
    promoted = [e for e in await _shadow_event_rows(live_db_path) if e["promoted_item_id"]]
    assert len(promoted) == 1 and promoted[0]["promoted_item_id"] == row["id"], (
        "the promoted event must point at the ledger row it became"
    )


async def test_a_foreground_row_written_after_observation_is_not_duplicated(
    tmp_path, sessions_root, live_db_path, live_mode
):
    """The TOCTOU P2, replayed at the seam it names.

    Observation-time matching saw an empty ledger (match_kind='none'); a
    foreground `session_ledger_add` then records the same agreement before
    promotion runs. The in-transaction recheck must see it, write the
    discovered match back onto the event, and skip — never insert a twin.
    """
    import genesis.db.crud.session_charters as charters_crud

    text = "wire the rollback lever before the widget refactor ships"
    await _fabricate_run(live_db_path, [_fab_event(text)])

    async with aiosqlite.connect(str(live_db_path)) as db:
        db.row_factory = aiosqlite.Row
        await charters_crud.upsert_stub(db, SID)
        await charters_crud.ledger_add(db, session_id=SID, text=text)

    out = await lw._promote_live(live_db_path, SID, trigger="manual")
    assert out["promoted"] == 0
    assert out["disqualified_at_write"] == 1

    ledger = await _ledger(live_db_path)
    assert len(ledger) == 1 and ledger[0]["added_by"] == "foreground", (
        "the recheck failed: a duplicate ambient row was inserted"
    )
    (ev,) = await _shadow_event_rows(live_db_path)
    assert ev["match_kind"] != "none" and ev["matched_item_id"] == ledger[0]["id"], (
        "the discovered match must be recorded on the event (it is what "
        "permanently disqualifies it from future sweeps)"
    )


async def test_a_crashed_mark_self_heals_without_a_duplicate(
    tmp_path, sessions_root, live_db_path, live_mode
):
    """The crash window between ledger commit and event mark.

    `ledger_add` commits, then the process dies before `promoted_item_id` is
    written. The row exists; the event still looks unpromoted. The next
    sweep's recheck must find the row as a match of its own text and
    disqualify the event — never insert it again.
    """
    import genesis.db.crud.session_charters as charters_crud

    text = "give the ingest queue its own retention policy"
    await _fabricate_run(live_db_path, [_fab_event(text)])
    async with aiosqlite.connect(str(live_db_path)) as db:
        db.row_factory = aiosqlite.Row
        await charters_crud.upsert_stub(db, SID)
        # The half-finished promotion: the ledger row landed, the mark did not.
        await charters_crud.ledger_add(
            db, session_id=SID, text=text, added_by=lw.PROMOTION_ADDED_BY,
            evidence="yes, do that",
        )

    out = await lw._promote_live(live_db_path, SID, trigger="manual")
    assert out["promoted"] == 0
    assert len(await _ledger(live_db_path)) == 1, "self-heal inserted a duplicate"


async def test_a_stale_prompt_generation_backlog_is_never_promoted(
    tmp_path, sessions_root, live_db_path, live_mode
):
    """Flipping live must not ship the backlog an OLD prompt produced.

    The v1 corpus was adjudicated at 43% wanted — the reason v2 exists. The
    sweep therefore promotes only events whose run carries the CURRENT
    prompt_version; everything older stays shadow-only forever.
    """
    await _fabricate_run(
        live_db_path,
        [_fab_event("an agreement the retired prompt extracted")],
        prompt_version="v0-retired",
    )
    out = await lw._promote_live(live_db_path, SID, trigger="manual")
    assert out["promoted"] == 0
    assert out["qualifying"] == 0, "a stale-prompt event entered the sweep"
    assert await _ledger(live_db_path) == []


# ── session-id containment ───────────────────────────────────────────────


async def test_an_unsafe_session_id_never_touches_the_filesystem(
    tmp_path, sessions_root, shadow_mode, db_path, transcript
):
    """The id becomes ONE path component under the sessions root; a traversal
    value must skip the run entirely rather than write state elsewhere."""
    for bad in ("../evil", "a/b", "", "x" * 256):
        out = await lw.run_ledger_worker(
            bad, str(transcript), transcript.stat().st_size, db_path=db_path
        )
        assert out["status"] == "skipped_bad_session_id", (bad, out)
    assert not (sessions_root.parent / "evil").exists()
    assert not sessions_root.exists(), "no session dir may be created for a bad id"


async def test_backfill_rejects_an_unsafe_session_id(
    tmp_path, sessions_root, shadow_mode, db_path, transcript
):
    out = await lw.run_backfill("../evil", str(transcript), db_path=db_path)
    assert out["status"] == "skipped_bad_session_id"
    assert not (sessions_root.parent / "evil").exists()


# ── review round 1: what the sweep SELECTS, and what the run REPORTS ─────
#
# Two finding classes from the adversarial pass. Class A: the sweep filtered
# on properties of the candidate ROW, but eligibility lives on its run and its
# duplicate chain — so it both over-selected (retroactive) and under-selected
# (chains suppressed forever). Class B: the promotion path was built as a
# write and not as an OBSERVABLE write.


async def test_shadow_era_proposals_are_not_promoted_when_live_is_switched_on(
    tmp_path, sessions_root, live_db_path, live_mode
):
    """The flip must not be RETROACTIVE.

    Every proposal recorded while the config promised "the live session_ledger
    is NEVER written" carries mode='shadow'. Without a clause on it, setting
    the two live keys does not begin promoting from now on — it drains the
    whole backlog gathered under the opposite promise, 5 rows per compaction.
    MEASURED on the live corpus when this was found: 550/550 stored events
    were mode='shadow'. The two-key gate proves CURRENT intent; applying it
    backwards grants more write authority than the operator asked for.
    """
    await _fabricate_run(
        live_db_path,
        [_fab_event("an agreement observed while the promise was shadow")],
        mode="shadow",
    )
    out = await lw._promote_live(live_db_path, SID, trigger="manual")
    assert out["promoted"] == 0
    assert out["qualifying"] == 0, "a shadow-era proposal entered the sweep"
    assert await _ledger(live_db_path) == []


async def test_a_duplicate_of_an_ineligible_root_is_still_promoted(
    tmp_path, sessions_root, live_db_path, live_mode
):
    """The mirror-image defect: silent, permanent SUPPRESSION.

    match_proposals builds its dedup pool from ALL prior events of the
    session, unfiltered by mode or prompt generation. So a live v2 re-proposal
    links to a shadow-era root — and a `duplicate_of IS NULL` clause would
    then exclude the re-proposal, while the mode clause excludes its root. The
    agreement is dropped forever with no log line and no counter.

    Idempotency does not depend on that clause: the in-transaction recheck is
    the guarantee (mutation-verified), so letting the chain through costs one
    recheck and permanently marks it.
    """
    text = "stop vendoring the parser and depend on it upstream instead"
    await _fabricate_run(
        live_db_path, [_fab_event(text, ev_id="root-shadow")],
        mode="shadow", run_id="run-shadow",
    )
    await _fabricate_run(
        live_db_path,
        [dict(_fab_event(text, ev_id="reproposal-live"), duplicate_of="root-shadow")],
        mode="live", run_id="run-live",
    )

    out = await lw._promote_live(live_db_path, SID, trigger="manual")
    assert out["promoted"] == 1, (
        "the re-proposal was suppressed by its ineligible root — the agreement "
        "would never reach the ledger"
    )
    (row,) = await _ledger(live_db_path)
    assert row["text"] == text


async def test_promotion_counters_reach_the_run_outcome(
    tmp_path, sessions_root, live_mode, live_db_path, transcript, monkeypatch
):
    """A sweep where every candidate FAILED must not read as one that found
    nothing. Both recorded promoted=0 and nothing else — byte-identical — with
    the only signal in a stderr log nobody tails."""
    import genesis.db.crud.session_charters as charters_crud

    async def _always_fails(*a, **kw):
        raise RuntimeError("simulated live-write failure")

    monkeypatch.setattr(charters_crud, "ledger_add", _always_fails)
    out = await _run_once(tmp_path, transcript, live_db_path, [AGREEMENT])

    assert out["promoted"] == 0
    assert out["qualifying"] == 1, "the candidate count is what distinguishes the two"
    assert out["promotion_failed_rows"] == 1, out
    assert await _ledger(live_db_path) == []


async def test_the_unreadable_context_note_reaches_the_run_row(
    tmp_path, sessions_root, live_mode, live_db_path, transcript, monkeypatch
):
    """`_base_row` joins detail_notes at CALL time, so a note appended after
    `_record_run` is written to a string that was already rendered. The note
    on the fail-closed novelty gate — the one branch with no other signal —
    never reached the database."""

    async def _unreadable(_db_path, _sid):
        return [], [], False

    monkeypatch.setattr(lw, "_load_match_context", _unreadable)
    out = await _run_once(tmp_path, transcript, live_db_path, [AGREEMENT])
    assert out["promoted"] == 0

    (run,) = [r for r in await _runs(live_db_path) if r["status"] == "ok"]
    assert run["detail"] and "promotion_skipped_match_context_unreadable" in run["detail"], (
        f"the note never reached session_ledger_shadow_runs.detail: {run['detail']!r}"
    )


async def test_a_mid_run_rollback_to_shadow_stops_the_promotion(
    tmp_path, sessions_root, live_db_path, transcript, monkeypatch
):
    """The gate is read at entry, then a Haiku call runs for up to 120s. The
    documented emergency rollback is `mode: shadow`; an operator doing that
    mid-run must not still get this run's promotions."""
    modes = iter(["live", "shadow"])  # entry read, then the pre-write re-read
    monkeypatch.setattr(lw, "effective_mode", lambda: next(modes, "shadow"))

    out = await _run_once(tmp_path, transcript, live_db_path, [AGREEMENT])
    assert out["status"] == "ok", "the shadow record must still be written"
    assert out["promoted"] == 0
    assert await _ledger(live_db_path) == [], "promoted after the operator rolled back"


def test_the_session_id_guard_is_one_definition_not_a_copy():
    """It diverged the first time it was written: `^…$` accepts a trailing
    newline in Python where `\\A…\\Z` does not, and the cap differed (128 vs
    255). A regex copied "to mirror" a sibling must be diffed against it."""
    from genesis.session_charter import _SAFE_SESSION_ID

    assert lw._SESSION_ID_RE is _SAFE_SESSION_ID, "the copy came back"
    assert not lw._SESSION_ID_RE.match("abc\n"), "trailing newline accepted"
    assert lw._SESSION_ID_RE.match("a" * 255)
    assert not lw._SESSION_ID_RE.match("a" * 256)


# ── Round-8/9 review fixes: renewal, atomicity, mid-sweep rollback, honesty ──


async def test_a_renewed_agreement_promotes_after_its_old_row_closed(
    tmp_path, sessions_root, live_mode, live_db_path
):
    """A closed ledger row must not permanently disqualify its own renewal.

    The transactional recheck was narrowed to open rows in an earlier round —
    but observation-time matching still ran against ALL statuses, stamped the
    renewal `exact` against the finished row, and the sweep's
    `match_kind = 'none'` prefilter rejected it before the recheck could run.
    The fresh-evidence finding: only the inner SELECT was narrowed. This test
    drives the OBSERVATION path (`_load_match_context` -> `match_proposals`),
    not a pre-stamped fixture, so the prefilter is actually exercised.
    """
    import genesis.db.crud.session_charters as charters_crud

    text = "ship the retention follow-up for the shadow store"
    async with aiosqlite.connect(str(live_db_path)) as db:
        await charters_crud.upsert_stub(db, SID)
        old_id = await charters_crud.ledger_add(db, session_id=SID, text=text)
        await charters_crud.ledger_update(db, item_id=old_id, status="done")

    # The observation half: the match pool the extractor's stamping reads
    # from must already exclude the closed row, or match_proposals stamps the
    # renewal `exact` and the sweep prefilter rejects it forever.
    items, _priors, ok = await lw._load_match_context(live_db_path, SID)
    assert ok
    assert items == [], "a closed row reached the observation-time match pool"

    # The sweep half: an event stamped 'none' (as the open-only pool now
    # yields) must promote even though a closed twin exists.
    await _fabricate_run(live_db_path, [_fab_event(text)])
    out = await lw._promote_live(live_db_path, SID, trigger="manual")
    assert out["promoted"] == 1, (
        "the renewal was disqualified by its closed predecessor — the renewed "
        "commitment can never reach the ledger"
    )
    rows = await _ledger(live_db_path)
    assert [r["status"] for r in rows] == ["done", "open"]


async def test_promotion_insert_and_link_land_atomically(
    tmp_path, sessions_root, live_mode, live_db_path
):
    """Crash between the ledger insert and the event link mints no orphan.

    With `ledger_add` committing on its own, a crash in the gap left a row no
    event claimed: invisible to the (open-only) recheck once it closed, minted
    as a duplicate by the still-qualifying event, and read by the leak
    invariant as unattributed. Insert + link now share one transaction — this
    simulates the crash as a rollback at the exact old commit point and
    asserts NEITHER side landed, then that the retry promotes exactly once.
    """
    import genesis.db.crud.session_charters as charters_crud

    text = "wire the audit trail into the flip decision"
    await _fabricate_run(live_db_path, [_fab_event(text)])

    async with aiosqlite.connect(str(live_db_path)) as db:
        db.row_factory = aiosqlite.Row
        await charters_crud.upsert_stub(db, SID)
        await db.execute("BEGIN IMMEDIATE")
        item_id = await charters_crud.ledger_add(
            db, session_id=SID, text=text,
            added_by=lw.PROMOTION_ADDED_BY, commit=False,
        )
        # crash before the link would have been written
        await db.rollback()

    rows = await _ledger(live_db_path)
    assert rows == [], f"the insert survived the crash alone: {rows}"
    events = await _shadow_event_rows(live_db_path)
    assert events[0]["promoted_item_id"] is None
    assert item_id  # the id existed in-transaction; nothing durable did

    out = await lw._promote_live(live_db_path, SID, trigger="manual")
    assert out["promoted"] == 1
    (row,) = await _ledger(live_db_path)
    events = await _shadow_event_rows(live_db_path)
    assert events[0]["promoted_item_id"] == row["id"], (
        "promotion landed without its attribution link"
    )


async def test_mode_rollback_midsweep_stops_before_the_next_write(
    tmp_path, sessions_root, live_db_path, monkeypatch
):
    """An operator's `mode: shadow` during the sweep stops the NEXT candidate.

    The pre-sweep recheck closed the window before the sweep; five separate
    transactions with lock waits between them are a real interval too, and
    the emergency-rollback contract is about the next WRITE, not the next
    sweep. `effective_mode` reads config fresh per call (no cache), which is
    what makes a per-candidate re-read meaningful at all.
    """
    modes = iter(["live", "shadow"])
    monkeypatch.setattr(lw, "effective_mode", lambda: next(modes, "shadow"))
    await _fabricate_run(
        live_db_path,
        [_fab_event("first agreement", ev_id="ev-a"),
         _fab_event("second agreement", ev_id="ev-b")],
    )
    out = await lw._promote_live(live_db_path, SID, trigger="manual")
    assert out["promoted"] == 1, (out, "the first candidate ran under a live gate")
    assert out["mode_stopped"] is True
    rows = await _ledger(live_db_path)
    assert len(rows) == 1, "the rolled-back mode still got a second write"


async def test_an_aborted_sweep_is_not_an_ok_run(
    tmp_path, sessions_root, live_mode, tmp_path_factory
):
    """`sweep_error` distinguishes 'could not sweep' from 'nothing qualified'.

    A sweep that died at connect returned the same all-zero counters as one
    with no candidates, so the run reported ok while zero promotion work
    happened — the exact silent-failure shape the counters were added to kill,
    one layer up.
    """
    missing = tmp_path_factory.mktemp("gone") / "no-such-subdir" / "genesis.db"
    out = await lw._promote_live(missing, SID, trigger="manual")
    assert out["promoted"] == 0
    assert out["sweep_error"] is True, (
        "an aborted sweep is indistinguishable from an empty one"
    )


async def test_early_failures_carry_the_prompt_version(
    tmp_path, sessions_root, shadow_mode, live_db_path
):
    """lock_busy and transcript-unreadable rows are CURRENT runs, and say so.

    The report scopes its health population by prompt_version; recording these
    rows without one filed them as legacy, so worker health looked better
    precisely when runs were failing before extraction.
    """
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")

    # transcript_unreadable: point the worker at a path that does not exist.
    out = await lw.run_ledger_worker(
        SID, str(tmp_path / "missing.jsonl"), 10, trigger="manual",
        db_path=live_db_path,
    )
    assert out["status"] == "failed"

    # lock_busy: hold the per-session flock ourselves.
    session_dir = lw._sessions_root() / SID
    session_dir.mkdir(parents=True, exist_ok=True)
    with (session_dir / lw.LOCK_FILENAME).open("w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        out = await lw.run_ledger_worker(
            SID, str(transcript), 0, trigger="manual", db_path=live_db_path,
        )
        assert out["status"] == "lock_busy"

    async with aiosqlite.connect(str(live_db_path)) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT status, prompt_version FROM session_ledger_shadow_runs"
        )
        rows = [dict(r) for r in await cur.fetchall()]
    assert rows, "no run rows recorded"
    for r in rows:
        assert r["prompt_version"] == lw.PROMPT_VERSION, (
            f"{r['status']} row lost its version — filed as legacy by the report"
        )


async def test_promotion_failure_re_marks_the_durable_run_row(
    tmp_path, sessions_root, live_mode, live_db_path, transcript, monkeypatch
):
    """The run ROW must say failed, not just telemetry and the outcome dict.

    The shadow row is deliberately written status='ok' before the sweep (audit
    trail first) — but the report's status histogram and failure rate read run
    rows, so a live promotion failure that only flipped telemetry looked
    healthy exactly where the flip decision looks. The row is re-marked after
    the fact."""
    import genesis.db.crud.session_charters as charters_crud

    async def _boom(*a, **k):
        raise RuntimeError("simulated ledger write failure")

    monkeypatch.setattr(charters_crud, "ledger_add", _boom)
    out = await _run_once(tmp_path, transcript, live_db_path, [AGREEMENT])
    assert out["status"] == "failed"
    assert out["promotion_failed_rows"] == 1

    (run,) = await _runs(live_db_path)
    assert run["status"] == "failed", (
        "the durable run row still says ok — the report's histogram reads rows"
    )
    assert "promotion_failed=1" in (run["detail"] or "")
