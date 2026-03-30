"""Tests for HealthDataService."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.observability.health_data import HealthDataService
from genesis.routing.types import (
    CallSiteConfig,
    ProviderConfig,
    ProviderState,
    RetryPolicy,
    RoutingConfig,
)


def _make_provider(name: str, *, free: bool = True) -> ProviderConfig:
    return ProviderConfig(
        name=name, provider_type="test", model_id="test-model",
        is_free=free, rpm_limit=None, open_duration_s=120,
    )


def _make_config(
    providers: dict[str, ProviderConfig],
    call_sites: dict[str, CallSiteConfig],
) -> RoutingConfig:
    return RoutingConfig(
        providers=providers,
        call_sites=call_sites,
        retry_profiles={"default": RetryPolicy()},
    )


def _mock_breaker(state: ProviderState = ProviderState.CLOSED, failures: int = 0, trips: int = 0):
    cb = MagicMock()
    cb.state = state
    cb.consecutive_failures = failures
    cb.trip_count = trips
    return cb


def _mock_registry(breakers: dict[str, MagicMock]):
    registry = MagicMock()
    registry.get.side_effect = lambda name: breakers.get(name, _mock_breaker())
    return registry


class TestCallSiteStatus:
    """Test healthy/degraded/down derivation."""

    def _service(self, chain: list[str], breaker_states: dict[str, ProviderState]):
        providers = {n: _make_provider(n) for n in chain}
        config = _make_config(providers, {
            "test_site": CallSiteConfig(id="test_site", chain=chain),
        })
        breakers_map = {n: _mock_breaker(s) for n, s in breaker_states.items()}
        registry = _mock_registry(breakers_map)
        return HealthDataService(circuit_breakers=registry, routing_config=config)

    @pytest.mark.asyncio
    async def test_healthy_when_first_provider_closed(self):
        svc = self._service(["a", "b"], {"a": ProviderState.CLOSED, "b": ProviderState.CLOSED})
        snap = await svc.snapshot()
        assert snap["call_sites"]["test_site"]["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_degraded_when_first_open_but_fallback_closed(self):
        svc = self._service(["a", "b"], {"a": ProviderState.OPEN, "b": ProviderState.CLOSED})
        snap = await svc.snapshot()
        assert snap["call_sites"]["test_site"]["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_down_when_all_open(self):
        svc = self._service(["a", "b"], {"a": ProviderState.OPEN, "b": ProviderState.OPEN})
        snap = await svc.snapshot()
        assert snap["call_sites"]["test_site"]["status"] == "down"

    @pytest.mark.asyncio
    async def test_active_provider_is_first_closed(self):
        svc = self._service(["a", "b", "c"], {
            "a": ProviderState.OPEN,
            "b": ProviderState.CLOSED,
            "c": ProviderState.CLOSED,
        })
        snap = await svc.snapshot()
        assert snap["call_sites"]["test_site"]["active_provider"] == "b"

    @pytest.mark.asyncio
    async def test_chain_health_includes_failures(self):
        svc = self._service(["a"], {"a": ProviderState.CLOSED})
        # Override to include failures
        breakers = {"a": _mock_breaker(ProviderState.CLOSED, failures=2)}
        svc._breakers = _mock_registry(breakers)
        snap = await svc.snapshot()
        chain = snap["call_sites"]["test_site"]["chain_health"]
        assert chain[0]["failures"] == 2


class TestGracefulDegradation:
    """Test that None dependencies produce valid output."""

    @pytest.mark.asyncio
    async def test_all_none_produces_valid_snapshot(self):
        svc = HealthDataService()
        snap = await svc.snapshot()
        assert "timestamp" in snap
        assert snap["call_sites"] == {}
        assert snap["queues"]["deferred_work"] == 0
        assert snap["cost"]["budget_status"] == "unknown"

    @pytest.mark.asyncio
    async def test_no_breakers_returns_empty_call_sites(self):
        config = _make_config(
            {"a": _make_provider("a")},
            {"s": CallSiteConfig(id="s", chain=["a"])},
        )
        svc = HealthDataService(routing_config=config)
        snap = await svc.snapshot()
        assert snap["call_sites"] == {}

    @pytest.mark.asyncio
    async def test_no_db_returns_unknown_cc_sessions(self):
        svc = HealthDataService()
        snap = await svc.snapshot()
        assert snap["cc_sessions"]["foreground"]["status"] == "unknown"


class TestQueues:
    @pytest.mark.asyncio
    async def test_queue_depths_from_subsystems(self):
        deferred = AsyncMock()
        deferred.count_pending = AsyncMock(return_value=5)
        dead_letter = AsyncMock()
        dead_letter.get_pending_count = AsyncMock(return_value=2)

        svc = HealthDataService(deferred_queue=deferred, dead_letter=dead_letter)
        snap = await svc.snapshot()
        assert snap["queues"]["deferred_work"] == 5
        assert snap["queues"]["dead_letters"] == 2


class TestCost:
    @pytest.mark.asyncio
    async def test_cost_from_tracker(self):
        tracker = AsyncMock()
        tracker.get_period_cost = AsyncMock(side_effect=lambda p: 0.42 if p == "today" else 8.70)
        tracker.check_budget = AsyncMock(return_value="under_limit")

        svc = HealthDataService(cost_tracker=tracker)
        snap = await svc.snapshot()
        assert snap["cost"]["daily_usd"] == 0.42
        assert snap["cost"]["monthly_usd"] == 8.70


class TestCCSessions:
    @pytest.mark.asyncio
    async def test_cc_sessions_from_db(self):
        db = AsyncMock()
        cursor = AsyncMock()
        cursor.fetchall = AsyncMock(return_value=[("foreground", 1), ("background", 3)])
        db.execute = AsyncMock(return_value=cursor)

        svc = HealthDataService(db=db)
        snap = await svc.snapshot()
        assert snap["cc_sessions"]["foreground"]["active"] == 1
        assert snap["cc_sessions"]["background"]["active"] == 3


class TestInfrastructure:
    @pytest.mark.asyncio
    async def test_db_probe(self):
        db = AsyncMock()
        cursor = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=(1,))
        db.execute = AsyncMock(return_value=cursor)

        svc = HealthDataService(db=db)
        snap = await svc.snapshot()
        assert snap["infrastructure"]["genesis.db"]["status"] in ("healthy", "down", "unknown")

    @pytest.mark.asyncio
    async def test_no_scheduler_no_db_returns_unknown(self):
        svc = HealthDataService()
        snap = await svc.snapshot()
        assert snap["infrastructure"]["scheduler"]["status"] == "unknown"

    @pytest.mark.asyncio
    async def test_scheduler_db_fallback_healthy(self):
        """When scheduler is None but DB has recent job activity, report healthy."""
        from datetime import UTC, datetime

        db = AsyncMock()
        cursor = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=(datetime.now(UTC).isoformat(),))
        db.execute = AsyncMock(return_value=cursor)
        svc = HealthDataService(db=db)
        snap = await svc.snapshot()
        assert snap["infrastructure"]["scheduler"]["status"] == "healthy"


class TestCallSiteTripCount:
    """Verify trip_count is exposed in chain_health entries."""

    @pytest.mark.asyncio
    async def test_trip_count_included_when_positive(self):
        providers = {"a": _make_provider("a")}
        config = _make_config(providers, {
            "test_site": CallSiteConfig(id="test_site", chain=["a"]),
        })
        breakers_map = {"a": _mock_breaker(ProviderState.OPEN, failures=0, trips=3)}
        registry = _mock_registry(breakers_map)
        svc = HealthDataService(circuit_breakers=registry, routing_config=config)
        snap = await svc.snapshot()
        chain = snap["call_sites"]["test_site"]["chain_health"]
        assert chain[0]["trip_count"] == 3

    @pytest.mark.asyncio
    async def test_trip_count_omitted_when_zero(self):
        providers = {"a": _make_provider("a")}
        config = _make_config(providers, {
            "test_site": CallSiteConfig(id="test_site", chain=["a"]),
        })
        breakers_map = {"a": _mock_breaker(ProviderState.CLOSED, failures=0, trips=0)}
        registry = _mock_registry(breakers_map)
        svc = HealthDataService(circuit_breakers=registry, routing_config=config)
        snap = await svc.snapshot()
        chain = snap["call_sites"]["test_site"]["chain_health"]
        assert "trip_count" not in chain[0]
