"""The hourly page for background sessions that stopped (stopped-session paging, PR 2).

A stop is a background ``cc_sessions`` row whose recorded outcome is failed,
partial, blocked or expired. It reaches the owner exactly once: immediately for
owner-requested work, otherwise in one batched page an hour. ``outcome_paged_at``
is what makes "exactly once" hold across restarts and both send paths.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.cc.direct_session import DirectSessionRequest, DirectSessionRunner, pages_immediately
from genesis.db.crud import cc_sessions
from genesis.outreach import stopped_sessions as ss
from genesis.outreach.types import OutreachResult, OutreachStatus

NOW = datetime(2026, 11, 4, 12, 5, tzinfo=UTC)


def _sent(status: OutreachStatus) -> OutreachResult:
    return OutreachResult(outreach_id="o", status=status, channel="telegram", message_content="")


async def _row(
    db,
    sid: str,
    *,
    session_type="background_task",
    source_tag="campaign",
    status="failed",
    outcome="failed",
    reason="boom",
    ago=timedelta(minutes=10),
    paged=None,
) -> None:
    await cc_sessions.create(
        db,
        id=sid,
        session_type=session_type,
        model="sonnet",
        effort="medium",
        status="active",
        user_id="u1",
        channel="bridge",
        source_tag=source_tag,
        started_at="2026-11-04T00:00:00",
        last_activity_at="2026-11-04T00:00:00",
    )
    await cc_sessions.update_status(
        db,
        sid,
        status=status,
        outcome=outcome,
        outcome_reason=reason,
        ts=(NOW - ago).isoformat(),
    )
    if paged:
        await db.execute("UPDATE cc_sessions SET outcome_paged_at = ? WHERE id = ?", (paged, sid))
        await db.commit()


# ── which rows are stops ───────────────────────────────────────────────────


async def test_only_unpaged_recent_background_stops_are_listed(db):
    await _row(db, "stop-failed")
    await _row(db, "stop-partial", status="completed", outcome="partial")
    await _row(db, "stop-expired", status="expired", outcome="expired")
    await _row(db, "ok", status="completed", outcome="finished")
    await _row(db, "unreported", status="completed", outcome="unreported")
    await _row(db, "parked", outcome="parked")
    await _row(db, "fg", session_type="foreground", source_tag="foreground")
    await _row(db, "paged", paged="earlier")
    await _row(db, "old", ago=timedelta(days=8))
    await _row(db, "just-now", ago=timedelta(minutes=1))  # left to its immediate notifier
    rows, total = await cc_sessions.list_unpaged_stops(
        db, since=(NOW - ss.LOOKBACK).isoformat(), until=(NOW - ss.GRACE).isoformat(), limit=10
    )
    assert sorted(r["id"] for r in rows) == ["stop-expired", "stop-failed", "stop-partial"]
    assert total == 3


async def test_the_count_covers_what_the_limit_left_out(db):
    for i in range(4):
        await _row(db, f"s{i}", ago=timedelta(minutes=10 + i))
    rows, total = await cc_sessions.list_unpaged_stops(
        db, since="2026-11-01", until=NOW.isoformat(), limit=2
    )
    assert (len(rows), total) == (2, 4)
    assert [r["id"] for r in rows] == ["s3", "s2"]  # oldest first


async def test_marking_paged_never_restamps(db):
    await _row(db, "a")
    assert await cc_sessions.mark_outcome_paged(db, ["a"], ts="first") == 1
    assert await cc_sessions.mark_outcome_paged(db, ["a"], ts="second") == 0
    assert (await cc_sessions.get_by_id(db, "a"))["outcome_paged_at"] == "first"
    assert await cc_sessions.mark_outcome_paged(db, [], ts="x") == 0


# ── the page ───────────────────────────────────────────────────────────────


def test_page_names_each_stop_and_counts_the_rest():
    rows = [
        {"id": "abcdef0123", "source_tag": "campaign", "outcome": "failed", "outcome_reason": "x"},
        {
            "id": "0123456789",
            "source_tag": "inbox_evaluation",
            "outcome": "expired",
            "outcome_reason": None,
        },
    ]
    text, listed = ss.format_page(rows, total=5)
    assert listed == ["abcdef0123", "0123456789"]
    assert text.splitlines()[0] == "5 background sessions stopped"
    assert "• campaign: failed (abcdef01) - x" in text
    assert "• inbox_evaluation: expired (01234567)" in text
    assert "3 more" in text
    assert ss.format_page([], total=0) is None


def test_a_long_reason_is_previewed_and_says_where_the_rest_is():
    row = {"id": "a" * 10, "source_tag": "t", "outcome": "failed", "outcome_reason": "y" * 500}
    text, _ = ss.format_page([row], total=1)
    assert "full reason on the session row" in text
    assert "y" * 500 not in text


def test_a_full_page_stays_under_the_channel_limit_and_lists_only_what_fits():
    """The channel formatter cuts at its platform limit; a row cut off there
    would be stamped without being shown (review BLOCKER). Only listed ids are
    returned, and the rest are counted for the next hour."""
    rows = [
        {
            "id": f"{i:032d}",
            "source_tag": f"user_job:{'x' * 36}",
            "outcome": "failed",
            "outcome_reason": "z" * 300,
        }
        for i in range(25)
    ]
    text, listed = ss.format_page(rows, total=25)
    assert len(text) <= 4096
    assert 0 < len(listed) < 25
    assert all(r[:8] in text for r in listed)
    assert f"{25 - len(listed)} more" in text


def test_reasons_are_html_escaped():
    row = {"id": "a" * 10, "source_tag": "t", "outcome": "failed", "outcome_reason": "<b>&"}
    text, _ = ss.format_page([row], total=1)
    assert "&lt;b&gt;&amp;" in text and "<b>" not in text


# ── one run of the hourly job ──────────────────────────────────────────────


async def test_only_rendered_rows_are_stamped(db, monkeypatch):
    for i in range(3):
        await _row(db, f"r{i}", ago=timedelta(minutes=30 + i))
    monkeypatch.setattr(ss, "PAGE_BUDGET", 60)  # room for exactly one line
    pipeline = AsyncMock()
    pipeline.submit_raw.return_value = _sent(OutreachStatus.DELIVERED)
    assert await ss.run_page(db, pipeline, now=NOW) == 1
    stamped = [
        r for r in ("r0", "r1", "r2") if (await cc_sessions.get_by_id(db, r))["outcome_paged_at"]
    ]
    assert stamped == ["r2"]  # the oldest, the one actually listed
    request = pipeline.submit_raw.await_args.args[1]
    assert request.defer_retry is False


async def test_a_delivered_page_stamps_exactly_the_listed_stops(db):
    await _row(db, "a")
    await _row(db, "b", status="completed", outcome="partial")
    pipeline = AsyncMock()
    pipeline.submit_raw.return_value = _sent(OutreachStatus.DELIVERED)
    assert await ss.run_page(db, pipeline, now=NOW) == 2
    assert pipeline.submit_raw.await_count == 1
    assert (await cc_sessions.get_by_id(db, "a"))["outcome_paged_at"] == NOW.isoformat()
    # The next hour has nothing new to say.
    assert await ss.run_page(db, pipeline, now=NOW + timedelta(hours=1)) == 0
    assert pipeline.submit_raw.await_count == 1


@pytest.mark.parametrize(
    "status", [OutreachStatus.FAILED, OutreachStatus.REJECTED, OutreachStatus.HELD]
)
async def test_an_undelivered_page_stamps_nothing_so_it_retries(db, status):
    await _row(db, "a")
    pipeline = AsyncMock()
    pipeline.submit_raw.return_value = _sent(status)
    assert await ss.run_page(db, pipeline, now=NOW) == 0
    assert (await cc_sessions.get_by_id(db, "a"))["outcome_paged_at"] is None


async def test_nothing_to_report_sends_nothing(db):
    await _row(db, "ok", status="completed", outcome="finished")
    pipeline = AsyncMock()
    assert await ss.run_page(db, pipeline, now=NOW) == 0
    pipeline.submit_raw.assert_not_awaited()


async def test_a_page_counts_the_hours_unreported_sessions(db):
    await _row(db, "a")
    for i, ago in enumerate((10, 30, 50)):
        await _row(
            db, f"u{i}", status="completed", outcome="unreported", ago=timedelta(minutes=ago)
        )
    await _row(db, "u-old", status="completed", outcome="unreported", ago=timedelta(hours=2))
    await _row(db, "u-fg", session_type="foreground", status="completed", outcome="unreported")
    await _row(db, "u-now", status="completed", outcome="unreported", ago=timedelta(minutes=1))
    pipeline = AsyncMock()
    pipeline.submit_raw.return_value = _sent(OutreachStatus.DELIVERED)
    assert await ss.run_page(db, pipeline, now=NOW) == 1
    text = pipeline.submit_raw.await_args.args[0]
    assert text.endswith(
        "3 other background sessions in the last hour ended without reporting a status."
    )


async def test_unreported_sessions_alone_send_nothing(db):
    await _row(db, "u", status="completed", outcome="unreported")
    pipeline = AsyncMock()
    assert await ss.run_page(db, pipeline, now=NOW) == 0
    pipeline.submit_raw.assert_not_awaited()


def test_the_count_line_is_singular_for_one_and_absent_for_none():
    stops = [{"id": "abcdefgh1", "source_tag": "campaign", "outcome": "failed"}]
    assert "1 other background session in" in ss.format_page(stops, total=1, unreported=1)[0]
    assert "without reporting" not in ss.format_page(stops, total=1)[0]


async def test_the_kill_switch_sends_and_stamps_nothing(db, monkeypatch):
    await _row(db, "a")
    monkeypatch.setenv("GENESIS_STOPPED_SESSION_PAGES", "off")
    pipeline = AsyncMock()
    assert await ss.run_page(db, pipeline, now=NOW) == 0
    pipeline.submit_raw.assert_not_awaited()
    assert (await cc_sessions.get_by_id(db, "a"))["outcome_paged_at"] is None


def test_governance_never_dedups_the_hourly_page():
    """The row stamp is the dedup; the 24h default would drop every page but
    the first of the day."""
    from genesis.outreach.governance import _DEDUP_WINDOWS

    assert _DEDUP_WINDOWS["stopped_background_sessions"] == 0


async def test_the_job_is_an_hourly_cron():
    from genesis.outreach.config import OutreachConfig, QuietHours
    from genesis.outreach.scheduler import OutreachScheduler

    cfg = OutreachConfig(
        quiet_hours=QuietHours(start="22:00", end="07:00"),
        channel_preferences={},
        thresholds={},
        max_daily=5,
        surplus_daily=1,
        content_daily=3,
        notification_daily=10,
        morning_report_time="07:00",
        engagement_timeout_hours=24,
        engagement_poll_minutes=60,
    )
    sched = OutreachScheduler(AsyncMock(), AsyncMock(), AsyncMock(), cfg, MagicMock())
    sched.start()
    try:
        job = sched._scheduler.get_job("outreach_stopped_sessions")
        assert job is not None
        assert str(job.trigger.fields[job.trigger.FIELD_NAMES.index("minute")]) == "5"
        assert job.coalesce is True
    finally:
        sched._scheduler.shutdown(wait=False)


# ── the immediate class ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kwargs", "immediate"),
    [
        ({"caller_context": "mcp_tool"}, True),
        ({"source_tag": "ego_dispatch", "caller_context": "ego_proposal:p1"}, True),
        ({"source_tag": "user_job:j1", "caller_context": "user_job:j1"}, True),
        (
            {"caller_context": "rate_limit_resume:k", "origin_caller_context": "ego_proposal:p"},
            True,
        ),
        (
            {"source_tag": "models_md_synthesis", "caller_context": "schedule:models_md_synthesis"},
            False,
        ),
        ({"source_tag": "campaign", "caller_context": "campaign:c"}, False),
        ({}, False),
    ],
)
def test_who_pages_immediately(kwargs, immediate):
    assert pages_immediately(DirectSessionRequest(prompt="x", **kwargs)) is immediate


@pytest.mark.parametrize(
    ("status", "stamped"), [(OutreachStatus.DELIVERED, True), (OutreachStatus.REJECTED, False)]
)
async def test_an_immediate_send_stamps_only_when_delivered(db, status, stamped):
    from genesis.cc.session_manager import SessionManager

    await _row(db, "a")
    runner = DirectSessionRunner(
        invoker=AsyncMock(),
        session_manager=SessionManager(db=db, day_boundary_hour=0),
        config_builder=AsyncMock(),
        runtime=MagicMock(),
    )
    await runner._mark_paged("a", _sent(status))
    await runner._mark_paged("a", None)  # no pipeline: never stamps
    assert ((await cc_sessions.get_by_id(db, "a"))["outcome_paged_at"] is not None) is stamped


async def test_each_owner_requested_failure_gets_its_own_dedup_identity():
    """Governance dedups (signal, topic, category) for 24h; a fixed topic let only
    the first immediate failure of the day page at once (owner ruling: immediate)."""
    from genesis.cc.direct_session import DirectSessionResult

    pipeline = AsyncMock()
    runtime = MagicMock()
    runtime._outreach_pipeline = pipeline
    runner = DirectSessionRunner(
        invoker=AsyncMock(),
        session_manager=MagicMock(),
        config_builder=AsyncMock(),
        runtime=runtime,
    )
    req = DirectSessionRequest(prompt="x", caller_context="mcp_tool")
    for sid in ("s-one", "s-two"):
        await runner._notify(req, DirectSessionResult(session_id=sid, error="e"), success=False)
    topics = [c.args[0].topic for c in pipeline.submit.await_args_list]
    assert len(set(topics)) == 2
    assert all(c.args[0].defer_retry is False for c in pipeline.submit.await_args_list)
