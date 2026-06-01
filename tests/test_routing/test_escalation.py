"""Tests for provider failure escalation — breaker trip → observation creation."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest

from genesis.db.schema import INDEXES, TABLES
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
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["observations"])
        for idx in INDEXES:
            if "observations" in idx:
                await conn.execute(idx)
        yield conn


@pytest.fixture
def event_bus():
    return GenesisEventBus()


@pytest.fixture
def escalation(db, event_bus):
    return ProviderEscalation(db=db, event_bus=event_bus)


class TestTripTracking:
    """Test the state tracking logic (_on_event) without DB interaction."""

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


class TestObservationCreation:
    """Test _create_observation DB writes directly (bypasses create_task)."""

    async def test_creates_observation(self, escalation, db):
        """_create_observation should insert a high-priority observation."""
        state = {"trip_count": 5, "first_trip_at": datetime.now(UTC).isoformat(), "escalated": True}
        await escalation._create_observation("test-provider", state)

        cursor = await db.execute(
            "SELECT source, type, priority, category FROM observations"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["source"] == "routing"
        assert row["type"] == "provider_failure"
        assert row["priority"] == "high"
        assert row["category"] == "system_health"

    async def test_dedup_prevents_duplicates(self, escalation, db):
        """Same provider should not create duplicate unresolved observations."""
        state = {"trip_count": 5, "first_trip_at": datetime.now(UTC).isoformat(), "escalated": True}
        await escalation._create_observation("test-provider", state)
        await escalation._create_observation("test-provider", state)

        cursor = await db.execute("SELECT COUNT(*) FROM observations")
        count = (await cursor.fetchone())[0]
        assert count == 1

    async def test_new_observation_after_resolution(self, escalation, db):
        """After resolving, a new observation for the same provider is allowed."""
        state = {"trip_count": 5, "first_trip_at": datetime.now(UTC).isoformat(), "escalated": True}
        await escalation._create_observation("test-provider", state)

        # Resolve
        await db.execute("UPDATE observations SET resolved = 1 WHERE source = 'routing'")
        await db.commit()

        # Second observation
        await escalation._create_observation("test-provider", state)

        cursor = await db.execute("SELECT COUNT(*) FROM observations WHERE source = 'routing'")
        count = (await cursor.fetchone())[0]
        assert count == 2


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
