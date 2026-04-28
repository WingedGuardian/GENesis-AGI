"""Tests for depth_thresholds CRUD module."""

from genesis.db.crud import depth_thresholds


async def test_get_existing(db):
    row = await depth_thresholds.get(db, "Micro")
    assert row is not None
    assert row["threshold"] == 0.50
    assert row["floor_seconds"] == 1800


async def test_get_nonexistent(db):
    row = await depth_thresholds.get(db, "Nonexistent")
    assert row is None


async def test_list_all(db):
    rows = await depth_thresholds.list_all(db)
    assert len(rows) == 4
    names = [r["depth_name"] for r in rows]
    assert "Micro" in names
    assert "Strategic" in names


async def test_update_threshold(db):
    ok = await depth_thresholds.update_threshold(db, "Deep", new_threshold=0.60)
    assert ok is True
    row = await depth_thresholds.get(db, "Deep")
    assert row["threshold"] == 0.60
