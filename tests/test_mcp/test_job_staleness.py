"""Tests for silent-job-staleness surfacing.

Two surfaces:
  - ``_impl_health_alerts`` emits a ``job_stale:<name>`` WARNING when a job's
    ``last_run`` is more than the threshold ahead of its ``last_success``
    (running but not succeeding). Exercised against a REAL in-memory SQLite DB
    so the ``julianday`` gap math + WHERE threshold are tested, not mocked away.
  - ``_annotate_staleness`` (job_health MCP output) adds ``days_since_success``
    and ``stale`` per job without mutating the source dict.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

# Fixed timestamps so the last_run − last_success gap is deterministic
# (julianday works on the stored strings, not on "now").
_RUN = "2026-06-28T00:00:00+00:00"
_OK_STALE = "2026-06-14T00:00:00+00:00"   # 14.0 days before _RUN  → alerts
_OK_RECENT = "2026-06-25T00:00:00+00:00"  # 3.0 days before _RUN   → below threshold
_OK_HEALTHY = _RUN                          # 0.0 days               → healthy


async def _make_service_with_jobs(rows):
    """Build a mock HealthDataService backed by a real in-memory job_health DB.

    ``rows`` = list of (job_name, last_run, last_success). Only the columns the
    staleness query touches are created; other alert blocks that query missing
    tables fail gracefully inside their own try/except.
    """
    db = await aiosqlite.connect(":memory:")
    # Match production: get_db() sets row_factory=aiosqlite.Row, so rows are
    # Row objects (name-based access), never plain tuples. Exercise that path.
    db.row_factory = aiosqlite.Row
    await db.execute(
        "CREATE TABLE job_health ("
        "job_name TEXT PRIMARY KEY, last_run TEXT, last_success TEXT, "
        "last_failure TEXT, last_error TEXT, consecutive_failures INTEGER DEFAULT 0)"
    )
    await db.executemany(
        "INSERT INTO job_health (job_name, last_run, last_success) VALUES (?, ?, ?)",
        rows,
    )
    await db.commit()

    svc = MagicMock()
    svc._db = db
    svc.snapshot = AsyncMock(return_value={
        "call_sites": {}, "infrastructure": {}, "cc_sessions": {},
        "queues": {}, "awareness": {}, "services": {},
    })
    return svc


async def _run_alerts(svc):
    with patch("genesis.mcp.health_mcp._service", svc), \
         patch("genesis.mcp.health_mcp._activity_tracker", None), \
         patch("genesis.mcp.health_mcp._job_retry_registry", None), \
         patch("genesis.mcp.health_mcp._alert_history", {}):
        from genesis.mcp.health.errors import _impl_health_alerts
        return await _impl_health_alerts(active_only=True)


@pytest.mark.asyncio
async def test_stale_job_alerts():
    """A job running-but-not-succeeding past the threshold emits one WARNING."""
    svc = await _make_service_with_jobs([("weekly_assessment", _RUN, _OK_STALE)])
    try:
        alerts = await _run_alerts(svc)
    finally:
        await svc._db.close()

    stale = [a for a in alerts if a.get("id", "").startswith("job_stale:")]
    assert len(stale) == 1
    a = stale[0]
    assert a["id"] == "job_stale:weekly_assessment"
    assert a["severity"] == "WARNING"
    assert "weekly_assessment" in a["message"]
    assert "14" in a["message"]          # gap days
    assert "2026-06-14" in a["message"]  # last success date


@pytest.mark.asyncio
async def test_healthy_and_recent_jobs_do_not_alert():
    """gap==0 (healthy) and gap<threshold (single recent miss) stay silent."""
    svc = await _make_service_with_jobs([
        ("healthy_job", _RUN, _OK_HEALTHY),   # gap 0
        ("recent_miss", _RUN, _OK_RECENT),    # gap 3 < 6.0
    ])
    try:
        alerts = await _run_alerts(svc)
    finally:
        await svc._db.close()

    stale = [a for a in alerts if a.get("id", "").startswith("job_stale:")]
    assert stale == []


@pytest.mark.asyncio
async def test_never_succeeded_job_is_not_flagged_here():
    """A job with last_success NULL is excluded (liveness signal's domain)."""
    svc = await _make_service_with_jobs([("never_ok", _RUN, None)])
    try:
        alerts = await _run_alerts(svc)
    finally:
        await svc._db.close()

    stale = [a for a in alerts if a.get("id", "").startswith("job_stale:")]
    assert stale == []


@pytest.mark.asyncio
async def test_multiple_stale_jobs_sorted_worst_first():
    """Every stale job gets its own alert; query orders by widest gap first."""
    svc = await _make_service_with_jobs([
        ("weekly_calibration", _RUN, "2026-06-20T00:00:00+00:00"),  # 8 days
        ("weekly_assessment", _RUN, _OK_STALE),                     # 14 days
    ])
    try:
        alerts = await _run_alerts(svc)
    finally:
        await svc._db.close()

    stale = [a for a in alerts if a.get("id", "").startswith("job_stale:")]
    ids = [a["id"] for a in stale]
    assert ids == ["job_stale:weekly_assessment", "job_stale:weekly_calibration"]


# ── _annotate_staleness (job_health MCP output field) ────────────────────────

def test_annotate_staleness_computes_gap_and_flag():
    from genesis.mcp.health.manifest import _annotate_staleness

    out = _annotate_staleness({
        "stale_job": {"last_run": _RUN, "last_success": _OK_STALE, "consecutive_failures": 0},
        "healthy_job": {"last_run": _RUN, "last_success": _OK_HEALTHY},
    })
    assert out["stale_job"]["days_since_success"] == 14.0
    assert out["stale_job"]["stale"] is True
    assert out["healthy_job"]["days_since_success"] == 0.0
    assert out["healthy_job"]["stale"] is False
    # existing fields preserved
    assert out["stale_job"]["consecutive_failures"] == 0


def test_annotate_staleness_handles_missing_and_bad_timestamps():
    from genesis.mcp.health.manifest import _annotate_staleness

    out = _annotate_staleness({
        "no_success": {"last_run": _RUN, "last_success": None},
        "no_run": {"last_run": None, "last_success": _OK_STALE},
        "garbage": {"last_run": "not-a-date", "last_success": _OK_STALE},
    })
    for name in ("no_success", "no_run", "garbage"):
        assert out[name]["days_since_success"] is None
        assert out[name]["stale"] is False


def test_annotate_staleness_handles_mixed_tz_awareness():
    """A naive last_run vs an aware last_success must not silently become None."""
    from genesis.mcp.health.manifest import _annotate_staleness

    out = _annotate_staleness({
        # last_run naive, last_success offset-aware — both UTC in Genesis
        "mixed": {"last_run": "2026-06-28T00:00:00", "last_success": _OK_STALE},
    })
    assert out["mixed"]["days_since_success"] == 14.0
    assert out["mixed"]["stale"] is True


def test_annotate_staleness_does_not_mutate_source():
    from genesis.mcp.health.manifest import _annotate_staleness

    source = {"j": {"last_run": _RUN, "last_success": _OK_STALE}}
    _annotate_staleness(source)
    assert "days_since_success" not in source["j"]
    assert "stale" not in source["j"]
