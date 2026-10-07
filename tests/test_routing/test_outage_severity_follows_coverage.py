"""A dead provider pages (critical) only when an essential call site it serves
is left with no available provider; while fallback covers every essential site
its notice is "high" — dashboard and morning report, never Telegram.

MEASURED 2026-10-07 on a live install: the NVIDIA NIM DeepSeek and Gemini
outages each paged critical, and neither left any essential site uncovered.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from genesis.db.crud import observations as obs_crud
from genesis.routing.escalation import ProviderEscalation, notify_provider_if_due

pytestmark = pytest.mark.asyncio


async def _due_outage(db, provider="prov-x", hours=2):
    started = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    await obs_crud.create(
        db,
        id=f"fail-{provider}",
        person_id=None,
        type="provider_failure",
        content=json.dumps({"provider": provider, "first_trip_at": started}),
        source="routing",
        priority="high",
        content_hash=ProviderEscalation._provider_content_hash(provider),
        created_at=started,
    )


async def _notice(db, provider="prov-x"):
    rows = await obs_crud.unresolved_by_hash(
        db,
        source="routing",
        content_hash=ProviderEscalation._notify_content_hash(provider, None),
    )
    assert len(rows) == 1
    return rows[0], json.loads(rows[0]["content"])["message"]


async def _notify(db, coverage_for, priority="critical", provider="prov-x"):
    return await notify_provider_if_due(
        db,
        provider,
        priority=priority,
        provider_still_failing=lambda p: True,
        coverage_for=coverage_for,
    )


async def test_covered_outage_is_high_and_says_fallback_works(empty_db):
    await _due_outage(empty_db)
    assert await _notify(empty_db, lambda p: []) is True
    row, msg = await _notice(empty_db)
    assert row["priority"] == "high"
    assert "every essential call site still has an available provider" in msg


async def test_uncovered_essential_site_pages_and_names_it(empty_db):
    await _due_outage(empty_db)
    assert await _notify(empty_db, lambda p: ["9_fact_extraction"]) is True
    row, msg = await _notice(empty_db)
    assert row["priority"] == "critical"
    assert "9_fact_extraction" in msg
    assert "falling back" not in msg


async def test_unknown_coverage_keeps_the_page(empty_db):
    await _due_outage(empty_db)
    assert await _notify(empty_db, lambda p: None) is True
    row, _ = await _notice(empty_db)
    assert row["priority"] == "critical"


async def test_a_coverage_error_keeps_the_page(empty_db):
    def boom(_p):
        raise RuntimeError("registry gone")

    await _due_outage(empty_db)
    assert await _notify(empty_db, boom) is True
    row, _ = await _notice(empty_db)
    assert row["priority"] == "critical"


async def test_no_coverage_callback_is_the_old_behaviour(empty_db):
    await _due_outage(empty_db)
    assert await _notify(empty_db, None) is True
    row, _ = await _notice(empty_db)
    assert row["priority"] == "critical"


async def test_the_lever_still_caps_an_uncovered_outage(empty_db):
    """propose_only (priority="high") is an upper bound coverage never raises."""
    await _due_outage(empty_db)
    assert await _notify(empty_db, lambda p: ["3_micro_reflection"], priority="high") is True
    row, _ = await _notice(empty_db)
    assert row["priority"] == "high"


async def test_coverage_is_read_under_the_current_provider_name(empty_db):
    seen = []

    def cov(name):
        seen.append(name)
        return []

    await _due_outage(empty_db, provider="old-name")
    await notify_provider_if_due(
        empty_db,
        "old-name",
        provider_still_failing=lambda p: True,
        incident_owner=lambda p, i: "new-name",
        coverage_for=cov,
    )
    assert seen == ["new-name"]


# ── registry accessor ──────────────────────────────────────────────────────


def _registry(essential):
    from genesis.routing.circuit_breaker import CircuitBreakerRegistry
    from genesis.routing.types import ProviderConfig

    providers = {
        n: ProviderConfig(
            name=n,
            provider_type="groq",
            model_id="m",
            is_free=True,
            rpm_limit=None,
            open_duration_s=120,
            has_api_key=True,
        )
        for n in ("a", "b", "c")
    }
    return CircuitBreakerRegistry(providers, persist=False, essential_sites=essential)


def _trip(reg, name):
    from genesis.routing.types import ErrorCategory

    cb = reg.get(name)
    for _ in range(10):
        cb.record_failure(ErrorCategory.TRANSIENT)


async def test_registry_reports_only_sites_the_provider_serves():
    reg = _registry({"site1": ["a", "b"], "site2": ["c"]})
    _trip(reg, "a")
    assert reg.uncovered_essential_sites_for("a") == [], "b still covers site1"
    _trip(reg, "b")
    assert reg.uncovered_essential_sites_for("a") == ["site1"]
    assert reg.uncovered_essential_sites_for("c") == ["site2"], "no OTHER provider serves site2"


async def test_registry_without_an_essential_map_is_unknown():
    reg = _registry(None)
    assert reg.uncovered_essential_sites_for("a") is None


async def test_a_dead_provider_never_covers_its_own_site():
    """Once its OPEN window expires the breaker reads HALF_OPEN ("available" to
    the router); it must still not count as cover for its own outage."""
    reg = _registry({"site1": ["a"]})
    _trip(reg, "a")
    assert reg.uncovered_essential_sites_for("a") == ["site1"]
    reg.get("a")._opened_at -= 10**6  # backoff expired -> HALF_OPEN
    assert reg.get("a").is_available()
    assert reg.uncovered_essential_sites_for("a") == ["site1"]


async def test_a_deselected_provider_covers_nothing():
    reg = _registry({"site1": ["a", "b"]})
    _trip(reg, "a")
    assert reg.uncovered_essential_sites_for("a") == []
    assert reg.uncovered_essential_sites_for("a", also_unavailable=lambda n: n == "b") == [
        "site1"
    ]


async def test_an_empty_essential_map_means_nothing_is_essential():
    assert _registry({}).uncovered_essential_sites_for("a") == []


async def test_an_acknowledged_covered_notice_never_blocks_the_page(empty_db):
    """The user acks the "fallback works" notice; later the last cover dies too.
    That loss of coverage must still page."""
    await _due_outage(empty_db)
    assert await _notify(empty_db, lambda p: []) is True
    row, _ = await _notice(empty_db)
    await obs_crud.resolve_batch(
        empty_db, [row["id"]], resolved_at=datetime.now(UTC).isoformat(),
        resolution_notes="acknowledged on the dashboard",
    )
    assert await _notify(empty_db, lambda p: ["9_fact_extraction"]) is True
    row, _ = await _notice(empty_db)
    assert row["priority"] == "critical"


async def test_an_acknowledged_page_still_suppresses_a_repeat(empty_db):
    """Control for the test above: acking a critical page keeps the
    once-per-outage contract."""
    await _due_outage(empty_db)
    assert await _notify(empty_db, lambda p: ["9_fact_extraction"]) is True
    row, _ = await _notice(empty_db)
    await obs_crud.resolve_batch(
        empty_db, [row["id"]], resolved_at=datetime.now(UTC).isoformat(),
        resolution_notes="acknowledged on the dashboard",
    )
    assert await _notify(empty_db, lambda p: ["9_fact_extraction"]) is False
