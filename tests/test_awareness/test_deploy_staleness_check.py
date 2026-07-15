"""Tests for the deploy-staleness awareness check (_check_deploy_staleness).

The collectors (observability/snapshots/deploy_health.py) have their own tests;
here the snapshot function is monkeypatched so each test controls the exact
drift state. What's under test is the ALERTING state machine: hybrid severity
(high on any drift, critical only sustained), class-keyed dedup that survives
per-run count drift, the >24h missing-unit escalation anchor (restart-safe,
immune to its own escalation resetting the clock), supersede-on-state-change,
and resolve-on-recovery.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.awareness import loop
from genesis.db.schema import create_all_tables

# The snapshots package __init__ shadows the submodule name with the function
# of the same name, so a plain `import … as dh_module` binds the FUNCTION.
# import_module returns the real module — the attribute loop.py resolves at
# call time, so patching it here is what the check actually sees.
dh_module = importlib.import_module("genesis.observability.snapshots.deploy_health")

SOURCE = "deploy_staleness_monitor"


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture(autouse=True)
def _reset_cooldowns(monkeypatch):
    monkeypatch.setattr(loop, "_last_deploy_alert_at", 0.0)
    monkeypatch.setattr(loop, "_last_deploy_alert_key", "")


def _snap(
    findings,
    *,
    age_days=None,
    behind=None,
    missing_units=None,
    tier2=None,
    host_status="ok",
):
    return {
        "status": "attention" if findings else "healthy",
        "findings": findings,
        "last_update": {"age_days": age_days, "new_commit": "abc", "completed_at": "x"},
        "git": {"head": "abc", "commits_behind_upstream": behind, "fetch_age_hours": 1.0},
        "missing_units": missing_units or [],
        "tier2_pending": tier2 or [],
        "host_gateway": {"status": host_status},
    }


def _patch_snapshot(monkeypatch, snap):
    async def fake(db):
        return snap

    monkeypatch.setattr(dh_module, "deploy_health", fake)


async def _rows(db, resolved=0):
    cur = await db.execute(
        "SELECT id, priority, content, content_hash, created_at, resolution_notes "
        f"FROM observations WHERE source='{SOURCE}' AND resolved={resolved}"
    )
    return list(await cur.fetchall())


async def test_no_findings_is_quiet(db, monkeypatch):
    _patch_snapshot(monkeypatch, _snap([]))
    await loop._check_deploy_staleness(db)
    assert await _rows(db) == []


async def test_drift_raises_high_once(db, monkeypatch):
    _patch_snapshot(
        monkeypatch,
        _snap(["tier2_pending:2"], age_days=1.0, behind=3, tier2=["a", "b"]),
    )
    await loop._check_deploy_staleness(db)
    rows = await _rows(db)
    assert len(rows) == 1
    assert rows[0]["priority"] == "high"
    assert "update.sh" in rows[0]["content"]
    # Second run, same state: content_hash dedup + cooldown — no second row.
    await loop._check_deploy_staleness(db)
    assert len(await _rows(db)) == 1


async def test_count_drift_does_not_churn_alert(db, monkeypatch):
    """behind_upstream:52 -> :53 is the same alert state, not a new alert."""
    _patch_snapshot(monkeypatch, _snap(["behind_upstream:52"], behind=52))
    await loop._check_deploy_staleness(db)
    first = await _rows(db)
    monkeypatch.setattr(loop, "_last_deploy_alert_at", 0.0)  # bypass cooldown
    _patch_snapshot(monkeypatch, _snap(["behind_upstream:53"], behind=53))
    await loop._check_deploy_staleness(db)
    rows = await _rows(db)
    assert len(rows) == 1
    assert rows[0]["content_hash"] == first[0]["content_hash"]


async def test_sustained_staleness_is_critical(db, monkeypatch):
    _patch_snapshot(
        monkeypatch,
        _snap(["behind_upstream:25"], age_days=8.0, behind=25),
    )
    await loop._check_deploy_staleness(db)
    rows = await _rows(db)
    assert len(rows) == 1
    assert rows[0]["priority"] == "critical"


async def test_escalation_supersedes_high_row(db, monkeypatch):
    _patch_snapshot(monkeypatch, _snap(["behind_upstream:25"], age_days=1.0, behind=25))
    await loop._check_deploy_staleness(db)
    assert (await _rows(db))[0]["priority"] == "high"
    monkeypatch.setattr(loop, "_last_deploy_alert_at", 0.0)
    _patch_snapshot(monkeypatch, _snap(["behind_upstream:25"], age_days=8.0, behind=25))
    await loop._check_deploy_staleness(db)
    active = await _rows(db)
    assert len(active) == 1
    assert active[0]["priority"] == "critical"
    superseded = await _rows(db, resolved=1)
    assert len(superseded) == 1
    assert superseded[0]["resolution_notes"] == loop._DEPLOY_SUPERSEDED_NOTE


async def test_missing_unit_escalates_after_24h(db, monkeypatch):
    snap = _snap(
        ["missing_units:x.timer"],
        age_days=1.0,
        behind=0,
        missing_units=["x.timer"],
    )
    _patch_snapshot(monkeypatch, snap)
    await loop._check_deploy_staleness(db)
    rows = await _rows(db)
    assert rows[0]["priority"] == "high"
    # Age the anchor row past 24h; the same state now escalates.
    old = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    await db.execute(f"UPDATE observations SET created_at=? WHERE source='{SOURCE}'", (old,))
    await db.commit()
    monkeypatch.setattr(loop, "_last_deploy_alert_at", 0.0)
    await loop._check_deploy_staleness(db)
    active = await _rows(db)
    assert len(active) == 1
    assert active[0]["priority"] == "critical"
    # The escalated row must NOT reset the clock: the superseded high row (old
    # created_at, superseded note) still anchors, so the state STAYS critical.
    monkeypatch.setattr(loop, "_last_deploy_alert_at", 0.0)
    await loop._check_deploy_staleness(db)
    active = await _rows(db)
    assert len(active) == 1
    assert active[0]["priority"] == "critical"


async def test_recovery_resolves_and_retires_anchors(db, monkeypatch):
    snap = _snap(["missing_units:x.timer"], age_days=1.0, behind=0, missing_units=["x.timer"])
    _patch_snapshot(monkeypatch, snap)
    await loop._check_deploy_staleness(db)
    # Simulate a prior superseded row too (escalation happened at some point).
    await db.execute(
        "INSERT INTO observations (id, source, type, content, priority, created_at,"
        " resolved, resolution_notes) VALUES ('old', ?, 'infrastructure_alert',"
        " 'missing_units:x.timer', 'high', '2020-01-01T00:00:00+00:00', 1, ?)",
        (SOURCE, loop._DEPLOY_SUPERSEDED_NOTE),
    )
    await db.commit()
    _patch_snapshot(monkeypatch, _snap([]))
    await loop._check_deploy_staleness(db)
    assert await _rows(db) == []  # active alert resolved
    # Anchor retired: no row carries the superseded note anymore, so a future
    # missing unit cannot inherit this incident's clock and page instantly.
    cur = await db.execute(
        "SELECT COUNT(*) FROM observations WHERE source=? AND resolution_notes=?",
        (SOURCE, loop._DEPLOY_SUPERSEDED_NOTE),
    )
    assert (await cur.fetchone())[0] == 0


async def test_snapshot_error_is_quiet(db, monkeypatch):
    _patch_snapshot(monkeypatch, {"status": "error"})
    await loop._check_deploy_staleness(db)
    assert await _rows(db) == []


async def test_check_never_raises_into_tick(db, monkeypatch):
    async def boom(db):
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(dh_module, "deploy_health", boom)
    await loop._check_deploy_staleness(db)  # must not raise
    assert await _rows(db) == []
