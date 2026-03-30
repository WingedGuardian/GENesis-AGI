"""Tests for predictions CRUD."""

import aiosqlite
import pytest

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
async def test_log_prediction(db):
    await pred_crud.log_prediction(
        db,
        id="p-1",
        action_id="a-1",
        prediction="will engage",
        confidence=0.75,
        confidence_bucket="0.7-0.8",
        domain="outreach",
        reasoning="user likes this topic",
    )
    row = await pred_crud.get_by_id(db, "p-1")
    assert row is not None
    assert row["confidence"] == 0.75
    assert row["outcome"] is None


@pytest.mark.asyncio
async def test_record_outcome(db):
    await pred_crud.log_prediction(
        db, id="p-2", action_id="a-2", prediction="will engage",
        confidence=0.8, confidence_bucket="0.8-0.9", domain="outreach",
        reasoning="test",
    )
    await pred_crud.record_outcome(db, "p-2", outcome="engaged", correct=True)
    row = await pred_crud.get_by_id(db, "p-2")
    assert row["outcome"] == "engaged"
    assert row["correct"] == 1
    assert row["matched_at"] is not None


@pytest.mark.asyncio
async def test_list_unmatched(db):
    await pred_crud.log_prediction(
        db, id="p-3", action_id="a-3", prediction="test",
        confidence=0.6, confidence_bucket="0.6-0.7", domain="triage",
        reasoning="test",
    )
    unmatched = await pred_crud.list_unmatched(db, domain="triage")
    assert len(unmatched) == 1
    assert unmatched[0]["id"] == "p-3"


@pytest.mark.asyncio
async def test_save_and_get_calibration_curve(db):
    await pred_crud.save_calibration_curve(
        db,
        domain="outreach",
        confidence_bucket="0.7-0.8",
        predicted_confidence=0.75,
        actual_success_rate=0.60,
        sample_count=55,
        correction_factor=0.80,
    )
    curves = await pred_crud.get_calibration_curves(db, domain="outreach")
    assert len(curves) == 1
    assert curves[0]["correction_factor"] == pytest.approx(0.80)
