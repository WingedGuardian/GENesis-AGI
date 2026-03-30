"""Tests for prediction-outcome reconciliation."""

from datetime import UTC, datetime

import aiosqlite
import pytest

from genesis.calibration.reconciler import PredictionReconciler
from genesis.db.crud import outreach as outreach_crud
from genesis.db.crud import predictions as pred_crud
from genesis.db.schema import create_all_tables


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_reconcile_outreach_engaged(db):
    now = datetime.now(UTC).isoformat()
    await pred_crud.log_prediction(
        db, id="pred-1", action_id="outreach-abc",
        prediction="user will engage", confidence=0.8,
        confidence_bucket="0.8-0.9", domain="outreach",
        reasoning="similar topic engaged before",
    )
    await outreach_crud.create(
        db, id="outreach-abc", signal_type="surplus", topic="Test",
        category="surplus", salience_score=0.8, channel="telegram",
        message_content="Hello", created_at=now,
    )
    await outreach_crud.record_delivery(db, "outreach-abc", delivered_at=now)
    await outreach_crud.record_engagement(db, "outreach-abc", engagement_outcome="engaged", engagement_signal="reply")

    reconciler = PredictionReconciler(db)
    count = await reconciler.reconcile_outreach()
    assert count == 1

    row = await pred_crud.get_by_id(db, "pred-1")
    assert row["outcome"] == "engaged"
    assert row["correct"] == 1


@pytest.mark.asyncio
async def test_reconcile_outreach_ignored(db):
    now = datetime.now(UTC).isoformat()
    await pred_crud.log_prediction(
        db, id="pred-2", action_id="outreach-def",
        prediction="user will engage", confidence=0.7,
        confidence_bucket="0.7-0.8", domain="outreach",
        reasoning="test",
    )
    await outreach_crud.create(
        db, id="outreach-def", signal_type="surplus", topic="Test2",
        category="surplus", salience_score=0.7, channel="telegram",
        message_content="Hi", created_at=now,
    )
    await outreach_crud.record_delivery(db, "outreach-def", delivered_at=now)
    await outreach_crud.record_engagement(db, "outreach-def", engagement_outcome="ignored", engagement_signal="timeout")

    reconciler = PredictionReconciler(db)
    count = await reconciler.reconcile_outreach()
    assert count == 1

    row = await pred_crud.get_by_id(db, "pred-2")
    assert row["outcome"] == "ignored"
    assert row["correct"] == 0


@pytest.mark.asyncio
async def test_reconcile_skips_already_matched(db):
    await pred_crud.log_prediction(
        db, id="pred-3", action_id="outreach-ghi",
        prediction="test", confidence=0.5,
        confidence_bucket="0.5-0.6", domain="outreach",
        reasoning="test",
    )
    await pred_crud.record_outcome(db, "pred-3", outcome="engaged", correct=True)

    reconciler = PredictionReconciler(db)
    count = await reconciler.reconcile_outreach()
    assert count == 0
