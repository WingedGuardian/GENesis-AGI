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
        # Raw INSERT (the repo's obs-test convention) so setup does not depend on
        # obs_crud.create being unmocked under the full suite. The content_hash
        # matches what _resolve_observation resolves on.
        await empty_db.execute(
            "INSERT INTO observations "
            "(id, source, type, content, priority, resolved, content_hash, created_at) "
            "VALUES (?, 'routing', 'provider_failure', ?, 'high', 0, ?, datetime('now'))",
            (oid, f"{provider} failing", escalation._provider_content_hash(provider)),
        )
        await empty_db.commit()

    async def _pf_state(self, empty_db, oid):
        # Raw read of (resolved, resolution_notes) — independent of obs_crud so the
        # assertion can't be defeated by a leaked CRUD mock elsewhere in the suite.
        cur = await empty_db.execute(
            "SELECT resolved, resolution_notes FROM observations WHERE id = ?",
            (oid,),
        )
        return await cur.fetchone()

    async def test_recovery_resolves_provider_observation(self, escalation, empty_db):
        """record_recovery resolves THIS provider's unresolved provider_failure obs."""
        await self._make_pf(escalation, empty_db, "prov-x", "pf-x")
        await escalation._resolve_observation("prov-x")
        row = await self._pf_state(empty_db, "pf-x")
        assert row["resolved"] == 1
        assert "recovered" in (row["resolution_notes"] or "")

    async def test_recovery_does_not_resolve_other_providers(self, escalation, empty_db):
        """A recovered provider must NOT resolve a different (still-down) provider."""
        await self._make_pf(escalation, empty_db, "prov-a", "pf-a")
        await self._make_pf(escalation, empty_db, "prov-b", "pf-b")
        await escalation._resolve_observation("prov-a")
        assert (await self._pf_state(empty_db, "pf-a"))["resolved"] == 1
        assert (await self._pf_state(empty_db, "pf-b"))["resolved"] == 0

    async def test_record_recovery_schedules_resolve_task(self, escalation, empty_db):
        """record_recovery (running loop) schedules + completes the resolve task."""
        import asyncio

        await self._make_pf(escalation, empty_db, "prov-y", "pf-y")
        escalation.record_recovery("prov-y")
        pending = [t for t in asyncio.all_tasks() if t.get_name() == "escalation-resolve-prov-y"]
        assert pending, "record_recovery did not schedule the resolve task"
        await asyncio.gather(*pending)
        assert (await self._pf_state(empty_db, "pf-y"))["resolved"] == 1

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


class TestTaskFailureReporting:
    """The tracked_task swap (reflex A4): a crash in a deferred escalation
    task must land on the event bus as task.failed — the log-only
    _on_task_done callback it replaced reported to nobody. The inner DB
    helpers swallow their own errors, so these tests inject the failure at
    the coroutine boundary (the escape path the wrapper exists for)."""

    async def _settle(self):
        import asyncio

        for _ in range(10):
            await asyncio.sleep(0)

    def _capture(self, event_bus):
        captured: list = []

        async def listener(event):
            captured.append(event)

        event_bus.subscribe(listener, min_severity=Severity.ERROR)
        return captured

    async def test_create_observation_crash_emits_task_failed(
        self, escalation, event_bus, monkeypatch
    ):
        captured = self._capture(event_bus)

        async def _boom(provider, state):
            raise RuntimeError("obs write exploded")

        monkeypatch.setattr(escalation, "_create_observation", _boom)
        for _ in range(_TRIP_THRESHOLD):
            await escalation._on_event(_make_event())
        await self._settle()

        failed = [e for e in captured if e.event_type == "task.failed"]
        assert len(failed) == 1
        assert failed[0].details["task_name"] == "escalation-obs-test-provider"
        assert failed[0].details["error_type"] == "RuntimeError"
        assert failed[0].subsystem == Subsystem.ROUTING

    async def test_resolve_observation_crash_emits_task_failed(
        self, escalation, event_bus, monkeypatch
    ):
        captured = self._capture(event_bus)

        async def _boom(provider):
            raise RuntimeError("resolve exploded")

        monkeypatch.setattr(escalation, "_resolve_observation", _boom)
        # Seed state so record_recovery has something to clear, then recover.
        for _ in range(_TRIP_THRESHOLD):
            await escalation._on_event(_make_event())
        escalation.record_recovery("test-provider")
        await self._settle()

        failed = [
            e
            for e in captured
            if e.event_type == "task.failed"
            and e.details.get("task_name") == "escalation-resolve-test-provider"
        ]
        assert len(failed) == 1
        assert failed[0].details["error_type"] == "RuntimeError"

    async def test_successful_task_emits_nothing(self, escalation, event_bus):
        captured = self._capture(event_bus)
        for _ in range(_TRIP_THRESHOLD):
            await escalation._on_event(_make_event())
        await self._settle()
        assert [e for e in captured if e.event_type == "task.failed"] == []


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
