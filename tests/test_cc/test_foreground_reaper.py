"""Tests for the foreground-session liveness reaper (D3).

Covers the pure tail classifier (``dark_signal`` + age guard) and the full
``reap_dark_foreground`` behavior matrix: reap-skips-live, reap-checkpoints-dead,
notify-only-on-unanswered-user (with the mid-flight age guard), suppressed on
clean idle / when covered by park or dispatch, promise=shadow, non-telegram
graceful, mode gating, and the checkpoint race guard. Real in-memory db; fake
runtime with a mocked outreach pipeline; the transcript read is monkeypatched so
the classifier's input is controlled without touching disk.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from genesis.cc import foreground_reaper as fr
from genesis.db.crud import cc_sessions
from genesis.db.crud import direct_session_queue as dsq

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)
OLD = "2026-07-20T00:00:00+00:00"  # ~60h before NOW → older than the 24h cutoff
RECENT = "2026-07-22T11:30:00+00:00"  # 30 min before NOW → NEWER than the cutoff


# --- transcript entry builders -------------------------------------------------
def _user(text: str, ts: str) -> dict:
    return {"type": "user", "promptSource": "typed", "timestamp": ts, "message": {"content": text}}


def _assistant(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


# --- pure classifier -----------------------------------------------------------
def test_dark_signal_unanswered_user():
    sig, ts = fr.dark_signal([_user("do the thing", OLD)])
    assert sig == "unanswered_user"
    assert ts == OLD


def test_dark_signal_clean_when_answered():
    sig, _ = fr.dark_signal([_user("do X", OLD), _assistant("here you go")])
    assert sig == "clean"


def test_dark_signal_promise_when_answered_with_deferral():
    sig, _ = fr.dark_signal(
        [_user("research X", OLD), _assistant("On it — I'll report back when it finishes.")]
    )
    assert sig == "promise"


def test_dark_signal_unknown_without_user_turn():
    sig, ts = fr.dark_signal([_assistant("orphan")])
    assert sig == "unknown"
    assert ts is None


def test_ts_older_than_guard():
    cutoff = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
    assert fr._ts_older_than(OLD, cutoff) is True
    assert fr._ts_older_than(RECENT, cutoff) is False
    assert fr._ts_older_than(None, cutoff) is False
    assert fr._ts_older_than("not-a-date", cutoff) is False


# --- reaper harness ------------------------------------------------------------
def _rt(db):
    rt = MagicMock()
    rt._db = db
    pipe = MagicMock()
    pipe.submit_urgent = AsyncMock()
    pipe._forum_chat_id = None
    rt._outreach_pipeline = pipe
    return rt


async def _seed(
    db,
    *,
    sid="s1",
    cc_sid="cc-s1",
    last_activity=OLD,
    channel="telegram",
    chat_id="12345",
    status="active",
):
    await cc_sessions.create(
        db,
        id=sid,
        session_type="foreground",
        model="sonnet",
        effort="medium",
        status=status,
        user_id="tg-999",
        channel=channel,
        started_at=last_activity,
        last_activity_at=last_activity,
        source_tag="foreground",
        chat_id=chat_id,
    )
    if cc_sid:
        await cc_sessions.update_cc_session_id(db, sid, cc_session_id=cc_sid)
    return sid


def _patch_tail(monkeypatch, entries):
    monkeypatch.setattr(fr, "_read_tail_entries", lambda *a, **k: list(entries))


async def _obs_count(db) -> int:
    cur = await db.execute(
        "SELECT COUNT(*) FROM observations WHERE type = 'dark_foreground_session'"
    )
    return (await cur.fetchone())[0]


async def test_skips_live_foreground(db, monkeypatch):
    await _seed(db, last_activity=RECENT)  # idle < 24h → not stale
    _patch_tail(monkeypatch, [_user("x", RECENT)])
    res = await fr.reap_dark_foreground(_rt(db), now=NOW, idle_hours=24, mode="notify")
    assert res["scanned"] == 0 and res["reaped"] == 0


async def test_checkpoints_dead_and_notifies_on_unanswered(db, monkeypatch):
    sid = await _seed(db)
    _patch_tail(monkeypatch, [_user("finish my research", OLD)])
    rt = _rt(db)
    res = await fr.reap_dark_foreground(rt, now=NOW, idle_hours=24, mode="notify")
    assert res["reaped"] == 1 and res["notified"] == 1
    assert (await cc_sessions.get_by_id(db, sid))["status"] == "checkpointed"
    rt._outreach_pipeline.submit_urgent.assert_awaited_once()
    req = rt._outreach_pipeline.submit_urgent.call_args[0][0]
    assert req.target_chat_id == "12345"
    assert req.topic == f"dark_session:{sid}"
    assert await _obs_count(db) == 1


async def test_age_guard_suppresses_notify_for_midflight_turn(db, monkeypatch):
    # Session-level last_activity is stale (selected), but the unanswered user
    # turn itself is RECENT — a long in-flight turn, not a dead one. Must NOT notify.
    sid = await _seed(db)
    _patch_tail(monkeypatch, [_user("still working on this", RECENT)])
    rt = _rt(db)
    res = await fr.reap_dark_foreground(rt, now=NOW, idle_hours=24, mode="notify")
    assert res["reaped"] == 1 and res["notified"] == 0
    rt._outreach_pipeline.submit_urgent.assert_not_awaited()
    assert (await cc_sessions.get_by_id(db, sid))["status"] == "checkpointed"


async def test_suppressed_on_clean_idle(db, monkeypatch):
    await _seed(db)
    _patch_tail(monkeypatch, [_user("hi", OLD), _assistant("hello — here's your answer")])
    rt = _rt(db)
    res = await fr.reap_dark_foreground(rt, now=NOW, idle_hours=24, mode="notify")
    assert res["reaped"] == 1 and res["notified"] == 0
    rt._outreach_pipeline.submit_urgent.assert_not_awaited()
    assert await _obs_count(db) == 0  # a clean reap is silent hygiene


async def test_promise_is_shadow_only(db, monkeypatch):
    await _seed(db)
    _patch_tail(
        monkeypatch,
        [_user("dig into X", OLD), _assistant("Running in the background and will report back.")],
    )
    rt = _rt(db)
    res = await fr.reap_dark_foreground(rt, now=NOW, idle_hours=24, mode="notify")
    assert res["reaped"] == 1 and res["shadow"] == 1 and res["notified"] == 0
    rt._outreach_pipeline.submit_urgent.assert_not_awaited()
    assert await _obs_count(db) == 1  # shadow observation recorded


async def test_suppressed_when_covered_by_dispatch(db, monkeypatch):
    sid = await _seed(db)
    await dsq.enqueue(db, prompt="p", profile="research", origin_session_id=sid)
    _patch_tail(monkeypatch, [_user("do it", OLD)])
    rt = _rt(db)
    res = await fr.reap_dark_foreground(rt, now=NOW, idle_hours=24, mode="notify")
    assert res["reaped"] == 1 and res["notified"] == 0
    rt._outreach_pipeline.submit_urgent.assert_not_awaited()


async def test_non_telegram_origin_graceful(db, monkeypatch):
    sid = await _seed(db, channel="web", chat_id=None)
    _patch_tail(monkeypatch, [_user("do it", OLD)])
    rt = _rt(db)
    res = await fr.reap_dark_foreground(rt, now=NOW, idle_hours=24, mode="notify")
    assert res["reaped"] == 1 and res["notified"] == 0  # unaddressable → no notify, no crash
    assert (await cc_sessions.get_by_id(db, sid))["status"] == "checkpointed"
    assert await _obs_count(db) == 1  # unanswered still observed


async def test_observe_mode_reaps_without_notify(db, monkeypatch):
    await _seed(db)
    _patch_tail(monkeypatch, [_user("do it", OLD)])
    rt = _rt(db)
    res = await fr.reap_dark_foreground(rt, now=NOW, idle_hours=24, mode="observe")
    assert res["reaped"] == 1 and res["notified"] == 0
    rt._outreach_pipeline.submit_urgent.assert_not_awaited()


async def test_off_mode_no_op(db, monkeypatch):
    sid = await _seed(db)
    _patch_tail(monkeypatch, [_user("do it", OLD)])
    res = await fr.reap_dark_foreground(_rt(db), now=NOW, idle_hours=24, mode="off")
    assert res["reaped"] == 0
    assert (await cc_sessions.get_by_id(db, sid))["status"] == "active"


async def test_resume_still_works_after_checkpoint(db, monkeypatch):
    # After a reap, get_active_foreground still returns the row (widened), with
    # cc_session_id intact — so --resume is preserved.
    sid = await _seed(db)
    _patch_tail(monkeypatch, [_user("do it", OLD)])
    await fr.reap_dark_foreground(_rt(db), now=NOW, idle_hours=24, mode="observe")
    row = await cc_sessions.get_active_foreground(db, user_id="tg-999", channel="telegram")
    assert row is not None and row["id"] == sid
    assert row["status"] == "checkpointed"
    assert row["cc_session_id"] == "cc-s1"


async def test_dispatched_dsq_does_not_suppress(db, monkeypatch):
    # A 'dispatched' queue row persists forever after its session completes
    # (no terminal status), so it must NOT permanently suppress the notify.
    sid = await _seed(db)
    qid = await dsq.enqueue(db, prompt="p", profile="research", origin_session_id=sid)
    await db.execute("UPDATE direct_session_queue SET status='dispatched' WHERE id=?", (qid,))
    await db.commit()
    _patch_tail(monkeypatch, [_user("do it", OLD)])
    rt = _rt(db)
    res = await fr.reap_dark_foreground(rt, now=NOW, idle_hours=24, mode="notify")
    assert res["notified"] == 1  # dispatched is not "open" → not suppressed
    rt._outreach_pipeline.submit_urgent.assert_awaited_once()


async def test_pending_dsq_still_suppresses(db, monkeypatch):
    # A genuinely not-yet-run ('pending') queue row WILL deliver → still suppress.
    sid = await _seed(db)
    await dsq.enqueue(db, prompt="p", profile="research", origin_session_id=sid)
    _patch_tail(monkeypatch, [_user("do it", OLD)])
    rt = _rt(db)
    res = await fr.reap_dark_foreground(rt, now=NOW, idle_hours=24, mode="notify")
    assert res["notified"] == 0
    rt._outreach_pipeline.submit_urgent.assert_not_awaited()


async def test_notify_delivery_failure_preserves_alert(db, monkeypatch):
    # submit_urgent raising after checkpoint must NOT lose the alert: no crash,
    # and a HIGH-priority observation still surfaces the dead request.
    sid = await _seed(db)
    _patch_tail(monkeypatch, [_user("finish it", OLD)])
    rt = _rt(db)
    rt._outreach_pipeline.submit_urgent = AsyncMock(side_effect=RuntimeError("tg down"))
    res = await fr.reap_dark_foreground(rt, now=NOW, idle_hours=24, mode="notify")
    assert res["reaped"] == 1 and res["notified"] == 0  # delivery failed, no crash
    assert (await cc_sessions.get_by_id(db, sid))["status"] == "checkpointed"
    cur = await db.execute("SELECT priority FROM observations WHERE type='dark_foreground_session'")
    rows = await cur.fetchall()
    assert len(rows) == 1 and rows[0][0] == "high"  # eligibility-driven, survives failure


# ── Evidence-based reaping (2026-09-04: the 6.5h ghost) ───────────────────
# A fresh heartbeat is ALIVE-proof (skip, both paths); a dead pid on a
# terminal-registered row is DEATH-proof (checkpoint after
# dead_process_minutes, not 24h). Heartbeat ABSENCE proves nothing.

FRESH_HB = "2026-07-22T11:55:00+00:00"  # 5 min before NOW → fresh
IDLE_40M = "2026-07-22T11:20:00+00:00"  # 40 min before NOW


async def _seed_terminal(
    db, *, sid="term-1", last_activity=IDLE_40M, pid=4242, addressable=True
):
    """A terminal-registered row: pid known, ``id == cc_session_id``.

    ``addressable`` sets channel/chat_id. It defaults TRUE because
    ``_notify_origin`` resolves its target from those columns and returns False
    without them — so a fixture lacking them makes every
    ``submit_urgent.await_count == 0`` assertion pass VACUOUSLY, whatever the
    code under test does. That is not hypothetical: it is how three cells in
    this file were green before the negative control caught it.
    """
    await cc_sessions.register_from_filesystem(
        db, id=sid, cc_session_id=sid, started_at=last_activity, status="active",
    )
    await db.execute(
        "UPDATE cc_sessions SET last_activity_at = ? WHERE id = ?",
        (last_activity, sid),
    )
    if addressable:
        await db.execute(
            "UPDATE cc_sessions SET channel = 'telegram', chat_id = '12345', "
            "user_id = 'tg-999' WHERE id = ?",
            (sid,),
        )
    await db.commit()
    await cc_sessions.set_pid(db, sid, pid=pid)


async def _hb(db, cc_sid, updated_at):
    await db.execute(
        "INSERT INTO session_heartbeats (cc_session_id, updated_at) VALUES (?, ?)",
        (cc_sid, updated_at),
    )
    await db.commit()


async def test_fresh_heartbeat_blocks_24h_reap(db, monkeypatch):
    """A 60h-idle row with a 5-minute-old heartbeat is ALIVE — the old
    pure-timestamp path would have checkpointed it."""
    await _seed(db, last_activity=OLD)
    await _hb(db, "cc-s1", FRESH_HB)
    _patch_tail(monkeypatch, [])
    res = await fr.reap_dark_foreground(_rt(db), now=NOW, idle_hours=24, mode="observe")
    row = await cc_sessions.get_by_id(db, "s1")
    assert row["status"] == "active"
    assert res["reaped"] == 0


async def test_dead_pid_fast_path_checkpoints(db, monkeypatch):
    """Acceptance bar — the ghost shape: terminal row, 40 min idle, pid
    provably dead → checkpointed NOW, not 24h later."""
    await _seed_terminal(db)
    _patch_tail(monkeypatch, [])
    monkeypatch.setattr(fr, "_pid_dead", lambda pid, row: True)
    res = await fr.reap_dark_foreground(_rt(db), now=NOW, idle_hours=24, mode="observe")
    row = await cc_sessions.get_by_id(db, "term-1")
    assert row["status"] == "checkpointed"
    assert row["checkpointed_at"] is not None
    assert res["reaped"] == 1


async def test_alive_pid_untouched_by_fast_path(db, monkeypatch):
    await _seed_terminal(db)
    _patch_tail(monkeypatch, [])
    monkeypatch.setattr(fr, "_pid_dead", lambda pid, row: False)
    res = await fr.reap_dark_foreground(_rt(db), now=NOW, idle_hours=24, mode="observe")
    row = await cc_sessions.get_by_id(db, "term-1")
    assert row["status"] == "active"
    assert res["reaped"] == 0


async def test_dead_pid_with_fresh_heartbeat_stays(db, monkeypatch):
    """Alive-proof outranks death evidence: a fresh heartbeat means the
    session IS running (pid evidence may be stale — recycled row pid)."""
    await _seed_terminal(db)
    await _hb(db, "term-1", FRESH_HB)
    _patch_tail(monkeypatch, [])
    monkeypatch.setattr(fr, "_pid_dead", lambda pid, row: True)
    await fr.reap_dark_foreground(_rt(db), now=NOW, idle_hours=24, mode="observe")
    row = await cc_sessions.get_by_id(db, "term-1")
    assert row["status"] == "active"


async def test_dead_pid_fast_path_notifies_recent_unanswered(db, monkeypatch):
    """A PID-proven death cannot be a mid-flight turn: the unanswered-turn
    age guard uses the dead-process cutoff, not the 24h idle cutoff — a user
    prompt 40 minutes before the crash must still alert."""
    # id == cc_session_id marks a terminal row (the fast-path discriminator);
    # channel/chat_id make the origin addressable for the notify.
    sid = await _seed(db, sid="term-tg", cc_sid="term-tg", last_activity=IDLE_40M)
    await cc_sessions.set_pid(db, sid, pid=4242)
    _patch_tail(monkeypatch, [_user("finish this now", IDLE_40M)])
    monkeypatch.setattr(fr, "_pid_dead", lambda pid, row: True)
    rt = _rt(db)
    res = await fr.reap_dark_foreground(rt, now=NOW, idle_hours=24, mode="notify")
    assert res["reaped"] == 1 and res["notified"] == 1
    rt._outreach_pipeline.submit_urgent.assert_awaited_once()


async def test_dead_pid_race_refresh_leaves_row_active(db, monkeypatch):
    """Prompt revival between selection and checkpoint: the row dict carries
    a stale last_activity_at while the live row was refreshed — the guarded
    write must lose and leave the row active."""
    await _seed_terminal(db)
    stale_row = {
        "id": "term-1",
        "cc_session_id": "term-1",
        "pid": 4242,
        "last_activity_at": IDLE_40M,
    }
    monkeypatch.setattr(
        fr.cc_sessions,
        "query_dead_candidate_foreground",
        lambda *a, **k: _ret([stale_row]),
    )
    monkeypatch.setattr(fr, "_pid_dead", lambda pid, row: True)
    # Revival lands between the SELECT and the UPDATE.
    await db.execute(
        "UPDATE cc_sessions SET last_activity_at = ? WHERE id = ?",
        (NOW.isoformat(), "term-1"),
    )
    await db.commit()
    _patch_tail(monkeypatch, [])
    res = await fr.reap_dark_foreground(_rt(db), now=NOW, idle_hours=24, mode="observe")
    row = await cc_sessions.get_by_id(db, "term-1")
    assert row["status"] == "active"
    assert res["reaped"] == 0


async def _ret(rows):
    return rows


async def test_close_dead_lever_disables_fast_path(db, monkeypatch):
    await _seed_terminal(db)
    _patch_tail(monkeypatch, [])
    monkeypatch.setattr(fr, "_pid_dead", lambda pid, row: True)
    base_cfg = fr.load_config()
    monkeypatch.setattr(fr, "load_config", lambda: {**base_cfg, "close_dead": False})
    res = await fr.reap_dark_foreground(_rt(db), now=NOW, idle_hours=24, mode="observe")
    row = await cc_sessions.get_by_id(db, "term-1")
    assert row["status"] == "active"
    assert res["reaped"] == 0


class TestPidDead:
    def _row(self, started="2026-07-22T00:00:00+00:00", last=None):
        return {"started_at": started, "last_activity_at": last or started}

    def test_missing_proc_is_dead(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fr, "_PROC_ROOT", str(tmp_path))
        assert fr._pid_dead(424242, self._row()) is True

    def test_wrong_comm_is_dead(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fr, "_PROC_ROOT", str(tmp_path))
        d = tmp_path / "4242"
        d.mkdir()
        (d / "comm").write_text("python3\n")
        assert fr._pid_dead(4242, self._row()) is True

    def test_recycled_pid_is_dead(self, tmp_path, monkeypatch):
        """Same pid, but the process started AFTER the row's newest
        liveness stamp — a recycle."""
        monkeypatch.setattr(fr, "_PROC_ROOT", str(tmp_path))
        d = tmp_path / "4242"
        d.mkdir()
        (d / "comm").write_text("claude\n")
        monkeypatch.setattr(
            fr, "read_proc_start_iso", lambda pid: "2026-07-22T11:00:00+00:00"
        )
        assert fr._pid_dead(
            4242, self._row(last="2026-07-22T10:00:00+00:00")
        ) is True

    def test_resumed_session_is_alive(self, tmp_path, monkeypatch):
        """The audit's false-death case: --resume reopens the row with a NEW
        pid in the same write that stamps last_activity_at, so the process
        starts after started_at but BEFORE the anchor — alive, not
        recycled. Anchoring to started_at alone read every resumed session
        as dead after 30 idle minutes."""
        monkeypatch.setattr(fr, "_PROC_ROOT", str(tmp_path))
        d = tmp_path / "4242"
        d.mkdir()
        (d / "comm").write_text("claude\n")
        monkeypatch.setattr(
            fr, "read_proc_start_iso", lambda pid: "2026-07-22T11:00:00+00:00"
        )
        assert fr._pid_dead(
            4242,
            self._row(
                started="2026-07-22T00:00:00+00:00",
                last="2026-07-22T11:00:05+00:00",
            ),
        ) is False

    def test_unreadable_proc_entry_is_alive(self, tmp_path, monkeypatch):
        """EACCES-style unreadability is 'cannot see', never death proof
        (audit SF-2): only a MISSING pid is positive evidence."""
        import genesis.cc.foreground_reaper as _fr

        monkeypatch.setattr(_fr, "_PROC_ROOT", str(tmp_path))
        d = tmp_path / "4242"
        d.mkdir()
        comm = d / "comm"
        comm.write_text("claude\n")
        comm.chmod(0o000)
        try:
            assert _fr._pid_dead(4242, self._row()) is False
        finally:
            comm.chmod(0o644)

    def test_unreadable_start_is_alive(self, tmp_path, monkeypatch):
        """Fail-open: unknown start time never flags."""
        monkeypatch.setattr(fr, "_PROC_ROOT", str(tmp_path))
        d = tmp_path / "4242"
        d.mkdir()
        (d / "comm").write_text("claude\n")
        monkeypatch.setattr(fr, "read_proc_start_iso", lambda pid: None)
        assert fr._pid_dead(4242, self._row()) is False

    def test_live_matching_claude_is_alive(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fr, "_PROC_ROOT", str(tmp_path))
        d = tmp_path / "4242"
        d.mkdir()
        (d / "comm").write_text("claude\n")
        monkeypatch.setattr(
            fr, "read_proc_start_iso", lambda pid: "2026-07-21T23:00:00+00:00"
        )
        assert fr._pid_dead(4242, self._row()) is False
# --- round-5 fixes: the pass-abort shape, dead_only, and the heartbeat race ----
#
# Every cell below pins a defect that survived four review rounds because
# nothing exercised it. `dead_only=True` in particular had ZERO coverage: it is
# the ONLY way the scheduled `session_reaper_dead_pid` job calls this function,
# so the production call signature was untested end to end.


async def test_liveness_check_raise_does_not_abort_the_pass(db, monkeypatch):
    """A raise from the liveness check must not silence the whole reaper.

    `_pid_dead` is called while BUILDING the candidate list, outside the
    per-row try, so before the fix a raise there propagated out of
    `reap_dark_foreground` entirely — losing not just the fast-path row but
    the already-fetched 24h-idle rows, the alive-proof read and every
    checkpoint.

    NOT an unreadable /proc entry: `_pid_dead` catches (OSError, UnicodeError)
    itself and fails open, so that case never reached here. The reachable
    shapes are malformed-row ones — a missing `pid` key, a non-string
    timestamp in the recycle compare — so this raises a generic exception to
    pin the CALL SITE's isolation rather than any one cause.

    The 24h row below is the evidence: it is unrelated to the raising row and
    must still be reaped."""
    await _seed(db, last_activity=OLD)  # ordinary 24h-idle row
    await _seed_terminal(db)  # fast-path candidate whose check will raise
    _patch_tail(monkeypatch, [])

    def _boom(pid, row):
        raise RuntimeError("liveness check blew up")

    monkeypatch.setattr(fr, "_pid_dead", _boom)
    res = await fr.reap_dark_foreground(_rt(db), now=NOW, idle_hours=24, mode="observe")

    assert res["reaped"] == 1, "the unrelated 24h row must still be reaped"
    assert (await cc_sessions.get_by_id(db, "s1"))["status"] == "checkpointed"
    # The raising candidate is treated as ALIVE — absence of evidence is not
    # death evidence.
    assert (await cc_sessions.get_by_id(db, "term-1"))["status"] == "active"


async def test_dead_only_skips_the_24h_scan(db, monkeypatch):
    """`dead_only=True` is the scheduled job's ONLY call shape, and it must not
    touch timestamp-only rows — that is what makes the 30-minute cadence cheap
    enough to run 48x a day."""
    await _seed(db, last_activity=OLD)  # 24h-idle, NOT pid-dead
    _patch_tail(monkeypatch, [])
    monkeypatch.setattr(fr, "_pid_dead", lambda pid, row: True)
    res = await fr.reap_dark_foreground(
        _rt(db), now=NOW, idle_hours=24, mode="observe", dead_only=True
    )
    assert res["reaped"] == 0
    assert (await cc_sessions.get_by_id(db, "s1"))["status"] == "active"


async def test_dead_only_still_reaps_a_dead_pid(db, monkeypatch):
    """The positive control for the cell above: `dead_only` must not be inert."""
    await _seed_terminal(db)
    _patch_tail(monkeypatch, [])
    monkeypatch.setattr(fr, "_pid_dead", lambda pid, row: True)
    res = await fr.reap_dark_foreground(
        _rt(db), now=NOW, idle_hours=24, mode="observe", dead_only=True
    )
    assert res["reaped"] == 1
    assert (await cc_sessions.get_by_id(db, "term-1"))["status"] == "checkpointed"


async def test_heartbeat_arriving_after_the_snapshot_blocks_the_write(db, monkeypatch):
    """The TOCTOU both reviewers reported, from the write side.

    The pass-level alive-proof filter is a SNAPSHOT. This commits a fresh
    heartbeat AFTER that snapshot and before the row's checkpoint write —
    exactly the interleaving the filter cannot see, and the one that produces
    a Telegram alert saying "nothing is still running on it" about a live
    session. The `NOT EXISTS` inside `checkpoint_dark` is what catches it."""
    await _seed_terminal(db)
    _patch_tail(monkeypatch, [])
    monkeypatch.setattr(fr, "_pid_dead", lambda pid, row: True)

    real_ids = fr._fresh_heartbeat_ids

    async def _snapshot_then_beat(db_, *, cutoff):
        got = await real_ids(db_, cutoff=cutoff)
        # The heartbeat lands here: after the read, before any write.
        await _hb(db_, "term-1", FRESH_HB)
        return got

    monkeypatch.setattr(fr, "_fresh_heartbeat_ids", _snapshot_then_beat)
    rt = _rt(db)  # ONE runtime: _rt() builds a fresh mock per call, so
    # asserting on a second _rt(db) would inspect a mock nothing ever used.
    res = await fr.reap_dark_foreground(rt, now=NOW, idle_hours=24, mode="notify")

    assert (await cc_sessions.get_by_id(db, "term-1"))["status"] == "active"
    assert res["reaped"] == 0
    # And no alert was sent about a session that is running.
    assert rt._outreach_pipeline.submit_urgent.await_count == 0


async def test_stale_heartbeat_does_not_block_the_write(db, monkeypatch):
    """Negative control for the cell above — without it, a `NOT EXISTS` that
    matched ANY heartbeat row would pass the positive test while breaking
    reaping entirely."""
    await _seed_terminal(db)
    await _hb(db, "term-1", OLD)  # 60h old — well outside the freshness window
    _patch_tail(monkeypatch, [])
    monkeypatch.setattr(fr, "_pid_dead", lambda pid, row: True)
    res = await fr.reap_dark_foreground(_rt(db), now=NOW, idle_hours=24, mode="observe")
    assert res["reaped"] == 1
    assert (await cc_sessions.get_by_id(db, "term-1"))["status"] == "checkpointed"


async def test_one_cutoff_feeds_both_the_filter_and_the_write_guard(db, monkeypatch):
    """The filter and the write guard must not each derive their own cutoff:
    two values computed at different moments disagree by exactly the window the
    guard closes."""
    seen: list[str] = []
    real_ids = fr._fresh_heartbeat_ids

    async def _record(db_, *, cutoff):
        seen.append(cutoff)
        return await real_ids(db_, cutoff=cutoff)

    monkeypatch.setattr(fr, "_fresh_heartbeat_ids", _record)
    real_ckpt = cc_sessions.checkpoint_dark

    async def _record_ckpt(db_, id_, **kw):
        seen.append(kw.get("heartbeat_fresh_after"))
        return await real_ckpt(db_, id_, **kw)

    monkeypatch.setattr(cc_sessions, "checkpoint_dark", _record_ckpt)
    await _seed_terminal(db)
    _patch_tail(monkeypatch, [])
    monkeypatch.setattr(fr, "_pid_dead", lambda pid, row: True)
    await fr.reap_dark_foreground(_rt(db), now=NOW, idle_hours=24, mode="observe")

    assert len(seen) == 2, f"expected filter + write guard, got {seen}"
    assert seen[0] is not None and seen[1] is not None
    assert seen[0] == seen[1]
    assert seen[0] == fr._heartbeat_cutoff(NOW)
# --- round 6: the notify window, after the checkpoint has already committed ---
#
# `checkpoint_dark` winning proves the row was dark AT THAT STATEMENT. Between
# it and the send sit a transcript read, two awaited queries and an awaited
# outreach submission — so the guard that closes the checkpoint race does not
# reach this one. These cells pin the LAST check before the message leaves.


async def test_revival_after_checkpoint_suppresses_the_alert(db, monkeypatch):
    """A prompt arriving after the checkpoint commits must not still be told
    its work was interrupted."""
    await _seed_terminal(db)
    _patch_tail(monkeypatch, [_user("do the thing", OLD)])  # notify-eligible
    monkeypatch.setattr(fr, "_pid_dead", lambda pid, row: True)

    real_ckpt = cc_sessions.checkpoint_dark

    async def _ckpt_then_revive(db_, id_, **kw):
        won = await real_ckpt(db_, id_, **kw)
        # The prompt lands HERE: after the checkpoint, before the notify.
        await _hb(db_, "term-1", FRESH_HB)
        await db_.execute("UPDATE cc_sessions SET status = 'active' WHERE id = ?", (id_,))
        await db_.commit()
        return won

    monkeypatch.setattr(cc_sessions, "checkpoint_dark", _ckpt_then_revive)
    rt = _rt(db)
    res = await fr.reap_dark_foreground(rt, now=NOW, idle_hours=24, mode="notify")

    assert rt._outreach_pipeline.submit_urgent.await_count == 0, (
        "a revived session was told nothing was running on it"
    )
    assert res["notified"] == 0
    assert res.get("revived") == 1


async def test_a_genuinely_dark_row_is_still_notified(db, monkeypatch):
    """Negative control. Without it, a re-check that returned True
    unconditionally would pass the cell above while disabling notification
    entirely — which is the failure this subsystem exists to prevent."""
    await _seed_terminal(db)
    _patch_tail(monkeypatch, [_user("do the thing", OLD)])
    monkeypatch.setattr(fr, "_pid_dead", lambda pid, row: True)
    rt = _rt(db)
    res = await fr.reap_dark_foreground(rt, now=NOW, idle_hours=24, mode="notify")

    assert rt._outreach_pipeline.submit_urgent.await_count == 1
    assert res["notified"] == 1
    assert res.get("revived", 0) == 0


async def test_revival_recheck_fails_toward_sending():
    """The asymmetry is deliberate and runs the OPPOSITE way to the checkpoint
    guard: a MISSED interruption alert is the failure this subsystem exists to
    prevent, so an unreadable re-check must not swallow the alert.

    Driven against the helper directly with a raising stand-in rather than by
    monkeypatching the real connection: `db` is a SerializedConnection, and
    wrapping its `execute` while re-entering it from inside the wrapper
    deadlocks the serializer — an earlier version of this cell hung the whole
    suite rather than failing."""

    class _RaisingDb:
        async def execute(self, *a, **k):
            raise RuntimeError("re-check query exploded")

    revived = await fr._revived_since_checkpoint(
        _RaisingDb(), {"id": "term-1"}, heartbeat_cutoff=FRESH_HB
    )
    assert revived is False, "an unreadable re-check must not suppress the alert"


async def test_revival_recheck_with_no_cutoff_fails_toward_sending():
    """Same direction for the other degraded input: no cutoff to compare
    against means no evidence of revival, which must not become evidence OF
    it."""

    class _Row(dict):
        pass

    class _Db:
        async def execute(self, sql, params=None):
            class _Cur:
                async def fetchone(_self):
                    return {"status": "checkpointed", "cc_session_id": "cc-1"}

            return _Cur()

    revived = await fr._revived_since_checkpoint(
        _Db(), {"id": "term-1"}, heartbeat_cutoff=None
    )
    assert revived is False


async def test_heartbeat_only_revival_is_caught(db, monkeypatch):
    """`session_observer_hook` writes session_heartbeats ONLY — it never
    touches cc_sessions — so a session busy inside one long tool call revives
    without its status changing. Status alone cannot see that."""
    await _seed_terminal(db)
    _patch_tail(monkeypatch, [_user("do the thing", OLD)])
    monkeypatch.setattr(fr, "_pid_dead", lambda pid, row: True)

    real_ckpt = cc_sessions.checkpoint_dark

    async def _ckpt_then_beat_only(db_, id_, **kw):
        won = await real_ckpt(db_, id_, **kw)
        await _hb(db_, "term-1", FRESH_HB)  # heartbeat ONLY; status untouched
        return won

    monkeypatch.setattr(cc_sessions, "checkpoint_dark", _ckpt_then_beat_only)
    rt = _rt(db)
    res = await fr.reap_dark_foreground(rt, now=NOW, idle_hours=24, mode="notify")

    row = await cc_sessions.get_by_id(db, "term-1")
    assert row["status"] == "checkpointed", "status really did stay unchanged"
    assert rt._outreach_pipeline.submit_urgent.await_count == 0
    assert res.get("revived") == 1
