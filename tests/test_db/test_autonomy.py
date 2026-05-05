"""Tests for autonomy_state CRUD."""

from genesis.db.crud import autonomy
from genesis.db.crud.autonomy import bayesian_level, bayesian_posterior

_COMMON = dict(
    category="code_execution",
    updated_at="2026-01-01T00:00:00",
)


async def test_create_and_get(db):
    rid = await autonomy.create(db, id="a1", **_COMMON)
    assert rid == "a1"
    row = await autonomy.get_by_id(db, "a1")
    assert row is not None
    assert row["current_level"] == 1


async def test_get_nonexistent(db):
    assert await autonomy.get_by_id(db, "nope") is None


async def test_get_by_category(db):
    await autonomy.create(db, id="a2", **_COMMON)
    row = await autonomy.get_by_category(db, "code_execution")
    assert row is not None


async def test_get_by_category_nonexistent(db):
    assert await autonomy.get_by_category(db, "nonexistent") is None


async def test_list_all(db):
    await autonomy.create(db, id="a3", category="cat1", updated_at="2026-01-01")
    await autonomy.create(db, id="a4", category="cat2", updated_at="2026-01-01")
    rows = await autonomy.list_all(db)
    assert len(rows) >= 2


async def test_record_success(db):
    await autonomy.create(db, id="a5", **_COMMON)
    assert await autonomy.record_success(db, "a5", updated_at="2026-01-02") is True
    row = await autonomy.get_by_id(db, "a5")
    assert row["total_successes"] == 1
    assert row["consecutive_corrections"] == 0


async def test_record_success_nonexistent(db):
    assert await autonomy.record_success(db, "nope", updated_at="x") is False


async def test_record_correction_single(db):
    await autonomy.create(db, id="a6", category="corr1", updated_at="2026-01-01")
    assert await autonomy.record_correction(db, "a6", corrected_at="2026-01-02", updated_at="2026-01-02") is True
    row = await autonomy.get_by_id(db, "a6")
    assert row["total_corrections"] == 1
    assert row["current_level"] == 1  # already at L1, can't go lower


async def test_record_correction_bayesian_regression(db):
    """Bayesian regression: correction drops level based on posterior, not hard counter."""
    # Start at L3 with 0 successes — one correction is enough evidence to regress
    await autonomy.create(db, id="a7", category="corr2", updated_at="2026-01-01", current_level=3)
    await autonomy.record_correction(db, "a7", corrected_at="2026-01-02", updated_at="2026-01-02")
    row = await autonomy.get_by_id(db, "a7")
    # 0S + 1C → posterior = 1/3 = 0.33 → bayesian L2 → regressed from L3
    assert row["current_level"] == 2
    assert "Bayesian" in row["regression_reason"]


async def test_bayesian_stable_at_high_success(db):
    """50S + 2C → posterior 0.94 → stays L4 (Bayesian regression is proportional)."""
    await autonomy.create(db, id="a7b", category="stable1", updated_at="2026-01-01", current_level=4)
    # Manually set successes to 50
    await db.execute("UPDATE autonomy_state SET total_successes = 50 WHERE id = 'a7b'")
    await db.commit()
    # Two corrections shouldn't regress from L4 (posterior stays high)
    await autonomy.record_correction(db, "a7b", corrected_at="t1", updated_at="t1")
    await autonomy.record_correction(db, "a7b", corrected_at="t2", updated_at="t2")
    row = await autonomy.get_by_id(db, "a7b")
    assert row["current_level"] == 4  # 50S + 2C → posterior 0.94 → stays L4


async def test_bayesian_mixed_evidence_drops(db):
    """3S + 2C → posterior 0.57 → L3 (drops from L4 under mixed evidence)."""
    await autonomy.create(db, id="a7c", category="mixed1", updated_at="2026-01-01", current_level=4)
    await db.execute("UPDATE autonomy_state SET total_successes = 3 WHERE id = 'a7c'")
    await db.commit()
    await autonomy.record_correction(db, "a7c", corrected_at="t1", updated_at="t1")
    await autonomy.record_correction(db, "a7c", corrected_at="t2", updated_at="t2")
    row = await autonomy.get_by_id(db, "a7c")
    assert row["current_level"] == 3  # 3S + 2C → posterior 0.57 → L3


async def test_record_correction_nonexistent(db):
    assert await autonomy.record_correction(db, "nope", corrected_at="x", updated_at="x") is False


async def test_regression_floors_at_one(db):
    await autonomy.create(db, id="a8", category="floor1", updated_at="2026-01-01", current_level=1)
    await autonomy.record_correction(db, "a8", corrected_at="t1", updated_at="t1")
    await autonomy.record_correction(db, "a8", corrected_at="t2", updated_at="t2")
    row = await autonomy.get_by_id(db, "a8")
    assert row["current_level"] == 1  # can't go below 1


async def test_success_resets_consecutive(db):
    await autonomy.create(db, id="a9", category="reset1", updated_at="2026-01-01")
    await autonomy.record_correction(db, "a9", corrected_at="t1", updated_at="t1")
    await autonomy.record_success(db, "a9", updated_at="t2")
    row = await autonomy.get_by_id(db, "a9")
    assert row["consecutive_corrections"] == 0


async def test_delete(db):
    await autonomy.create(db, id="a10", category="del1", updated_at="2026-01-01")
    assert await autonomy.delete(db, "a10") is True


async def test_delete_nonexistent(db):
    assert await autonomy.delete(db, "nope") is False


# ─── Bayesian level function tests ───────────────────────────────────────────


def test_bayesian_level_no_evidence():
    assert bayesian_level(0, 0) == 1


def test_bayesian_level_high_success():
    # 50S + 2C → 51/54 = 0.944 → L4
    assert bayesian_level(50, 2) == 4


def test_bayesian_level_mixed():
    # 3S + 2C → 4/7 = 0.571 → L3
    assert bayesian_level(3, 2) == 3


def test_bayesian_level_low_success():
    # 0S + 3C → 1/5 = 0.20 → L1
    assert bayesian_level(0, 3) == 1


def test_bayesian_level_l2_threshold():
    # 1S + 2C → 2/5 = 0.40 → L2
    assert bayesian_level(1, 2) == 2


def test_bayesian_posterior_uninformative():
    assert bayesian_posterior(0, 0) == 0.5


def test_bayesian_posterior_computation():
    # 50S + 2C → 51/54 ≈ 0.944
    assert abs(bayesian_posterior(50, 2) - 0.944) < 0.01


async def test_success_does_not_auto_promote(db):
    """record_success increments counter but does NOT change level."""
    await autonomy.create(db, id="promo1", category="promo1", updated_at="2026-01-01", current_level=1)
    # Even with high posterior, level stays unchanged
    await autonomy.record_success(db, "promo1", updated_at="t1")
    row = await autonomy.get_by_id(db, "promo1")
    assert row["current_level"] == 1  # NOT promoted
    assert row["earned_level"] == 1
    assert row["total_successes"] == 1
    assert row["consecutive_corrections"] == 0


async def test_success_many_does_not_promote(db):
    """Even many successes do not auto-promote — requires explicit promote()."""
    await autonomy.create(db, id="promo2", category="promo2", updated_at="2026-01-01", current_level=1)
    await db.execute("UPDATE autonomy_state SET total_successes = 49 WHERE id = 'promo2'")
    await db.commit()
    await autonomy.record_success(db, "promo2", updated_at="t1")
    row = await autonomy.get_by_id(db, "promo2")
    assert row["current_level"] == 1  # Still L1 — no auto-promote
    assert row["total_successes"] == 50


async def test_promote_explicit(db):
    """promote() explicitly raises level on user approval."""
    await autonomy.create(db, id="pro1", category="pro1", updated_at="2026-01-01", current_level=1)
    result = await autonomy.promote(db, "pro1", to_level=2, updated_at="t1")
    assert result is True
    row = await autonomy.get_by_id(db, "pro1")
    assert row["current_level"] == 2
    assert row["earned_level"] == 2


async def test_promote_rejects_non_promotion(db):
    """promote() returns False if to_level <= current."""
    await autonomy.create(db, id="pro2", category="pro2", updated_at="2026-01-01", current_level=3)
    result = await autonomy.promote(db, "pro2", to_level=2, updated_at="t1")
    assert result is False
    result = await autonomy.promote(db, "pro2", to_level=3, updated_at="t1")
    assert result is False


async def test_promote_rejects_invalid_level(db):
    """promote() validates level range 1-4."""
    await autonomy.create(db, id="pro3", category="pro3", updated_at="2026-01-01", current_level=1)
    assert await autonomy.promote(db, "pro3", to_level=0, updated_at="t1") is False
    assert await autonomy.promote(db, "pro3", to_level=5, updated_at="t1") is False


async def test_promote_nonexistent(db):
    """promote() returns False for missing id."""
    assert await autonomy.promote(db, "ghost", to_level=2, updated_at="t1") is False


async def test_force_regress(db):
    """force_regress() resets BOTH current and earned level."""
    await autonomy.create(
        db, id="fr1", category="fr1", updated_at="2026-01-01",
        current_level=4, earned_level=4,
    )
    result = await autonomy.force_regress(db, "fr1", to_level=1, reason="user_revoked", updated_at="t1")
    assert result is True
    row = await autonomy.get_by_id(db, "fr1")
    assert row["current_level"] == 1
    assert row["earned_level"] == 1  # earned also reset
    assert row["regression_reason"] == "user_revoked"


async def test_force_regress_rejects_upward(db):
    """force_regress() rejects 'regression' to a higher level."""
    await autonomy.create(
        db, id="fr2", category="fr2", updated_at="2026-01-01",
        current_level=2, earned_level=2,
    )
    result = await autonomy.force_regress(db, "fr2", to_level=3, reason="test", updated_at="t1")
    assert result is False
    row = await autonomy.get_by_id(db, "fr2")
    assert row["current_level"] == 2  # unchanged


async def test_force_regress_rejects_same_level(db):
    """force_regress() rejects 'regression' to same level."""
    await autonomy.create(
        db, id="fr3", category="fr3", updated_at="2026-01-01",
        current_level=2, earned_level=2,
    )
    result = await autonomy.force_regress(db, "fr3", to_level=2, reason="test", updated_at="t1")
    assert result is False


async def test_force_regress_nonexistent(db):
    """force_regress() returns False for missing id."""
    assert await autonomy.force_regress(db, "ghost", updated_at="t1") is False


# ─── person_id tests ─────────────────────────────────────────────────────────


async def test_person_id_defaults_to_none(db):
    await autonomy.create(db, id="apid1", **_COMMON)
    row = await autonomy.get_by_id(db, "apid1")
    assert row["person_id"] is None


async def test_create_with_person_id(db):
    await autonomy.create(db, id="apid2", person_id="user-42", **_COMMON)
    row = await autonomy.get_by_id(db, "apid2")
    assert row["person_id"] == "user-42"


async def test_list_all_filters_by_person_id(db):
    await autonomy.create(db, id="apid3", category="cat_a", person_id="alice", updated_at="2026-01-01")
    await autonomy.create(db, id="apid4", category="cat_b", person_id="bob", updated_at="2026-01-01")
    rows = await autonomy.list_all(db, person_id="alice")
    assert len(rows) == 1
    assert rows[0]["id"] == "apid3"
