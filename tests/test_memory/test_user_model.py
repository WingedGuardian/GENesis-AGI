from __future__ import annotations

import json

import pytest

from genesis.db.crud import observations, user_model
from genesis.memory.user_model import UserModelEvolver


async def _create_delta(
    db, *, id: str, field: str, value: str, confidence: float
) -> None:
    await observations.create(
        db,
        id=id,
        source="reflection",
        type="user_model_delta",
        content=json.dumps({
            "field": field,
            "value": value,
            "evidence": "test evidence",
            "confidence": confidence,
        }),
        priority="medium",
        created_at="2026-03-08T00:00:00",
    )


@pytest.mark.asyncio
async def test_process_no_deltas(db):
    evolver = UserModelEvolver(db=db)
    result = await evolver.process_pending_deltas()
    assert result is None


@pytest.mark.asyncio
async def test_process_high_confidence_auto_accepts(db):
    await _create_delta(db, id="d1", field="preferred_language", value="Python", confidence=0.8)
    evolver = UserModelEvolver(db=db)
    result = await evolver.process_pending_deltas()
    assert result is not None
    assert result.model["preferred_language"] == "Python"
    assert result.version == 1
    assert result.evidence_count == 1


@pytest.mark.asyncio
async def test_process_low_confidence_not_accepted(db):
    await _create_delta(db, id="d1", field="timezone", value="UTC", confidence=0.4)
    evolver = UserModelEvolver(db=db)
    result = await evolver.process_pending_deltas()
    assert result is None


@pytest.mark.asyncio
async def test_process_accumulation_accepts(db):
    for i in range(3):
        await _create_delta(
            db, id=f"d{i}", field="editor", value="vim", confidence=0.4
        )
    evolver = UserModelEvolver(db=db)
    result = await evolver.process_pending_deltas()
    assert result is not None
    assert result.model["editor"] == "vim"
    assert result.evidence_count == 3


@pytest.mark.asyncio
async def test_process_marks_resolved(db):
    await _create_delta(db, id="d1", field="os", value="Linux", confidence=0.9)
    evolver = UserModelEvolver(db=db)
    await evolver.process_pending_deltas()
    row = await observations.get_by_id(db, "d1")
    assert row["resolved"] == 1


@pytest.mark.asyncio
async def test_get_current_model_empty(db):
    evolver = UserModelEvolver(db=db)
    assert await evolver.get_current_model() is None


@pytest.mark.asyncio
async def test_get_current_model_exists(db):
    await user_model.upsert(
        db,
        model_json={"lang": "Python"},
        synthesized_at="2026-03-08T00:00:00",
        synthesized_by="test",
        evidence_count=5,
    )
    evolver = UserModelEvolver(db=db)
    snapshot = await evolver.get_current_model()
    assert snapshot is not None
    assert snapshot.model == {"lang": "Python"}
    assert snapshot.version == 1
    assert snapshot.evidence_count == 5


@pytest.mark.asyncio
async def test_get_model_summary_empty(db):
    evolver = UserModelEvolver(db=db)
    assert await evolver.get_model_summary() == "No user model established yet."
