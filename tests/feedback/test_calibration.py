"""Tests for feedback/calibration.py + ego_calibration CRUD + migration 0026."""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.db.crud import ego_calibration as cal_crud
from genesis.db.crud import outcome_events as oe
from genesis.feedback.calibration import build_curve, compute_ego_calibration

MIGRATION = importlib.import_module("genesis.db.migrations.0026_ego_calibration_snapshots")


@pytest.fixture
async def db(tmp_path):
    """Full schema (outcome_events + ego_calibration_snapshots)."""
    from genesis.db.schema import create_all_tables

    path = str(tmp_path / "cal.db")
    async with aiosqlite.connect(path) as conn:
        await create_all_tables(conn)
        await conn.commit()
        yield conn


async def _add_t1(db, *, ref, conf, value, source="ego"):
    """Insert one tier-1 outcome_events row with confidence+value."""
    await oe.record(
        db, source=source, ref_type="proposal", ref_id=ref,
        signal_type="execution_outcome", signal_tier=1,
        polarity="positive" if value else "negative",
        value=value, stated_confidence=conf,
    )


# --------------------------------------------------------------------------- #
# build_curve (pure)
# --------------------------------------------------------------------------- #
class TestBuildCurve:
    def test_empty(self):
        assert build_curve([]) == []

    def test_buckets_and_rates(self):
        pairs = [
            {"stated_confidence": 0.85, "value": 1.0},
            {"stated_confidence": 0.82, "value": 0.0},  # same bucket 0.8-0.9
            {"stated_confidence": 0.95, "value": 1.0},  # bucket 0.9-1.0
        ]
        curve = build_curve(pairs)
        by_bucket = {c["confidence_bucket"]: c for c in curve}
        assert by_bucket["0.8-0.9"]["sample_count"] == 2
        assert by_bucket["0.8-0.9"]["actual_success_rate"] == 0.5
        assert abs(by_bucket["0.8-0.9"]["predicted_confidence"] - 0.85) < 1e-9  # midpoint
        assert by_bucket["0.9-1.0"]["actual_success_rate"] == 1.0
        assert abs(by_bucket["0.9-1.0"]["predicted_confidence"] - 0.95) < 1e-9

    def test_skips_null(self):
        pairs = [{"stated_confidence": None, "value": 1.0},
                 {"stated_confidence": 0.7, "value": None}]
        assert build_curve(pairs) == []


# --------------------------------------------------------------------------- #
# compute_ego_calibration
# --------------------------------------------------------------------------- #
class TestComputeEgoCalibration:
    @pytest.mark.asyncio
    async def test_zero_rows_writes_nothing(self, db):
        result = await compute_ego_calibration(db)
        assert result is None
        assert await cal_crud.get_latest(db) is None  # no spurious ece=0.0

    @pytest.mark.asyncio
    async def test_ece_mce_math(self, db):
        # Bucket 0.8-0.9: 10 rows, 9 success -> actual 0.9, predicted 0.85 -> |0.05|
        for i in range(10):
            await _add_t1(db, ref=f"a{i}", conf=0.85, value=1.0 if i < 9 else 0.0)
        # Bucket 0.9-1.0: 10 rows, 8 success -> actual 0.8, predicted 0.95 -> |0.15|
        for i in range(10):
            await _add_t1(db, ref=f"b{i}", conf=0.95, value=1.0 if i < 8 else 0.0)

        snap = await compute_ego_calibration(db)
        assert snap is not None
        assert snap["sample_count"] == 20
        assert snap["bucket_count"] == 2
        # ECE = 0.5*0.05 + 0.5*0.15 = 0.10 ; MCE = max(0.05, 0.15) = 0.15
        assert abs(snap["ece"] - 0.10) < 1e-6
        assert abs(snap["mce"] - 0.15) < 1e-6
        # 2 buckets (< 3) => low-confidence estimate
        assert snap["low_confidence"] is True

        stored = await cal_crud.get_latest(db)
        assert stored["ece"] == snap["ece"]
        assert len(stored["curve"]) == 2

    @pytest.mark.asyncio
    async def test_source_filter_excludes_non_ego(self, db):
        # ego rows
        for i in range(5):
            await _add_t1(db, ref=f"e{i}", conf=0.8, value=1.0)
        # outreach rows at tier 1 — MUST NOT be counted in ego calibration
        for i in range(5):
            await _add_t1(db, ref=f"o{i}", conf=0.2, value=0.0, source="outreach")

        snap = await compute_ego_calibration(db)
        assert snap["sample_count"] == 5  # only ego
        # the 0.2 outreach rows would have created a 0.2-0.3 bucket if leaked
        assert all(c["confidence_bucket"] != "0.2-0.3" for c in snap["curve"])

    @pytest.mark.asyncio
    async def test_low_confidence_clears_with_enough_data(self, db):
        # 3 buckets, 30 samples -> not low-confidence
        for i in range(10):
            await _add_t1(db, ref=f"x{i}", conf=0.75, value=1.0)
        for i in range(10):
            await _add_t1(db, ref=f"y{i}", conf=0.85, value=1.0 if i < 8 else 0.0)
        for i in range(10):
            await _add_t1(db, ref=f"z{i}", conf=0.95, value=1.0 if i < 9 else 0.0)
        snap = await compute_ego_calibration(db)
        assert snap["bucket_count"] == 3
        assert snap["sample_count"] == 30
        assert snap["low_confidence"] is False


# --------------------------------------------------------------------------- #
# CRUD + migration
# --------------------------------------------------------------------------- #
class TestCrudAndMigration:
    @pytest.mark.asyncio
    async def test_get_trend_orders_newest_first(self, db):
        await cal_crud.record_snapshot(
            db, domain="ego", ece=0.10, mce=0.2, sample_count=20,
            bucket_count=2, low_confidence=True, curve=[{"x": 1}],
        )
        await cal_crud.record_snapshot(
            db, domain="ego", ece=0.05, mce=0.1, sample_count=40,
            bucket_count=4, low_confidence=False, curve=[{"x": 2}],
        )
        trend = await cal_crud.get_trend(db, domain="ego")
        assert len(trend) == 2
        assert trend[0]["ece"] == 0.05  # newest first
        assert trend[0]["low_confidence"] is False

    @pytest.mark.asyncio
    async def test_mcp_surface_no_data_and_ok(self, db, monkeypatch):
        import genesis.mcp.health_mcp as hm
        from genesis.mcp.health.ego_calibration import _impl_ego_calibration_status

        class _StubService:
            def __init__(self, _db):
                self._db = _db

        monkeypatch.setattr(hm, "_service", _StubService(db), raising=False)

        # No snapshots yet -> no_data (never a spurious ece=0.0)
        res = await _impl_ego_calibration_status()
        assert res["status"] == "no_data"

        # Seed a snapshot -> ok, with readable curve + MCE caveat note
        await cal_crud.record_snapshot(
            db, domain="ego", ece=0.116, mce=0.45, sample_count=47,
            bucket_count=5, low_confidence=False,
            curve=[{"confidence_bucket": "0.8-0.9", "predicted_confidence": 0.85,
                    "actual_success_rate": 0.82, "sample_count": 22}],
        )
        res = await _impl_ego_calibration_status()
        assert res["status"] == "ok"
        assert res["ece"] == 0.116
        assert res["curve_readable"][0].startswith("says ~85%")
        assert res["note"] is not None  # mce 0.45 > 2*ece 0.232 → caveat shown

    @pytest.mark.asyncio
    async def test_migration_up_down(self, tmp_path):
        path = str(tmp_path / "m.db")
        async with aiosqlite.connect(path) as conn:
            await MIGRATION.up(conn)
            await MIGRATION.up(conn)  # idempotent
            await conn.commit()
            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='ego_calibration_snapshots'"
            )
            assert await cur.fetchone() is not None
            await MIGRATION.down(conn)
            await conn.commit()
            cur = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='ego_calibration_snapshots'"
            )
            assert await cur.fetchone() is None
