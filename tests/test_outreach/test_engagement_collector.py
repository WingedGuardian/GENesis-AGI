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
    # engagement_outcome CHECK is a no-op and prod carries drifted values
    # ('acted_on', 'acknowledged'); disable check enforcement for parity with
    # real data (see tests/feedback/test_harvest.py).
    await conn.execute("PRAGMA ignore_check_constraints = ON")
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


@pytest.mark.asyncio
async def test_proposals_with_typed_response_count_as_engaged(db):
    """A typed reason on a proposal resolution is engagement; an unresolved
    proposal counts toward total. 1 engaged / 2 outbound = 0.5."""
    from genesis.db.crud import ego as ego_crud

    now = datetime.now(UTC).isoformat()
    await ego_crud.create_proposal(
        db, id="pr-1", action_type="t", content="a", created_at=now,
    )
    await ego_crud.create_proposal(
        db, id="pr-2", action_type="t", content="b", created_at=now,
    )
    await ego_crud.resolve_proposal(
        db, "pr-1", status="rejected", user_response="typed deny reason",
    )

    reading = await OutreachEngagementCollector(db).collect()
    assert reading.value == 0.5
    assert "ego_proposals" in reading.source


@pytest.mark.asyncio
async def test_resolution_without_response_not_engaged(db):
    """A bare approve (no words) is outbound but not typed engagement."""
    from genesis.db.crud import ego as ego_crud

    now = datetime.now(UTC).isoformat()
    await ego_crud.create_proposal(
        db, id="pr-3", action_type="t", content="c", created_at=now,
    )
    await ego_crud.resolve_proposal(db, "pr-3", status="approved", user_response="")

    reading = await OutreachEngagementCollector(db).collect()
    assert reading.value == 0.0


@pytest.mark.asyncio
async def test_unified_ratio_mixes_surfaces(db):
    """2 engaged outreach + 1 responded proposal + 1 silent proposal = 3/4."""
    from genesis.db.crud import ego as ego_crud

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
    await ego_crud.create_proposal(
        db, id="pr-4", action_type="t", content="d", created_at=now,
    )
    await ego_crud.create_proposal(
        db, id="pr-5", action_type="t", content="e", created_at=now,
    )
    await ego_crud.resolve_proposal(
        db, "pr-4", status="rejected", user_response="because",
    )

    reading = await OutreachEngagementCollector(db).collect()
    assert reading.value == 0.75
