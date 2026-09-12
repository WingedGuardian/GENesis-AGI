"""Tests for the real OutreachEngagementCollector."""

from datetime import UTC, datetime

import aiosqlite
import pytest

from genesis.db.crud import outreach as outreach_crud
from genesis.db.schema import create_all_tables
from genesis.learning.signals.outreach_engagement import OutreachEngagementCollector


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    # WS-2 P1b: the engagement_outcome CHECK now ENFORCES the canonical
    # vocabulary (acted_on/acknowledged/engaged are legal members) — this
    # fixture runs with enforcement ON so a test writing outside the
    # vocabulary fails here, exactly like prod would.
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_no_outreach_returns_zero(db):
    collector = OutreachEngagementCollector(db)
    reading = await collector.collect()
    assert reading.name == "outreach_engagement_data"
    assert reading.value == 0.0


@pytest.mark.asyncio
async def test_all_engaged_returns_high(db):
    now = datetime.now(UTC).isoformat()
    for i in range(3):
        await outreach_crud.create(
            db, id=f"e-{i}", signal_type="surplus", topic=f"T{i}",
            category="content", salience_score=0.8, channel="discord",
            message_content="Hi", created_at=now,
        )
        await outreach_crud.record_delivery(db, f"e-{i}", delivered_at=now)
        # 'useful' is the value a real reply writes — was 'engaged', a value
        # never written, so this ratio used to be 0.0 no matter what.
        await outreach_crud.record_engagement(db, f"e-{i}", engagement_outcome="useful", engagement_signal="user_reply")

    collector = OutreachEngagementCollector(db)
    reading = await collector.collect()
    assert reading.value == 1.0


@pytest.mark.asyncio
async def test_positive_set_includes_behavioural(db):
    """useful / engaged / acted_on / acknowledged all count as engaged
    (the canonical POSITIVE_ENGAGEMENT_OUTCOMES set)."""
    now = datetime.now(UTC).isoformat()
    for i, outcome in enumerate(["useful", "engaged", "acted_on", "acknowledged"]):
        await outreach_crud.create(
            db, id=f"p-{i}", signal_type="surplus", topic=f"P{i}",
            category="content", salience_score=0.8, channel="discord",
            message_content="Hi", created_at=now,
        )
        await outreach_crud.record_delivery(db, f"p-{i}", delivered_at=now)
        await outreach_crud.record_engagement(db, f"p-{i}", engagement_outcome=outcome, engagement_signal="s")

    reading = await OutreachEngagementCollector(db).collect()
    assert reading.value == 1.0


@pytest.mark.asyncio
async def test_mixed_engagement(db):
    now = datetime.now(UTC).isoformat()
    for i in range(2):
        await outreach_crud.create(
            db, id=f"eng-{i}", signal_type="surplus", topic=f"E{i}",
            category="content", salience_score=0.8, channel="discord",
            message_content="Hi", created_at=now,
        )
        await outreach_crud.record_delivery(db, f"eng-{i}", delivered_at=now)
        await outreach_crud.record_engagement(db, f"eng-{i}", engagement_outcome="useful", engagement_signal="user_reply")
    for i in range(2):
        await outreach_crud.create(
            db, id=f"ign-{i}", signal_type="surplus", topic=f"I{i}",
            category="content", salience_score=0.8, channel="discord",
            message_content="Hi", created_at=now,
        )
        await outreach_crud.record_delivery(db, f"ign-{i}", delivered_at=now)
        await outreach_crud.record_engagement(db, f"ign-{i}", engagement_outcome="ignored", engagement_signal="timeout")

    collector = OutreachEngagementCollector(db)
    reading = await collector.collect()
    assert reading.value == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_owner_channel_excluded_from_denominator(db):
    """Owner-facing traffic (delivered over the Telegram channel — approvals,
    digests, etc.) must NOT pollute the engagement denominator. One external
    Discord reply + three owner-facing Telegram approvals that went unanswered
    should read 1/1 = 1.0, not 1/4 = 0.25."""
    now = datetime.now(UTC).isoformat()
    await outreach_crud.create(
        db, id="c-0", signal_type="content", topic="post",
        category="content", salience_score=0.8, channel="discord",
        message_content="Shipped X", created_at=now,
    )
    await outreach_crud.record_delivery(db, "c-0", delivered_at=now)
    await outreach_crud.record_engagement(
        db, "c-0", engagement_outcome="useful", engagement_signal="user_reply",
    )
    for i in range(3):
        await outreach_crud.create(
            db, id=f"appr-{i}", signal_type="approval", topic=f"A{i}",
            category="approval", salience_score=0.8, channel="telegram",
            message_content="Approve?", created_at=now,
        )
        await outreach_crud.record_delivery(db, f"appr-{i}", delivered_at=now)
        await outreach_crud.record_engagement(
            db, f"appr-{i}", engagement_outcome="ignored", engagement_signal="timeout",
        )

    reading = await OutreachEngagementCollector(db).collect()
    assert reading.value == 1.0


@pytest.mark.asyncio
async def test_get_engagement_stats_excludes_owner_channels(db):
    """get_engagement_stats feeds the morning report AND governance's
    engagement_throttle (ignore_rate = ignored/total). Owner-facing channels
    (telegram approvals, voice notifications) must be excluded or the owner
    ignoring their own pings would inflate the ignore-rate and wrongly throttle
    genuine external outreach. One external discord reply + three ignored
    telegram approvals + one ignored voice ping → total=1, engaged=1, ignored=0."""
    now = datetime.now(UTC).isoformat()
    await outreach_crud.create(
        db, id="ext-0", signal_type="content", topic="post",
        category="content", salience_score=0.8, channel="discord",
        message_content="Shipped X", created_at=now,
    )
    await outreach_crud.record_delivery(db, "ext-0", delivered_at=now)
    await outreach_crud.record_engagement(
        db, "ext-0", engagement_outcome="useful", engagement_signal="user_reply",
    )
    for i in range(3):
        await outreach_crud.create(
            db, id=f"tg-{i}", signal_type="approval", topic=f"A{i}",
            category="approval", salience_score=0.8, channel="telegram",
            message_content="Approve?", created_at=now,
        )
        await outreach_crud.record_delivery(db, f"tg-{i}", delivered_at=now)
        await outreach_crud.record_engagement(
            db, f"tg-{i}", engagement_outcome="ignored", engagement_signal="timeout",
        )
    await outreach_crud.create(
        db, id="vc-0", signal_type="notification", topic="chime",
        category="notification", salience_score=0.8, channel="voice",
        message_content="Heads up", created_at=now,
    )
    await outreach_crud.record_delivery(db, "vc-0", delivered_at=now)
    await outreach_crud.record_engagement(
        db, "vc-0", engagement_outcome="ignored", engagement_signal="timeout",
    )

    stats = await outreach_crud.get_engagement_stats(db, days=7)
    assert stats["total"] == 1
    assert stats["engaged"] == 1
    assert stats["ignored"] == 0


@pytest.mark.asyncio
async def test_email_notification_counts_as_external(db):
    """An email-channel 'notification' is a genuine PROSPECT reply, not owner
    housekeeping (MAIL_REPLY.md). Category alone would wrongly exclude it as
    relay; channel-based classification correctly COUNTS it. One engaged email
    reply + one owner-facing Telegram approval → 1/1 = 1.0."""
    now = datetime.now(UTC).isoformat()
    await outreach_crud.create(
        db, id="mail-0", signal_type="notification", topic="re: pitch",
        category="notification", salience_score=0.8, channel="email",
        message_content="Thanks — interested", created_at=now,
    )
    await outreach_crud.record_delivery(db, "mail-0", delivered_at=now)
    await outreach_crud.record_engagement(
        db, "mail-0", engagement_outcome="useful", engagement_signal="user_reply",
    )
    # Owner-facing Telegram approval that got no reaction — must not dilute.
    await outreach_crud.create(
        db, id="appr-x", signal_type="approval", topic="A",
        category="approval", salience_score=0.8, channel="telegram",
        message_content="Approve?", created_at=now,
    )
    await outreach_crud.record_delivery(db, "appr-x", delivered_at=now)
    await outreach_crud.record_engagement(
        db, "appr-x", engagement_outcome="ignored", engagement_signal="timeout",
    )

    reading = await OutreachEngagementCollector(db).collect()
    assert reading.value == 1.0


# ── Unified ratio: ego proposals count as outbound (PR decision-propagation)


async def _proposal_with_journal(db, pid, *, now, resolved_reason=None):
    """Seed a proposal + its journal row; optionally resolve with a typed
    reason through the SAME shared hook production uses."""
    from genesis.db.crud import ego as ego_crud
    from genesis.db.crud import intervention_journal as journal_crud
    from genesis.ego.resolution import handle_proposal_resolution

    await ego_crud.create_proposal(
        db, id=pid, action_type="t", content=pid, created_at=now,
    )
    await journal_crud.create(
        db, ego_source="user_ego_cycle", proposal_id=pid, cycle_id="c1",
        action_type="t", action_summary=pid, created_at=now,
    )
    if resolved_reason is not None:
        await ego_crud.resolve_proposal(
            db, pid, status="rejected", user_response=resolved_reason,
        )
        prop = await ego_crud.get_proposal(db, pid)
        await handle_proposal_resolution(
            db, prop, "rejected", reason=resolved_reason or None, source="test",
        )


@pytest.mark.asyncio
async def test_proposals_with_typed_response_count_as_engaged(db):
    """A typed reason on a proposal resolution is engagement; an unresolved
    proposal counts toward total. 1 engaged / 2 outbound = 0.5."""
    now = datetime.now(UTC).isoformat()
    await _proposal_with_journal(db, "pr-1", now=now, resolved_reason="typed deny reason")
    await _proposal_with_journal(db, "pr-2", now=now)

    reading = await OutreachEngagementCollector(db).collect()
    assert reading.value == 0.5
    assert "ego_proposals" in reading.source


@pytest.mark.asyncio
async def test_resolution_without_response_not_engaged(db):
    """A bare resolution (no words) is outbound but not typed engagement."""
    now = datetime.now(UTC).isoformat()
    await _proposal_with_journal(db, "pr-3", now=now, resolved_reason="")

    reading = await OutreachEngagementCollector(db).collect()
    assert reading.value == 0.0


@pytest.mark.asyncio
async def test_dispatch_bookkeeping_never_counts_as_engagement(db):
    """ego_proposals.user_response is overwritten by dispatch bookkeeping
    ('dispatching', session ids). Engagement reads the write-once journal
    record, so those system writes must not inflate the ratio."""
    from genesis.db.crud import ego as ego_crud

    now = datetime.now(UTC).isoformat()
    await _proposal_with_journal(db, "pr-d", now=now)
    # bare approve, then the dispatch path stamps user_response
    await ego_crud.resolve_proposal(db, "pr-d", status="approved", user_response="")
    await db.execute(
        "UPDATE ego_proposals SET user_response = 'dispatching' WHERE id = 'pr-d'"
    )
    await db.commit()

    reading = await OutreachEngagementCollector(db).collect()
    assert reading.value == 0.0


@pytest.mark.asyncio
async def test_unified_ratio_mixes_surfaces(db):
    """2 engaged outreach + 1 responded proposal + 1 silent proposal = 3/4."""
    now = datetime.now(UTC).isoformat()
    for i in range(2):
        await outreach_crud.create(
            db, id=f"m-{i}", signal_type="surplus", topic=f"M{i}",
            category="content", salience_score=0.8, channel="discord",
            message_content="Hi", created_at=now,
        )
        await outreach_crud.record_delivery(db, f"m-{i}", delivered_at=now)
        await outreach_crud.record_engagement(
            db, f"m-{i}", engagement_outcome="useful", engagement_signal="user_reply",
        )
    await _proposal_with_journal(db, "pr-4", now=now, resolved_reason="because")
    await _proposal_with_journal(db, "pr-5", now=now)

    reading = await OutreachEngagementCollector(db).collect()
    assert reading.value == 0.75
