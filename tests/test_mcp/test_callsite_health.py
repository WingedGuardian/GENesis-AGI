"""Tests for the unified critical call-site health detector (Part B/C).

The detector reads ``call_site_last_run`` and emits ``callsite:down:<id>`` for
any site whose LAST run failed within the recency window: CRITICAL/red for a
site in ``CRITICAL_CALL_SITES``, WARNING/yellow otherwise. It is dashboard-only
— never escalation-whitelisted (no Telegram), never Sentinel-mapped (no
firefighter).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_service(callsite_rows):
    """Mock HealthDataService whose call_site_last_run query returns
    ``callsite_rows`` (list of ``(call_site_id, last_run_at, provider_used)``).

    The credit-exhaustion and stale-job queries that also run in
    ``_impl_health_alerts`` return empty, so only the call-site block produces
    alerts here.
    """
    svc = MagicMock()
    svc._db = AsyncMock()
    svc.snapshot = AsyncMock(
        return_value={
            "call_sites": {},
            "infrastructure": {},
            "cc_sessions": {},
            "resilience": {"level": "L0"},
            "queues": {},
        }
    )

    callsite_cursor = AsyncMock()
    callsite_cursor.fetchall = AsyncMock(return_value=callsite_rows)

    empty_cursor = AsyncMock()
    empty_cursor.fetchall = AsyncMock(return_value=[])
    empty_cursor.fetchone = AsyncMock(return_value=None)

    async def mock_execute(query, params=None):
        if "FROM call_site_last_run" in query:
            return callsite_cursor
        return empty_cursor

    svc._db.execute = mock_execute
    return svc


_PATCHES = dict(
    _activity_tracker=None,
    _job_retry_registry=None,
    _alert_history={},
)


async def _run(svc):
    with (
        patch("genesis.mcp.health_mcp._service", svc),
        patch("genesis.mcp.health_mcp._activity_tracker", None),
        patch("genesis.mcp.health_mcp._job_retry_registry", None),
        patch("genesis.mcp.health_mcp._alert_history", {}),
    ):
        from genesis.mcp.health.errors import _impl_health_alerts

        return await _impl_health_alerts(active_only=True)


@pytest.mark.asyncio
async def test_critical_site_down_is_critical():
    """A failing site in CRITICAL_CALL_SITES → CRITICAL/red."""
    svc = _make_service([("21_embeddings", "2026-07-12T10:00:00", "embedding")])
    alerts = await _run(svc)
    down = [a for a in alerts if a["id"] == "callsite:down:21_embeddings"]
    assert len(down) == 1
    assert down[0]["severity"] == "CRITICAL"


@pytest.mark.asyncio
async def test_noncritical_site_down_is_warning():
    """A failing site NOT in the critical set → WARNING/yellow (watched)."""
    svc = _make_service([("30_triage_calibration", "2026-07-12T10:00:00", "mistral")])
    alerts = await _run(svc)
    down = [a for a in alerts if a["id"] == "callsite:down:30_triage_calibration"]
    assert len(down) == 1
    assert down[0]["severity"] == "WARNING"


@pytest.mark.asyncio
async def test_autonomous_executor_is_warning_not_critical():
    """autonomous_executor_reasoning is deliberately yellow (has CC fallback)."""
    svc = _make_service([("autonomous_executor_reasoning", "2026-07-12T10:00:00", "openrouter")])
    alerts = await _run(svc)
    down = [a for a in alerts if a["id"] == "callsite:down:autonomous_executor_reasoning"]
    assert len(down) == 1
    assert down[0]["severity"] == "WARNING"


@pytest.mark.asyncio
async def test_no_failing_rows_no_alerts():
    """No failing rows (all recovered / none stale) → no callsite:down alerts."""
    svc = _make_service([])
    alerts = await _run(svc)
    assert [a for a in alerts if a["id"].startswith("callsite:down:")] == []


@pytest.mark.asyncio
async def test_recency_and_success_filter_via_real_db():
    """SQL correctness against a REAL sqlite DB: only rows that are BOTH
    failed (success=0) AND recent (within the window) surface; recovered rows
    and stale-failed rows are excluded. Proves the WHERE clause, not a mock."""
    import aiosqlite

    from genesis.mcp.health.critical_sites import CALLSITE_DOWN_RECENCY_HOURS

    now = datetime.now(UTC)
    fresh = (now - timedelta(hours=1)).isoformat()
    stale = (now - timedelta(hours=CALLSITE_DOWN_RECENCY_HOURS + 48)).isoformat()

    async with aiosqlite.connect(":memory:") as db:
        await db.execute(
            "CREATE TABLE call_site_last_run ("
            "call_site_id TEXT PRIMARY KEY, last_run_at TEXT, "
            "provider_used TEXT, success INTEGER)"
        )
        await db.executemany(
            "INSERT INTO call_site_last_run "
            "(call_site_id, last_run_at, provider_used, success) VALUES (?,?,?,?)",
            [
                ("21_embeddings", fresh, "embedding", 0),  # fresh fail, critical
                ("30_triage_calibration", fresh, "mistral", 0),  # fresh fail, non-crit
                ("5_deep_reflection", stale, "cc", 0),  # STALE fail → excluded
                ("9_fact_extraction", fresh, "groq", 1),  # recovered → excluded
            ],
        )
        await db.commit()

        svc = MagicMock()
        svc._db = db
        svc.snapshot = AsyncMock(
            return_value={
                "call_sites": {},
                "infrastructure": {},
                "cc_sessions": {},
                "resilience": {"level": "L0"},
                "queues": {},
            }
        )
        alerts = await _run(svc)

    down = {a["id"]: a["severity"] for a in alerts if a["id"].startswith("callsite:down:")}
    assert down == {
        "callsite:down:21_embeddings": "CRITICAL",
        "callsite:down:30_triage_calibration": "WARNING",
    }


def test_callsite_down_not_escalation_whitelisted():
    """callsite:down: must never match an outreach escalation whitelist entry
    (prefix match) → no Telegram, ever."""
    from genesis.outreach.config import _DEFAULTS

    sample = "callsite:down:21_embeddings"
    assert not any(sample.startswith(entry) for entry in _DEFAULTS.immediate_escalation_alerts)


def test_callsite_down_unmapped_in_sentinel():
    """callsite:down: must be UNMAPPED (fail-closed) → never wakes the Sentinel,
    and explicitly listed in UNMAPPED_BY_DESIGN so the coverage lock passes."""
    from genesis.sentinel.remediation_map import (
        UNMAPPED_BY_DESIGN,
        required_tools,
    )

    assert "callsite:down:" in UNMAPPED_BY_DESIGN
    assert required_tools("callsite:down:21_embeddings") is None


def test_critical_call_sites_membership():
    """Part C: the curated red set — the 12 locked ids present, the deliberately
    excluded ones absent."""
    from genesis.mcp.health.critical_sites import CRITICAL_CALL_SITES

    expected = {
        "9_fact_extraction",
        "40_knowledge_distillation",
        "38_procedure_extraction",
        "21_embeddings",
        "21b_query_embedding",
        "ambient_arbiter",
        "7_genesis_ego_cycle",
        "7_user_ego_cycle",
        "40_ego_focus_selection",
        "5_deep_reflection",
        "6_strategic_reflection",
    }
    assert expected == CRITICAL_CALL_SITES
    # Deliberately NOT red. autonomous_executor_reasoning = watched-yellow (CC
    # fallback); 3_micro_reflection/judge = routine/eval-only; 8_ego_compaction =
    # DEAD (ego went ephemeral, never records a last_run row — can never fire).
    for excluded in (
        "autonomous_executor_reasoning",
        "3_micro_reflection",
        "judge",
        "8_ego_compaction",
    ):
        assert excluded not in CRITICAL_CALL_SITES


def test_credit_exhaustion_removed_from_escalation_default():
    """Part A: credit exhaustion is dashboard-only WARNING → removed from the
    code escalation default (WARNING can never page anyway; kept out for
    intent-clarity)."""
    from genesis.outreach.config import _DEFAULTS

    assert "provider:credit_exhaustion" not in _DEFAULTS.immediate_escalation_alerts


def test_health_data_uninitialized_escalates_in_default_and_repo_yaml():
    """Part E: a blind health system SHOULD page. It stays in the code default
    AND is restored to the repo outreach.yaml (which REPLACES the default)."""
    from genesis.outreach.config import (
        _DEFAULTS,
        _REPO_CONFIG,
        load_outreach_config,
    )

    assert "service:health_data_uninitialized" in _DEFAULTS.immediate_escalation_alerts
    repo_cfg = load_outreach_config(_REPO_CONFIG)
    assert "service:health_data_uninitialized" in repo_cfg.immediate_escalation_alerts
    # And credit_exhaustion stays OUT of the repo yaml (opposite treatment).
    assert "provider:credit_exhaustion" not in repo_cfg.immediate_escalation_alerts
