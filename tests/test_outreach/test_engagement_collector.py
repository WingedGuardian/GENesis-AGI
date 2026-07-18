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
            category="surplus", salience_score=0.8, channel="telegram",
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
            category="surplus", salience_score=0.8, channel="telegram",
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
            category="surplus", salience_score=0.8, channel="telegram",
            message_content="Hi", created_at=now,
        )
        await outreach_crud.record_delivery(db, f"eng-{i}", delivered_at=now)
        await outreach_crud.record_engagement(db, f"eng-{i}", engagement_outcome="useful", engagement_signal="user_reply")
    for i in range(2):
        await outreach_crud.create(
            db, id=f"ign-{i}", signal_type="surplus", topic=f"I{i}",
            category="surplus", salience_score=0.8, channel="telegram",
            message_content="Hi", created_at=now,
        )
        await outreach_crud.record_delivery(db, f"ign-{i}", delivered_at=now)
        await outreach_crud.record_engagement(db, f"ign-{i}", engagement_outcome="ignored", engagement_signal="timeout")

    collector = OutreachEngagementCollector(db)
    reading = await collector.collect()
    assert reading.value == pytest.approx(0.5)


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
            category="surplus", salience_score=0.8, channel="telegram",
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
