"""Tests for pulse-staleness (total-cessation) surfacing.

Two surfaces, one shared signal (``compute_heartbeat_staleness``):
  - ``_impl_health_alerts`` emits a ``subsystem_stale:<name>`` alert when a
    heartbeat-emitting subsystem's last durable ``heartbeat`` event is older
    than its per-subsystem overdue threshold (its scheduler/loop stopped — a
    silent death the failure-gap job alarms cannot see). ego → CRITICAL, the
    other four → WARNING. reflection is excluded (its pulse is emitted by the
    awareness loop) and awareness is excluded (it has ``awareness:tick_overdue``).
  - ``compute_heartbeat_staleness`` — the pure per-subsystem verdict the alert
    AND the ego dashboard tile both read, so the two can never disagree.

Exercised against a REAL in-memory SQLite ``events`` table so the crud query +
threshold math are tested, not mocked away.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest


def _iso_ago(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


async def _seed_heartbeat(db, subsystem: str, age_seconds: float) -> None:
    from genesis.db.crud import events as events_crud

    await events_crud.insert(
        db,
        subsystem=subsystem,
        severity="debug",
        event_type="heartbeat",
        message=f"{subsystem} pulse",
        timestamp=_iso_ago(age_seconds),
    )


# ── compute_heartbeat_staleness (the shared signal) ──────────────────────────


@pytest.mark.asyncio
async def test_staleness_overdue(db):
    """A pulse older than the subsystem's overdue threshold → status overdue."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    # ego threshold is 14400s (4h); 5h old → overdue.
    await _seed_heartbeat(db, "ego", age_seconds=5 * 3600)
    hb = await compute_heartbeat_staleness("ego", db=db)
    assert hb["status"] == "overdue"
    assert hb["age_seconds"] > 14400


@pytest.mark.asyncio
async def test_staleness_alive(db):
    """A fresh pulse well within threshold → status alive."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    await _seed_heartbeat(db, "surplus", age_seconds=10)
    hb = await compute_heartbeat_staleness("surplus", db=db)
    assert hb["status"] == "alive"


@pytest.mark.asyncio
async def test_staleness_no_heartbeat_empty_state(db):
    """No pulse ever (fresh install) → no_heartbeat, never 'stalled'."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    hb = await compute_heartbeat_staleness("ego", db=db)
    assert hb["status"] == "no_heartbeat"
    assert hb["last_seen"] is None


@pytest.mark.asyncio
async def test_staleness_unparseable_timestamp_is_unknown(db):
    """A corrupt timestamp → unknown (never silently 'alive')."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    await db.execute(
        "INSERT INTO events (id, timestamp, subsystem, severity, event_type, "
        "message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("x1", "not-a-date", "ego", "debug", "heartbeat", "bad", "not-a-date"),
    )
    await db.commit()
    hb = await compute_heartbeat_staleness("ego", db=db)
    assert hb["status"] == "unknown"


@pytest.mark.asyncio
async def test_staleness_unknown_subsystem_is_no_heartbeat(db):
    """A name not in the threshold table degrades to no_heartbeat, never raises."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    hb = await compute_heartbeat_staleness("not_a_subsystem", db=db)
    assert hb["status"] == "no_heartbeat"


@pytest.mark.asyncio
async def test_staleness_read_error_default_degrades_and_logs():
    """Default (alert/display callers): a query error is ERROR-logged and the
    verdict degrades to no_heartbeat — the read failure is recorded, and the
    verdict is never a spurious 'alive'."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    bad_db = MagicMock()
    bad_db.execute_fetchall = AsyncMock(side_effect=aiosqlite.OperationalError("db exploded"))
    with (
        patch("genesis.mcp.health_mcp._service", None),
        patch("genesis.mcp.health_mcp._event_bus", None),
    ):
        hb = await compute_heartbeat_staleness("ego", db=bad_db)
    assert hb["status"] == "no_heartbeat"


@pytest.mark.asyncio
async def test_staleness_read_error_raises_when_requested():
    """Tile caller (raise_on_error=True): a query error RE-RAISES so the caller
    fails loud (surfaces unknown) instead of a healthy tile on a broken read."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    bad_db = MagicMock()
    bad_db.execute_fetchall = AsyncMock(side_effect=aiosqlite.OperationalError("db exploded"))
    with (
        patch("genesis.mcp.health_mcp._service", None),
        patch("genesis.mcp.health_mcp._event_bus", None),
        pytest.raises(aiosqlite.OperationalError),
    ):
        await compute_heartbeat_staleness("ego", db=bad_db, raise_on_error=True)


# ── subsystem_stale alert (via _impl_health_alerts) ──────────────────────────


async def _run_alerts_with_db(db):
    """Run the real _compute_alerts path with `db` as the health service DB."""
    svc = MagicMock()
    svc._db = db
    svc.snapshot = AsyncMock(
        return_value={
            "call_sites": {},
            "infrastructure": {},
            "cc_sessions": {},
            "queues": {},
            "awareness": {},
            "services": {},
        }
    )
    with (
        patch("genesis.mcp.health_mcp._service", svc),
        patch("genesis.mcp.health_mcp._activity_tracker", None),
        patch("genesis.mcp.health_mcp._job_retry_registry", None),
        patch("genesis.mcp.health_mcp._alert_history", {}),
        patch("genesis.mcp.health_mcp._event_bus", None),
    ):
        from genesis.mcp.health.errors import _impl_health_alerts

        return await _impl_health_alerts(active_only=True)


def _stale(alerts):
    return [a for a in alerts if a.get("id", "").startswith("subsystem_stale:")]


@pytest.mark.asyncio
async def test_ego_cessation_alerts_critical(db):
    """A dead ego scheduler (heartbeat 5h old) → subsystem_stale:ego CRITICAL."""
    await _seed_heartbeat(db, "ego", age_seconds=5 * 3600)
    alerts = await _run_alerts_with_db(db)
    stale = _stale(alerts)
    assert any(a["id"] == "subsystem_stale:ego" for a in stale)
    ego = next(a for a in stale if a["id"] == "subsystem_stale:ego")
    assert ego["severity"] == "CRITICAL"
    assert "ego" in ego["message"]


@pytest.mark.asyncio
async def test_non_ego_cessation_alerts_warning(db):
    """A stalled inbox (2h old vs 1h threshold) → subsystem_stale:inbox WARNING."""
    await _seed_heartbeat(db, "inbox", age_seconds=2 * 3600)
    alerts = await _run_alerts_with_db(db)
    stale = _stale(alerts)
    inbox = next((a for a in stale if a["id"] == "subsystem_stale:inbox"), None)
    assert inbox is not None
    assert inbox["severity"] == "WARNING"


@pytest.mark.asyncio
async def test_alive_subsystem_does_not_alert(db):
    """A freshly-pulsing subsystem emits no subsystem_stale alert."""
    await _seed_heartbeat(db, "surplus", age_seconds=5)
    alerts = await _run_alerts_with_db(db)
    assert not [a for a in _stale(alerts) if a["id"] == "subsystem_stale:surplus"]


@pytest.mark.asyncio
async def test_reflection_is_excluded(db):
    """reflection is DROPPED from the alert set (its pulse tracks the awareness
    loop, not reflection-engine liveness) even when overdue."""
    await _seed_heartbeat(db, "reflection", age_seconds=5 * 3600)  # > 14400 threshold
    alerts = await _run_alerts_with_db(db)
    assert not [a for a in _stale(alerts) if a["id"] == "subsystem_stale:reflection"]


@pytest.mark.asyncio
async def test_awareness_not_duplicated(db):
    """awareness overdue is covered by awareness:tick_overdue, NOT a
    subsystem_stale:awareness duplicate."""
    await _seed_heartbeat(db, "awareness", age_seconds=5 * 3600)
    alerts = await _run_alerts_with_db(db)
    assert not [a for a in _stale(alerts) if a["id"] == "subsystem_stale:awareness"]


@pytest.mark.asyncio
async def test_empty_state_no_alerts(db):
    """Fresh install (no heartbeat events at all) → no subsystem_stale alerts."""
    alerts = await _run_alerts_with_db(db)
    assert _stale(alerts) == []


@pytest.mark.asyncio
async def test_helper_failure_isolated_from_other_alerts(db):
    """If the pulse-staleness block raises, the OTHER alerts still compute (its
    own try/except isolates it) and it is ERROR-logged, never a silent green."""
    await _seed_heartbeat(db, "ego", age_seconds=5 * 3600)
    with patch(
        "genesis.mcp.health.manifest.compute_heartbeat_staleness",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        # Should not raise; alerts list still returns (other blocks intact).
        alerts = await _run_alerts_with_db(db)
    assert _stale(alerts) == []  # block failed → no subsystem_stale alerts, but no crash


# ── heartbeat GC preserves the last pulse (durable staleness signal) ─────────


@pytest.mark.asyncio
async def test_gc_keeps_latest_heartbeat_keeps_dead_subsystem_detectable(db):
    """The 7-day heartbeat GC must KEEP the most-recent pulse per subsystem, so a
    scheduler dead LONGER than the retention window stays detectable — its last
    pulse is not pruned into a false ``no_heartbeat``/green."""
    from genesis.db.crud import events as events_crud
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    # ego died 10 days ago — its ONLY pulse is older than the 7-day GC cutoff.
    await _seed_heartbeat(db, "ego", age_seconds=10 * 86400)
    cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    await events_crud.prune(
        db, older_than=cutoff, event_type="heartbeat", keep_latest_per_subsystem=True
    )

    hb = await compute_heartbeat_staleness("ego", db=db)
    assert hb["status"] == "overdue"  # last pulse survived → still detectably dead


@pytest.mark.asyncio
async def test_gc_without_keep_latest_would_lose_the_signal(db):
    """Control: the OLD prune (keep_latest_per_subsystem=False) deletes the sole
    old pulse → the dead subsystem reverts to no_heartbeat. This is the hole the
    keep-latest fix closes; asserting it makes the fix's value explicit."""
    from genesis.db.crud import events as events_crud
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    await _seed_heartbeat(db, "ego", age_seconds=10 * 86400)
    cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    await events_crud.prune(db, older_than=cutoff, event_type="heartbeat")

    hb = await compute_heartbeat_staleness("ego", db=db)
    assert hb["status"] == "no_heartbeat"  # sole pulse pruned → signal lost (the bug)


@pytest.mark.asyncio
async def test_gc_keeps_only_newest_and_still_prunes_older(db):
    """keep-latest retains exactly the single newest pulse per subsystem; older
    pulses past the cutoff are still pruned (volume stays bounded)."""
    from genesis.db.crud import events as events_crud

    await _seed_heartbeat(db, "ego", age_seconds=12 * 86400)  # older → prunable
    await _seed_heartbeat(db, "ego", age_seconds=9 * 86400)  # newest → retained
    await _seed_heartbeat(db, "surplus", age_seconds=8 * 86400)  # sole → retained
    cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    await events_crud.prune(
        db, older_than=cutoff, event_type="heartbeat", keep_latest_per_subsystem=True
    )

    ego_rows = await events_crud.query(db, subsystem="ego", event_type="heartbeat", limit=10)
    surplus_rows = await events_crud.query(
        db, subsystem="surplus", event_type="heartbeat", limit=10
    )
    assert len(ego_rows) == 1  # only the newest ego pulse kept
    assert len(surplus_rows) == 1  # the sole surplus pulse kept
