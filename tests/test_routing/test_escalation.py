"""Tests for provider failure escalation — breaker trip → observation creation."""

from __future__ import annotations

import asyncio
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


async def _drain_tasks():
    """Let all pending asyncio tasks complete."""
    pending = [
        t for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and not t.done()
    ]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


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
    async def test_no_observation_below_threshold(self, escalation, db):
        """Fewer than _TRIP_THRESHOLD trips should not create observations."""
        for _ in range(_TRIP_THRESHOLD - 1):
            await escalation._on_event(_make_event())

        cursor = await db.execute("SELECT COUNT(*) FROM observations")
        count = (await cursor.fetchone())[0]
        assert count == 0

    async def test_observation_at_threshold(self, escalation, db):
        """Exactly _TRIP_THRESHOLD trips should create one observation."""
        for _ in range(_TRIP_THRESHOLD):
            await escalation._on_event(_make_event())

        await _drain_tasks()

        cursor = await db.execute(
            "SELECT source, type, priority, category FROM observations"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["source"] == "routing"
        assert row["type"] == "provider_failure"
        assert row["priority"] == "high"
        assert row["category"] == "system_health"

    async def test_no_duplicate_observations(self, escalation, db):
        """Further trips after escalation should not create more observations."""
        for _ in range(_TRIP_THRESHOLD + 10):
            await escalation._on_event(_make_event())

        await _drain_tasks()

        cursor = await db.execute("SELECT COUNT(*) FROM observations")
        count = (await cursor.fetchone())[0]
        assert count == 1

    async def test_separate_providers_tracked_independently(self, escalation, db):
        """Different providers each have their own trip counter."""
        for _ in range(_TRIP_THRESHOLD):
            await escalation._on_event(_make_event("provider-a"))
        for _ in range(_TRIP_THRESHOLD - 1):
            await escalation._on_event(_make_event("provider-b"))

        await _drain_tasks()

        cursor = await db.execute("SELECT COUNT(*) FROM observations")
        count = (await cursor.fetchone())[0]
        # Only provider-a hit threshold
        assert count == 1


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

    async def test_recovery_resets_escalation(self, escalation, db):
        """After recovery, a new failure cycle should create a new observation."""
        # First cycle
        for _ in range(_TRIP_THRESHOLD):
            await escalation._on_event(_make_event())
        await _drain_tasks()

        # Recover
        escalation.record_recovery("test-provider")

        # Resolve the first observation so dedup allows a new one
        await db.execute(
            "UPDATE observations SET resolved = 1 WHERE source = 'routing'"
        )
        await db.commit()

        # Second cycle
        for _ in range(_TRIP_THRESHOLD):
            await escalation._on_event(_make_event())
        await _drain_tasks()

        cursor = await db.execute(
            "SELECT COUNT(*) FROM observations WHERE source = 'routing'"
        )
        count = (await cursor.fetchone())[0]
        assert count == 2  # one resolved + one new


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
        # Trip the breaker
        cb.record_failure(ErrorCategory.TRANSIENT)
        assert cb._trip_count == 1

        # Force to HALF_OPEN so record_success can recover
        cb._state = ProviderState.HALF_OPEN

        # Recover
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
