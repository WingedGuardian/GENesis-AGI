"""Awareness posture check + job-wiring seam tests."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import memory_integrity as mi
from genesis.db.schema._tables import TABLES

pytestmark = pytest.mark.asyncio

_SOURCE = "memory_integrity_posture_monitor"


@pytest.fixture(autouse=True)
def _reset_posture_globals():
    """The posture cooldown state is module-global; reset it between tests so a
    same-key alert in one test doesn't suppress the next. (The crud table-cache
    is reset by the package conftest autouse fixture.)"""
    from genesis.awareness import loop

    loop._last_memory_integrity_alert_at = 0.0
    loop._last_memory_integrity_key = ""
    yield


async def _setup() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute(TABLES["observations"])
    await db.execute(TABLES["memory_consistency_reports"])
    await db.execute(TABLES["recall_probe_runs"])
    await db.commit()
    mi._tables_verified = False
    return db


async def _alerts(db) -> list[dict]:
    async with db.execute(
        "SELECT id, priority, resolved, content FROM observations "
        "WHERE source = ? AND type = 'infrastructure_alert'",
        (_SOURCE,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


def _open(alerts):
    return [a for a in alerts if a["resolved"] == 0]


# ── posture ──


async def test_no_rows_is_silent(monkeypatch):
    from genesis.awareness import loop

    db = await _setup()
    await loop._check_memory_integrity_posture(db)
    assert _open(await _alerts(db)) == []
    await db.close()


async def test_degraded_consistency_raises_alert(monkeypatch):
    from genesis.awareness import loop

    db = await _setup()
    await mi.insert_consistency_report(
        db,
        status="degraded",
        counts={
            "lying_mirror": 21,
            "ghost_points": 274,
            "fts_ghosts": 0,
            "fts_invisible": 0,
            "unexpected_vector": 0,
            "deprecated_divergence": 0,
        },
        total_rows=29699,
        sampled_rows=29699,
        sample_fraction=1.0,
        truncated=False,
    )
    await loop._check_memory_integrity_posture(db)
    opn = _open(await _alerts(db))
    assert len(opn) == 1
    assert "lying_mirror=21" in opn[0]["content"]
    await db.close()


async def test_healthy_resolves(monkeypatch):
    from genesis.awareness import loop

    db = await _setup()
    # first a degraded row → alert
    await mi.insert_consistency_report(
        db,
        status="degraded",
        counts={"lying_mirror": 9},
        total_rows=100,
        sampled_rows=100,
        sample_fraction=1.0,
        truncated=False,
    )
    await loop._check_memory_integrity_posture(db)
    assert len(_open(await _alerts(db))) == 1
    # now a healthy row supersedes → resolved
    await mi.insert_consistency_report(
        db,
        status="healthy",
        counts={"lying_mirror": 0},
        total_rows=100,
        sampled_rows=100,
        sample_fraction=1.0,
        truncated=False,
    )
    await loop._check_memory_integrity_posture(db)
    assert _open(await _alerts(db)) == []
    await db.close()


async def test_single_unknown_run_does_not_alert(monkeypatch):
    from genesis.awareness import loop

    db = await _setup()
    await mi.insert_consistency_report(
        db,
        status="unknown",
        counts={},
        total_rows=100,
        sampled_rows=0,
        sample_fraction=1.0,
        truncated=False,
        unknown_reason="qdrant_unavailable",
    )
    await loop._check_memory_integrity_posture(db)
    assert _open(await _alerts(db)) == []
    await db.close()


async def test_stuck_unknown_streak_alerts(monkeypatch):
    """A checker running LONGER than the window with only unknown reports fires
    the staleness finding; a fresh install with one unknown does not."""
    from genesis.awareness import loop

    db = await _setup()
    # earliest report is 10 days old, all unknown → stuck
    await mi.insert_consistency_report(
        db,
        status="unknown",
        counts={},
        total_rows=100,
        sampled_rows=0,
        sample_fraction=1.0,
        truncated=False,
        unknown_reason="qdrant_unavailable",
        created_at="2026-07-15 03:50:00",
    )
    await mi.insert_consistency_report(
        db,
        status="unknown",
        counts={},
        total_rows=100,
        sampled_rows=0,
        sample_fraction=1.0,
        truncated=False,
        unknown_reason="qdrant_unavailable",
        created_at="2026-07-25 03:50:00",
    )
    await loop._check_memory_integrity_posture(db)
    opn = _open(await _alerts(db))
    assert len(opn) == 1
    assert "no conclusive" in opn[0]["content"]
    await db.close()


async def test_unknown_after_degraded_holds_alert(monkeypatch):
    """A degraded report followed by an inconclusive 'unknown' run must NOT
    resolve the alert — recovery was never demonstrated (P1)."""
    from genesis.awareness import loop

    db = await _setup()
    await mi.insert_consistency_report(
        db,
        status="degraded",
        counts={"lying_mirror": 9},
        total_rows=100,
        sampled_rows=100,
        sample_fraction=1.0,
        truncated=False,
        created_at="2026-07-25 03:50:00",
    )
    await loop._check_memory_integrity_posture(db)
    assert len(_open(await _alerts(db))) == 1
    # Qdrant goes down → next run is unknown. Alert must persist.
    await mi.insert_consistency_report(
        db,
        status="unknown",
        counts={},
        total_rows=100,
        sampled_rows=0,
        sample_fraction=1.0,
        truncated=False,
        unknown_reason="qdrant_unavailable",
        created_at="2026-07-25 03:55:00",
    )
    await loop._check_memory_integrity_posture(db)
    assert len(_open(await _alerts(db))) == 1  # held, not resolved
    # A genuinely healthy run DOES resolve it.
    await mi.insert_consistency_report(
        db,
        status="healthy",
        counts={},
        total_rows=100,
        sampled_rows=100,
        sample_fraction=1.0,
        truncated=False,
        created_at="2026-07-25 04:00:00",
    )
    await loop._check_memory_integrity_posture(db)
    assert _open(await _alerts(db)) == []
    await db.close()


async def test_stuck_recall_probe_alerts_but_unseeded_does_not(monkeypatch):
    """A recall probe wedged on retriever errors past the window escalates; an
    unseeded golden set (golden_set_too_small) never does (P1 + exemption)."""
    from genesis.awareness import loop

    db = await _setup()
    # probe running 10d, all unknown due to a real retriever error → stale
    for ts in ("2026-07-15 03:20:00", "2026-07-25 03:20:00"):
        await mi.insert_recall_probe_run(
            db,
            status="unknown",
            probes_total=5,
            probes_hit=0,
            hit_rate=None,
            mean_rr=None,
            unknown_reason="recall_error: ConnectionError",
            created_at=ts,
        )
    await loop._check_memory_integrity_posture(db)
    opn = _open(await _alerts(db))
    assert len(opn) == 1 and "recall-health probe" in opn[0]["content"]
    await db.close()

    # Contrast: an unseeded golden set (golden_set_too_small) must NOT alert.
    db2 = await _setup()
    for ts in ("2026-07-15 03:20:00", "2026-07-25 03:20:00"):
        await mi.insert_recall_probe_run(
            db2,
            status="unknown",
            probes_total=0,
            probes_hit=0,
            hit_rate=None,
            mean_rr=None,
            unknown_reason="golden_set_too_small",
            created_at=ts,
        )
    await loop._check_memory_integrity_posture(db2)
    assert _open(await _alerts(db2)) == []
    await db2.close()


# ── job wiring ──


async def test_jobs_registered_on_scheduler():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from genesis.runtime.init.memory_integrity import _wire_memory_integrity_jobs

    class _RT:
        _db = object()
        _hybrid_retriever = None

    sched = AsyncIOScheduler()
    _wire_memory_integrity_jobs(sched, _RT())
    ids = {j.id for j in sched.get_jobs()}
    assert {"recall_health_probe", "memory_consistency_check", "memory_integrity_prune"} <= ids
    for j in sched.get_jobs():
        assert j.max_instances == 1
