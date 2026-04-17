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
    async def test_cc_sessions_from_db(self, db):
        """Use real in-memory DB so all snapshot subsystem queries work."""
        # Insert test cc_sessions rows
        now = "2026-04-16T12:00:00"
        base = (
            "INSERT INTO cc_sessions "
            "(id, session_type, model, effort, status, started_at, last_activity_at) "
            "VALUES (?, ?, 'test', 'medium', 'active', ?, ?)"
        )
        await db.execute(base, ("s1", "foreground", now, now))
        await db.execute(base, ("s2", "background_reflection", now, now))
        await db.execute(base, ("s3", "background_reflection", now, now))
        await db.execute(base, ("s4", "background_task", now, now))
        await db.commit()

        svc = HealthDataService(db=db)
        snap = await svc.snapshot()
        assert snap["cc_sessions"]["foreground"]["active"] == 1
        # 2 background_reflection + 1 background_task = 3 background
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


class TestProviderHealth:
    """Test _provider_health helper and unified chain walk."""

    def test_probe_reachable_is_up(self):
        from genesis.observability.snapshots.call_sites import _provider_health

        assert _provider_health({"state": "closed", "probe_status": "reachable"}) == "up"

    def test_probe_unreachable_is_down(self):
        from genesis.observability.snapshots.call_sites import _provider_health

        assert _provider_health({"state": "closed", "probe_status": "unreachable"}) == "down"

    def test_probe_rate_limited_is_suspect(self):
        from genesis.observability.snapshots.call_sites import _provider_health

        assert _provider_health({"state": "closed", "probe_status": "rate_limited"}) == "suspect"

    def test_no_probe_falls_back_to_cb_open(self):
        from genesis.observability.snapshots.call_sites import _provider_health

        assert _provider_health({"state": "open"}) == "down"

    def test_no_probe_falls_back_to_cb_half_open(self):
        from genesis.observability.snapshots.call_sites import _provider_health

        assert _provider_health({"state": "half_open"}) == "suspect"

    def test_no_probe_falls_back_to_cb_closed(self):
        from genesis.observability.snapshots.call_sites import _provider_health

        assert _provider_health({"state": "closed"}) == "up"

    def test_error_state_is_down(self):
        from genesis.observability.snapshots.call_sites import _provider_health

        assert _provider_health({"state": "error"}) == "down"

    def test_probe_overrides_cb(self):
        """Probe says unreachable but CB says closed — probe wins."""
        from genesis.observability.snapshots.call_sites import _provider_health

        assert _provider_health({"state": "closed", "probe_status": "unreachable"}) == "down"

    def test_not_configured_returns_disabled(self):
        """Provider with no API key returns 'disabled' (not down/up).

        Regression for the call-site-11 spam loop: 'not_configured' is a
        deployment-time config state, not an outage. Callers must filter
        these providers out of routing decisions, not treat them as failures.
        """
        from genesis.observability.snapshots.call_sites import _provider_health

        assert _provider_health({"state": "closed", "probe_status": "not_configured"}) == "disabled"

    def test_not_configured_overrides_cb_state(self):
        """not_configured wins over CB state — config absence is permanent."""
        from genesis.observability.snapshots.call_sites import _provider_health

        # Even if CB happens to be open, missing API key is the dominant truth
        assert _provider_health({"state": "open", "probe_status": "not_configured"}) == "disabled"


class TestDisabledChainSemantics:
    """Disabled providers must be filtered, not treated as failures.

    The bug this addresses: Anthropic providers (claude-sonnet, claude-opus,
    claude-haiku) have no ANTHROPIC_API_KEY in this deployment because Genesis
    accesses Claude only via Claude Code background sessions. The probe
    correctly returned configured=False, but the call_sites snapshot then
    treated them as "unreachable → down → CRITICAL alert → Sentinel wake."
    The fix: filter disabled providers out of the chain walk, and if every
    provider is disabled, mark the site itself "disabled" (config state, not
    alert condition).
    """

    @pytest.mark.asyncio
    async def test_chain_walk_filters_disabled_providers(self):
        """A chain of [disabled, healthy] should report healthy, not degraded."""
        from genesis.observability.provider_health import ProviderProbeResult
        from genesis.observability.snapshots.call_sites import call_sites

        providers = {
            "anthropic-direct": _make_provider("anthropic-direct"),
            "openrouter-fallback": _make_provider("openrouter-fallback"),
        }
        config = _make_config(providers, {
            "test_site": CallSiteConfig(
                id="test_site",
                chain=["anthropic-direct", "openrouter-fallback"],
            ),
        })
        breakers = _mock_registry({
            "anthropic-direct": _mock_breaker(ProviderState.CLOSED),
            "openrouter-fallback": _mock_breaker(ProviderState.CLOSED),
        })
        probe_results = {
            "anthropic-direct": ProviderProbeResult(
                provider_name="anthropic-direct",
                reachable=False,
                configured=False,
                error="no API key configured",
            ),
            "openrouter-fallback": ProviderProbeResult(
                provider_name="openrouter-fallback",
                reachable=True,
                configured=True,
            ),
        }
        result = await call_sites(
            db=None, routing_config=config, breakers=breakers,
            probe_results=probe_results,
        )
        assert result["test_site"]["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_all_disabled_chain_yields_disabled_status(self):
        """A chain where every provider is unconfigured → site status='disabled'."""
        from genesis.observability.provider_health import ProviderProbeResult
        from genesis.observability.snapshots.call_sites import call_sites

        providers = {
            "claude-sonnet": _make_provider("claude-sonnet"),
            "claude-opus": _make_provider("claude-opus"),
        }
        config = _make_config(providers, {
            "11_user_model_synthesis": CallSiteConfig(
                id="11_user_model_synthesis",
                chain=["claude-sonnet", "claude-opus"],
            ),
        })
        breakers = _mock_registry({
            "claude-sonnet": _mock_breaker(ProviderState.CLOSED),
            "claude-opus": _mock_breaker(ProviderState.CLOSED),
        })
        probe_results = {
            "claude-sonnet": ProviderProbeResult(
                provider_name="claude-sonnet",
                reachable=False, configured=False,
                error="no API key configured",
            ),
            "claude-opus": ProviderProbeResult(
                provider_name="claude-opus",
                reachable=False, configured=False,
                error="no API key configured",
            ),
        }
        result = await call_sites(
            db=None, routing_config=config, breakers=breakers,
            probe_results=probe_results,
        )
        site = result["11_user_model_synthesis"]
        assert site["status"] == "disabled"
        assert site["disabled_reason"] == "no_api_keys_configured"

    @pytest.mark.asyncio
    async def test_probe_overlay_marks_not_configured(self):
        """Probe overlay sets probe_status='not_configured' + reason='no_api_key'."""
        from genesis.observability.provider_health import ProviderProbeResult
        from genesis.observability.snapshots.call_sites import call_sites

        providers = {"claude-sonnet": _make_provider("claude-sonnet")}
        config = _make_config(providers, {
            "test_site": CallSiteConfig(id="test_site", chain=["claude-sonnet"]),
        })
        breakers = _mock_registry({
            "claude-sonnet": _mock_breaker(ProviderState.CLOSED),
        })
        probe_results = {
            "claude-sonnet": ProviderProbeResult(
                provider_name="claude-sonnet",
                reachable=False, configured=False,
                error="no API key configured",
            ),
        }
        result = await call_sites(
            db=None, routing_config=config, breakers=breakers,
            probe_results=probe_results,
        )
        chain = result["test_site"]["chain_health"]
        assert chain[0]["probe_status"] == "not_configured"
        assert chain[0]["probe_reason"] == "no_api_key"


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
