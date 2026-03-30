"""Tests for user_model_cache CRUD."""

from genesis.db.crud import user_model


async def test_get_current_empty(db):
    assert await user_model.get_current(db) is None


async def test_upsert_creates(db):
    rid = await user_model.upsert(
        db, model_json={"pref": "dark"}, synthesized_at="2026-01-01",
        synthesized_by="opus",
    )
    assert rid == "current"
    row = await user_model.get_current(db)
    assert row is not None
    assert row["version"] == 1


async def test_upsert_updates(db):
    await user_model.upsert(
        db, model_json={"v": 1}, synthesized_at="2026-01-01", synthesized_by="opus",
    )
    await user_model.upsert(
        db, model_json={"v": 2}, synthesized_at="2026-01-02", synthesized_by="opus",
        evidence_count=5, last_change_type="preference",
    )
    row = await user_model.get_current(db)
    assert row["version"] == 2
    assert row["evidence_count"] == 5


async def test_delete(db):
    await user_model.upsert(
        db, model_json={}, synthesized_at="2026-01-01", synthesized_by="opus",
    )
    assert await user_model.delete(db) is True
    assert await user_model.get_current(db) is None


async def test_delete_when_empty(db):
    assert await user_model.delete(db) is False


# ─── person_id tests ─────────────────────────────────────────────────────────


async def test_upsert_with_person_id(db):
    rid = await user_model.upsert(
        db, model_json={"pref": "light"}, synthesized_at="2026-01-01",
        synthesized_by="opus", person_id="user-42",
    )
    assert rid == "user-42"
    row = await user_model.get_by_person(db, "user-42")
    assert row is not None
    assert row["person_id"] == "user-42"
    # Default user model is unaffected
    assert await user_model.get_current(db) is None


async def test_get_by_person_nonexistent(db):
    assert await user_model.get_by_person(db, "nobody") is None


async def test_person_model_version_increments(db):
    await user_model.upsert(
        db, model_json={"v": 1}, synthesized_at="2026-01-01",
        synthesized_by="opus", person_id="user-99",
    )
    await user_model.upsert(
        db, model_json={"v": 2}, synthesized_at="2026-01-02",
        synthesized_by="opus", person_id="user-99",
    )
    row = await user_model.get_by_person(db, "user-99")
    assert row["version"] == 2
