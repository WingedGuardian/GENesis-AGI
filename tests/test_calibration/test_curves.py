"""Tests for calibration curve computation."""

import aiosqlite
import pytest

from genesis.calibration.curves import CalibrationCurveComputer
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
async def test_compute_curves_empty(db):
    computer = CalibrationCurveComputer(db)
    curves = await computer.compute("outreach")
    assert curves == []


@pytest.mark.asyncio
async def test_compute_curves_with_data(db):
    for i in range(10):
        await pred_crud.log_prediction(
            db, id=f"p-{i}", action_id=f"a-{i}",
            prediction="test", confidence=0.75,
            confidence_bucket="0.7-0.8", domain="outreach",
            reasoning="test",
        )
        await pred_crud.record_outcome(
            db, f"p-{i}", outcome="test", correct=(i < 6),
        )

    computer = CalibrationCurveComputer(db)
    curves = await computer.compute("outreach")
    assert len(curves) == 1
    assert curves[0]["confidence_bucket"] == "0.7-0.8"
    assert curves[0]["actual_success_rate"] == pytest.approx(0.6)
    assert curves[0]["sample_count"] == 10
    assert curves[0]["correction_factor"] == pytest.approx(0.6 / 0.75)


@pytest.mark.asyncio
async def test_compute_and_save(db):
    for i in range(5):
        await pred_crud.log_prediction(
            db, id=f"s-{i}", action_id=f"a-{i}",
            prediction="test", confidence=0.9,
            confidence_bucket="0.9-1.0", domain="triage",
            reasoning="test",
        )
        await pred_crud.record_outcome(db, f"s-{i}", outcome="ok", correct=True)

    computer = CalibrationCurveComputer(db)
    await computer.compute_and_save("triage")
    stored = await pred_crud.get_calibration_curves(db, "triage")
    assert len(stored) == 1
    assert stored[0]["actual_success_rate"] == pytest.approx(1.0)
