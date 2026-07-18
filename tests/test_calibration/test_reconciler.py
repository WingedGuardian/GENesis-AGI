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
    # WS-2 P1b: the engagement_outcome CHECK now ENFORCES the canonical
    # vocabulary (engaged/acted_on/acknowledged are legal members) — this
    # fixture runs with enforcement ON so a test writing outside the
    # vocabulary fails here, exactly like prod would.
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_reconcile_outreach_useful_is_correct(db):
    """A genuine reply ('useful') is a correct engagement prediction. Was
    asserted against 'engaged' — a value never written to the column — so every
    outreach prediction was graded incorrect regardless of the real outcome."""
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
    await outreach_crud.record_engagement(db, "outreach-abc", engagement_outcome="useful", engagement_signal="user_reply")

    reconciler = PredictionReconciler(db)
    count = await reconciler.reconcile_outreach()
    assert count == 1

    row = await pred_crud.get_by_id(db, "pred-1")
    assert row["outcome"] == "useful"
    assert row["correct"] == 1


@pytest.mark.asyncio
async def test_reconcile_outreach_noreply_is_skipped(db):
    """A 24h no-reply (outcome='ignored', signal='timeout') is unresolved, not a
    wrong prediction — the reconciler must skip it, not grade it incorrect
    (WS-0: 'ignored' != no value)."""
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
    assert count == 0  # no-reply skipped, not graded

    row = await pred_crud.get_by_id(db, "pred-2")
    assert row["outcome"] is None  # left unmatched, not scored incorrect


@pytest.mark.asyncio
async def test_reconcile_outreach_explicit_ignored_is_incorrect(db):
    """An explicit dismissal (outcome='ignored' with a non-'timeout' signal, e.g.
    via the outreach_engagement tool) IS a real negative and gets graded."""
    now = datetime.now(UTC).isoformat()
    await pred_crud.log_prediction(
        db, id="pred-x", action_id="outreach-x",
        prediction="user will engage", confidence=0.6,
        confidence_bucket="0.6-0.7", domain="outreach", reasoning="t",
    )
    await outreach_crud.create(
        db, id="outreach-x", signal_type="surplus", topic="X",
        category="surplus", salience_score=0.6, channel="telegram",
        message_content="Hi", created_at=now,
    )
    await outreach_crud.record_delivery(db, "outreach-x", delivered_at=now)
    await outreach_crud.record_engagement(db, "outreach-x", engagement_outcome="ignored", engagement_signal="user_dismiss")

    count = await PredictionReconciler(db).reconcile_outreach()
    assert count == 1
    row = await pred_crud.get_by_id(db, "pred-x")
    assert row["outcome"] == "ignored"
    assert row["correct"] == 0


@pytest.mark.asyncio
async def test_reconcile_outreach_engaged_dashboard_is_correct(db):
    """The dashboard /engage endpoint writes engagement_outcome='engaged' — a
    real positive that must grade correct (the original code only ever matched
    'engaged', which is precisely why replies were mis-graded)."""
    now = datetime.now(UTC).isoformat()
    await pred_crud.log_prediction(
        db, id="pred-e", action_id="outreach-e",
        prediction="user will engage", confidence=0.8,
        confidence_bucket="0.8-0.9", domain="outreach", reasoning="t",
    )
    await outreach_crud.create(
        db, id="outreach-e", signal_type="surplus", topic="E",
        category="surplus", salience_score=0.8, channel="telegram",
        message_content="Hi", created_at=now,
    )
    await outreach_crud.record_delivery(db, "outreach-e", delivered_at=now)
    await outreach_crud.record_engagement(db, "outreach-e", engagement_outcome="engaged", engagement_signal="dashboard")

    count = await PredictionReconciler(db).reconcile_outreach()
    assert count == 1
    row = await pred_crud.get_by_id(db, "pred-e")
    assert row["correct"] == 1


@pytest.mark.asyncio
async def test_reconcile_outreach_acted_on_is_correct(db):
    """The positive set includes behavioural signals beyond 'useful'."""
    now = datetime.now(UTC).isoformat()
    await pred_crud.log_prediction(
        db, id="pred-a", action_id="outreach-a",
        prediction="user will engage", confidence=0.8,
        confidence_bucket="0.8-0.9", domain="outreach", reasoning="t",
    )
    await outreach_crud.create(
        db, id="outreach-a", signal_type="surplus", topic="A",
        category="surplus", salience_score=0.8, channel="telegram",
        message_content="Hi", created_at=now,
    )
    await outreach_crud.record_delivery(db, "outreach-a", delivered_at=now)
    await outreach_crud.record_engagement(db, "outreach-a", engagement_outcome="acted_on", engagement_signal="behavioural")

    count = await PredictionReconciler(db).reconcile_outreach()
    assert count == 1
    row = await pred_crud.get_by_id(db, "pred-a")
    assert row["correct"] == 1


@pytest.mark.asyncio
async def test_reconcile_outreach_ambivalent_is_skipped(db):
    """An 'ambivalent' outcome (neutral implicit-activity signal) is not
    gradeable — the reconciler skips it, leaving the prediction unmatched
    rather than scoring it either way."""
    now = datetime.now(UTC).isoformat()
    await pred_crud.log_prediction(
        db, id="pred-amb", action_id="outreach-amb",
        prediction="user will engage", confidence=0.7,
        confidence_bucket="0.7-0.8", domain="outreach", reasoning="t",
    )
    await outreach_crud.create(
        db, id="outreach-amb", signal_type="surplus", topic="Amb",
        category="surplus", salience_score=0.7, channel="telegram",
        message_content="Hi", created_at=now,
    )
    await outreach_crud.record_delivery(db, "outreach-amb", delivered_at=now)
    await outreach_crud.record_engagement(db, "outreach-amb", engagement_outcome="ambivalent", engagement_signal="implicit_activity")

    count = await PredictionReconciler(db).reconcile_outreach()
    assert count == 0  # ambivalent skipped, not graded

    row = await pred_crud.get_by_id(db, "pred-amb")
    assert row["outcome"] is None  # left unmatched


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
