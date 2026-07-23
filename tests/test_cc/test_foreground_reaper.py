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
