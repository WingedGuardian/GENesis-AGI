"""Tests for provider failure escalation — breaker trip → observation creation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from genesis.observability.events import GenesisEventBus
from genesis.observability.types import GenesisEvent, Severity, Subsystem
from genesis.routing.circuit_breaker import CircuitBreaker
from genesis.routing.escalation import _TRIP_THRESHOLD, ProviderEscalation
from genesis.routing.types import ErrorCategory, ProviderConfig


def _provider(name: str = "test-provider") -> ProviderConfig:
    return ProviderConfig(
        name=name,
        provider_type="openrouter",
        model_id="test/model",
        is_free=False,
        rpm_limit=10,
        open_duration_s=120,
    )


def _make_event(provider: str = "test-provider") -> GenesisEvent:
    return GenesisEvent(
        subsystem=Subsystem.ROUTING,
        severity=Severity.WARNING,
        event_type="breaker.tripped",
        message=f"Circuit breaker tripped for {provider}",
        timestamp=datetime.now(UTC).isoformat(),
        details={"provider": provider, "call_site": "test"},
    )


@pytest.fixture
def event_bus():
    return GenesisEventBus()


@pytest.fixture
def escalation(empty_db, event_bus):
    return ProviderEscalation(db=empty_db, event_bus=event_bus)


class TestTripTracking:
    """Test the state tracking logic (_on_event)."""

    async def test_no_escalation_below_threshold(self, escalation):
        """Fewer than _TRIP_THRESHOLD trips should not set escalated flag."""
        for _ in range(_TRIP_THRESHOLD - 1):
            await escalation._on_event(_make_event())

        state = escalation._state.get("test-provider")
        assert state is not None
        assert state["trip_count"] == _TRIP_THRESHOLD - 1
        assert state["escalated"] is False

    async def test_escalation_at_threshold(self, escalation):
        """Exactly _TRIP_THRESHOLD trips should set escalated flag."""
        for _ in range(_TRIP_THRESHOLD):
            await escalation._on_event(_make_event())

        state = escalation._state["test-provider"]
        assert state["trip_count"] == _TRIP_THRESHOLD
        assert state["escalated"] is True
        assert state["first_trip_at"] is not None

    async def test_separate_providers_tracked_independently(self, escalation):
        """Different providers each have their own trip counter."""
        for _ in range(_TRIP_THRESHOLD):
            await escalation._on_event(_make_event("provider-a"))
        for _ in range(_TRIP_THRESHOLD - 1):
            await escalation._on_event(_make_event("provider-b"))

        assert escalation._state["provider-a"]["escalated"] is True
        assert escalation._state["provider-b"]["escalated"] is False


class TestRecovery:
    async def test_recovery_clears_state(self, escalation):
        """record_recovery should clear the provider's tracking state."""
        for _ in range(3):
            await escalation._on_event(_make_event())

        assert "test-provider" in escalation._state
        escalation.record_recovery("test-provider")
        assert "test-provider" not in escalation._state

    async def test_recovery_unknown_provider_no_error(self, escalation):
        """Recovering an untracked provider should not raise."""
        escalation.record_recovery("nonexistent")

    async def _make_pf(self, escalation, empty_db, provider, oid):
        from genesis.db.crud import observations as obs_crud

        await obs_crud.create(
            empty_db, id=oid, source="routing", type="provider_failure",
            content=f"{provider} failing", priority="high",
            created_at=datetime.now(UTC).isoformat(),
            content_hash=escalation._provider_content_hash(provider),
            skip_if_duplicate=True,
        )

    async def test_recovery_resolves_provider_observation(self, escalation, empty_db):
        """record_recovery resolves THIS provider's unresolved provider_failure obs."""
        from genesis.db.crud import observations as obs_crud

        await self._make_pf(escalation, empty_db, "prov-x", "pf-x")
        await escalation._resolve_observation("prov-x")
        row = await obs_crud.get_by_id(empty_db, "pf-x")
        assert row["resolved"] == 1
        assert "recovered" in (row["resolution_notes"] or "")

    async def test_recovery_does_not_resolve_other_providers(self, escalation, empty_db):
        """A recovered provider must NOT resolve a different (still-down) provider."""
        from genesis.db.crud import observations as obs_crud

        await self._make_pf(escalation, empty_db, "prov-a", "pf-a")
        await self._make_pf(escalation, empty_db, "prov-b", "pf-b")
        await escalation._resolve_observation("prov-a")
        assert (await obs_crud.get_by_id(empty_db, "pf-a"))["resolved"] == 1
        assert (await obs_crud.get_by_id(empty_db, "pf-b"))["resolved"] == 0

    async def test_record_recovery_schedules_resolve_task(self, escalation, empty_db):
        """record_recovery (running loop) schedules + completes the resolve task."""
        import asyncio

        from genesis.db.crud import observations as obs_crud

        await self._make_pf(escalation, empty_db, "prov-y", "pf-y")
        escalation.record_recovery("prov-y")
        pending = [
            t for t in asyncio.all_tasks()
            if t.get_name() == "escalation-resolve-prov-y"
        ]
        assert pending, "record_recovery did not schedule the resolve task"
        await asyncio.gather(*pending)
        assert (await obs_crud.get_by_id(empty_db, "pf-y"))["resolved"] == 1

    async def test_record_recovery_no_running_loop_no_raise(self, escalation):
        """record_recovery from a sync/no-loop context (worker thread) must not raise."""
        import asyncio

        # Runs the sync method in a thread with no running loop → guard returns.
        await asyncio.to_thread(escalation.record_recovery, "test-provider")


class TestEventFiltering:
    async def test_ignores_non_breaker_events(self, escalation):
        """Events other than breaker.tripped should be ignored."""
        event = GenesisEvent(
            subsystem=Subsystem.ROUTING,
            severity=Severity.WARNING,
            event_type="provider.fallback",
            message="fallback happened",
            timestamp=datetime.now(UTC).isoformat(),
            details={"provider": "test"},
        )
        await escalation._on_event(event)
        assert len(escalation._state) == 0


class TestEventBusIntegration:
    async def test_attach_subscribes_to_bus(self, escalation, event_bus):
        """attach() should register as a listener on the event bus."""
        initial_count = len(event_bus._listeners)
        escalation.attach()
        assert len(event_bus._listeners) == initial_count + 1


class TestCircuitBreakerRecoveryCallback:
    def test_on_recovery_called_on_full_recovery(self):
        """on_recovery fires when breaker transitions to CLOSED with trip_count reset."""
        from genesis.routing.types import ProviderState

        recoveries = []
        cb = CircuitBreaker(
            _provider("test"),
            failure_threshold=1,
            success_threshold=1,
            on_recovery=lambda name: recoveries.append(name),
        )
        cb.record_failure(ErrorCategory.TRANSIENT)
        assert cb._trip_count == 1

        cb._state = ProviderState.HALF_OPEN
        cb.record_success()
        assert cb._trip_count == 0
        assert recoveries == ["test"]

    def test_on_recovery_not_called_without_prior_trips(self):
        """on_recovery should NOT fire on a normal success (no prior trips)."""
        recoveries = []
        cb = CircuitBreaker(
            _provider("test"),
            on_recovery=lambda name: recoveries.append(name),
        )
        cb.record_success()
        assert recoveries == []
