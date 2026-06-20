"""Tests for the self_improvement_status MCP tool (soak observability).

DARK, read-only: the tool surfaces Outcome Bus coverage (tiers/signal_types),
the per-domain T1 success picture, and the ego ECE trend — without ranking or
changing behaviour. Exercises the _impl directly against a stubbed
HealthDataService, mirroring the ego_calibration MCP surface test.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import ego_calibration as cal_crud
from genesis.db.crud import outcome_events as oe
from genesis.mcp.health.self_improvement_status import _impl_self_improvement_status


@pytest.fixture
async def db(tmp_path):
    """Full schema (outcome_events + ego_calibration_snapshots)."""
    from genesis.db.schema import create_all_tables

    path = str(tmp_path / "sis.db")
    async with aiosqlite.connect(path) as conn:
        await create_all_tables(conn)
        await conn.commit()
        yield conn


class _StubService:
    def __init__(self, _db):
        self._db = _db


@pytest.mark.asyncio
async def test_unavailable_when_no_service(monkeypatch):
    import genesis.mcp.health_mcp as hm

    monkeypatch.setattr(hm, "_service", None, raising=False)
    res = await _impl_self_improvement_status()
    assert res["status"] == "unavailable"


@pytest.mark.asyncio
async def test_empty_db_is_coherent(db, monkeypatch):
    import genesis.mcp.health_mcp as hm

    monkeypatch.setattr(hm, "_service", _StubService(db), raising=False)
    res = await _impl_self_improvement_status()
    assert res["status"] == "ok"
    assert res["bus_total_events"] == 0
    assert res["tier_counts"] == {}
    assert res["t1_domains"] == []
    # No snapshot yet → no_data (never a spurious ece=0.0).
    assert res["ego_calibration"]["status"] == "no_data"


@pytest.mark.asyncio
async def test_seeded_soak_snapshot(db, monkeypatch):
    import genesis.mcp.health_mcp as hm

    monkeypatch.setattr(hm, "_service", _StubService(db), raising=False)

    # T1 ground truth: dispatch 1 pass + 1 fail (→0.5), investigate 1 pass (→1.0)
    await oe.record(
        db, source="ego", ref_type="proposal", ref_id="d1",
        signal_type="execution_outcome", signal_tier=1, domain="dispatch",
        polarity="positive", value=1.0, stated_confidence=0.9,
    )
    await oe.record(
        db, source="ego", ref_type="proposal", ref_id="d2",
        signal_type="execution_outcome", signal_tier=1, domain="dispatch",
        polarity="negative", value=0.0, stated_confidence=0.7,
    )
    await oe.record(
        db, source="ego", ref_type="proposal", ref_id="i1",
        signal_type="execution_outcome", signal_tier=1, domain="investigate",
        polarity="positive", value=1.0,
    )
    # One T3 coverage row (neutral, no value) — must NOT contaminate the T1 view.
    await oe.record(
        db, source="ego", ref_type="proposal", ref_id="x1",
        signal_type="dispatch", signal_tier=3, domain="approval",
        polarity="neutral",
    )
    await cal_crud.record_snapshot(
        db, domain="ego", ece=0.116, mce=0.45, sample_count=47,
        bucket_count=5, low_confidence=False,
        curve=[{"confidence_bucket": "0.8-0.9", "predicted_confidence": 0.85,
                "actual_success_rate": 0.82, "sample_count": 22}],
    )

    res = await _impl_self_improvement_status()
    assert res["status"] == "ok"
    assert res["bus_total_events"] == 4
    # Keys are stringified to match the JSON wire shape an MCP client receives.
    assert res["tier_counts"] == {"1": 3, "3": 1}

    t1 = {d["domain"]: d for d in res["t1_domains"]}
    # The T3-only 'approval' domain must be ABSENT from the tier-1 view.
    assert "approval" not in t1
    assert t1["dispatch"]["n"] == 2
    assert t1["dispatch"]["success_rate"] == 0.5
    assert t1["dispatch"]["positive"] == 1
    assert t1["dispatch"]["negative"] == 1
    assert t1["investigate"]["success_rate"] == 1.0

    cal = res["ego_calibration"]
    assert cal["status"] == "ok"
    assert cal["ece"] == 0.116
    assert cal["low_confidence"] is False
    assert cal["ece_trend"] == [0.116]
