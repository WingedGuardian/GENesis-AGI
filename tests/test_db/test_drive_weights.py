"""Tests for drive_weights CRUD."""

from genesis.db.crud import drive_weights


async def test_get_all(db):
    rows = await drive_weights.get_all(db)
    assert len(rows) == 4
    names = {r["drive_name"] for r in rows}
    assert names == {"preservation", "curiosity", "cooperation", "competence"}


async def test_get_weight(db):
    w = await drive_weights.get_weight(db, "cooperation")
    assert w == 0.25  # seed value


async def test_get_weight_nonexistent(db):
    assert await drive_weights.get_weight(db, "nonexistent") is None


async def test_update_weight(db):
    ok = await drive_weights.update_weight(db, "cooperation", 0.30)
    assert ok
    w = await drive_weights.get_weight(db, "cooperation")
    assert abs(w - 0.30) < 0.001


async def test_update_weight_clamps_to_max(db):
    """Weights above max_weight are clamped."""
    await drive_weights.update_weight(db, "cooperation", 0.99)
    w = await drive_weights.get_weight(db, "cooperation")
    assert w <= 0.50  # max_weight


async def test_update_weight_clamps_to_min(db):
    """Weights below min_weight are clamped."""
    await drive_weights.update_weight(db, "cooperation", 0.01)
    w = await drive_weights.get_weight(db, "cooperation")
    assert w >= 0.10  # min_weight


async def test_adapt_weight(db):
    """adapt_weight applies delta and respects bounds."""
    initial = await drive_weights.get_weight(db, "curiosity")
    new_w = await drive_weights.adapt_weight(db, "curiosity", 0.05)
    assert new_w is not None
    assert abs(new_w - (initial + 0.05)) < 0.001


async def test_adapt_weight_clamps(db):
    """adapt_weight doesn't exceed bounds."""
    new_w = await drive_weights.adapt_weight(db, "curiosity", 1.0)
    assert new_w is not None
    assert new_w <= 0.50  # max_weight

    new_w = await drive_weights.adapt_weight(db, "curiosity", -1.0)
    assert new_w is not None
    assert new_w >= 0.10  # min_weight


async def test_adapt_weight_nonexistent(db):
    assert await drive_weights.adapt_weight(db, "nonexistent", 0.01) is None
