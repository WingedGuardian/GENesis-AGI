"""Tests for PredictionLogger."""

import aiosqlite
import pytest

from genesis.calibration.logger import PredictionLogger
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
async def test_log_creates_prediction(db):
    plogger = PredictionLogger(db)
    pred_id = await plogger.log(
        action_id="gov-decision-1",
        prediction="surplus outreach will be engaged",
        confidence=0.75,
        domain="outreach",
        reasoning="User has engaged with similar topics recently",
    )
    assert pred_id is not None
    row = await pred_crud.get_by_id(db, pred_id)
    assert row is not None
    assert row["confidence_bucket"] == "0.7-0.8"
    assert row["domain"] == "outreach"


@pytest.mark.asyncio
async def test_log_auto_buckets_confidence(db):
    plogger = PredictionLogger(db)
    pred_id = await plogger.log(
        action_id="triage-1",
        prediction="depth 2 classification",
        confidence=0.85,
        domain="triage",
        reasoning="Complex interaction with tool use",
    )
    row = await pred_crud.get_by_id(db, pred_id)
    assert row["confidence_bucket"] == "0.8-0.9"


@pytest.mark.asyncio
async def test_log_with_edge_confidence(db):
    plogger = PredictionLogger(db)
    pred_id = await plogger.log(
        action_id="route-1",
        prediction="gemini will succeed",
        confidence=1.0,
        domain="routing",
        reasoning="High availability",
    )
    row = await pred_crud.get_by_id(db, pred_id)
    assert row["confidence_bucket"] == "0.9-1.0"
