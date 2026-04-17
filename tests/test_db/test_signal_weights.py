"""Tests for signal_weights CRUD (uses seed data: 16 rows)."""

from genesis.db.crud import signal_weights


async def test_list_all(db):
    rows = await signal_weights.list_all(db)
    # 10 → 16 on 2026-04-17: +6 new signals (awareness scoring overhaul).
    assert len(rows) == 16


async def test_get_existing(db):
    row = await signal_weights.get(db, "critical_failure")
    assert row is not None
    assert row["signal_name"] == "critical_failure"


async def test_get_nonexistent(db):
    assert await signal_weights.get(db, "nope") is None


async def test_list_by_depth(db):
    rows = await signal_weights.list_by_depth(db, "Light")
    assert len(rows) > 0
    # micro_count_since_light feeds Light
    assert any(r["signal_name"] == "micro_count_since_light" for r in rows)


async def test_list_by_depth_empty(db):
    rows = await signal_weights.list_by_depth(db, "Nonexistent")
    assert rows == []


async def test_update_weight(db):
    assert await signal_weights.update_weight(db, "critical_failure", new_weight=0.50) is True
    row = await signal_weights.get(db, "critical_failure")
    assert row["current_weight"] == 0.50


async def test_update_weight_clamped_high(db):
    row_before = await signal_weights.get(db, "critical_failure")
    max_w = row_before["max_weight"]
    await signal_weights.update_weight(db, "critical_failure", new_weight=999.0)
    row = await signal_weights.get(db, "critical_failure")
    assert row["current_weight"] == max_w


async def test_update_weight_clamped_low(db):
    row_before = await signal_weights.get(db, "critical_failure")
    min_w = row_before["min_weight"]
    await signal_weights.update_weight(db, "critical_failure", new_weight=-999.0)
    row = await signal_weights.get(db, "critical_failure")
    assert row["current_weight"] == min_w


async def test_update_weight_nonexistent(db):
    assert await signal_weights.update_weight(db, "nope", new_weight=0.5) is False


async def test_reset_to_initial(db):
    await signal_weights.update_weight(db, "critical_failure", new_weight=0.50)
    assert await signal_weights.reset_to_initial(db, "critical_failure") is True
    row = await signal_weights.get(db, "critical_failure")
    assert row["current_weight"] == row["initial_weight"]


async def test_reset_nonexistent(db):
    assert await signal_weights.reset_to_initial(db, "nope") is False
