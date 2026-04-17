"""Tests for RecoveryOrchestrator."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from genesis.observability.types import ProbeResult, ProbeStatus
from genesis.resilience.recovery import RecoveryOrchestrator
from genesis.resilience.state import (
    CCStatus,
    CloudStatus,
    ResilienceState,
    ResilienceStateMachine,
)


@pytest.fixture
def mocks():
    return {
        "db": AsyncMock(),
        "state_machine": ResilienceStateMachine(),
        "deferred_queue": AsyncMock(),
        "embedding_worker": AsyncMock(),
        "dead_letter": AsyncMock(),
        "event_bus": AsyncMock(),
    }


@pytest.fixture
def orchestrator(mocks):
    mocks["deferred_queue"].expire_stale = AsyncMock(return_value=0)
    mocks["deferred_queue"].drain_by_priority = AsyncMock(return_value=[])
    mocks["deferred_queue"].count_pending = AsyncMock(return_value=0)
    mocks["embedding_worker"].drain_pending = AsyncMock(return_value=0)
    mocks["dead_letter"].replay_pending = AsyncMock(return_value=0)
    return RecoveryOrchestrator(
        db=mocks["db"],
        state_machine=mocks["state_machine"],
        deferred_queue=mocks["deferred_queue"],
        embedding_worker=mocks["embedding_worker"],
        dead_letter=mocks["dead_letter"],
        event_bus=mocks["event_bus"],
        confirmation_probes=2,
        confirmation_interval_s=0,
    )


class TestShouldRecover:
    async def test_detects_improvement(self, orchestrator):
        old = ResilienceState(cloud=CloudStatus.FALLBACK)
        new = ResilienceState(cloud=CloudStatus.NORMAL)
        assert await orchestrator.should_recover(old, new) is True

    async def test_detects_cc_improvement(self, orchestrator):
        old = ResilienceState(cc=CCStatus.THROTTLED)
        new = ResilienceState(cc=CCStatus.NORMAL)
        assert await orchestrator.should_recover(old, new) is True

    async def test_no_improvement(self, orchestrator):
        old = ResilienceState(cloud=CloudStatus.NORMAL)
        new = ResilienceState(cloud=CloudStatus.NORMAL)
        assert await orchestrator.should_recover(old, new) is False

    async def test_degradation_not_recovery(self, orchestrator):
        old = ResilienceState(cloud=CloudStatus.NORMAL)
        new = ResilienceState(cloud=CloudStatus.FALLBACK)
        assert await orchestrator.should_recover(old, new) is False


class TestConfirmRecovery:
    async def test_passes_when_all_healthy(self, orchestrator):
        probe = AsyncMock(return_value=ProbeResult(
            name="test", status=ProbeStatus.HEALTHY, latency_ms=10,
        ))
        orchestrator._health_probes = [probe]
        assert await orchestrator.confirm_recovery() is True
        assert probe.call_count == 2  # confirmation_probes=2

    async def test_fails_when_probe_fails(self, orchestrator):
        call_count = [0]
        async def flaky_probe():
            call_count[0] += 1
            if call_count[0] == 2:
                return ProbeResult(name="test", status=ProbeStatus.DOWN, latency_ms=10)
            return ProbeResult(name="test", status=ProbeStatus.HEALTHY, latency_ms=10)
        orchestrator._health_probes = [flaky_probe]
        assert await orchestrator.confirm_recovery() is False

    async def test_no_probes_returns_true(self, orchestrator):
        orchestrator._health_probes = []
        assert await orchestrator.confirm_recovery() is True


class TestRunRecovery:
    async def test_expires_stale_first(self, orchestrator, mocks):
        mocks["deferred_queue"].expire_stale = AsyncMock(return_value=3)
        report = await orchestrator.run_recovery()
        assert report.items_expired == 3
        mocks["deferred_queue"].expire_stale.assert_awaited_once()

    async def test_drains_embeddings(self, orchestrator, mocks):
        mocks["embedding_worker"].drain_pending = AsyncMock(return_value=5)
        report = await orchestrator.run_recovery()
        assert report.embeddings_recovered == 5
        mocks["embedding_worker"].drain_pending.assert_awaited_once_with(limit=500)

    async def test_processes_deferred_work(self, orchestrator, mocks):
        items = [{"id": "a"}, {"id": "b"}]
        mocks["deferred_queue"].drain_by_priority = AsyncMock(return_value=items)
        mocks["deferred_queue"].mark_processing = AsyncMock(return_value=True)
        mocks["deferred_queue"].mark_completed = AsyncMock(return_value=True)
        report = await orchestrator.run_recovery()
        assert report.items_drained == 2

    async def test_replays_dead_letters(self, orchestrator, mocks):
        mocks["dead_letter"].replay_pending = AsyncMock(return_value=4)
        report = await orchestrator.run_recovery()
        assert report.dead_letters_replayed == 4

    async def test_queue_overflow_emits_event(self, orchestrator, mocks):
        mocks["deferred_queue"].count_pending = AsyncMock(return_value=1500)
        orchestrator._queue_overflow_threshold = 1000
        await orchestrator.run_recovery()
        mocks["event_bus"].emit.assert_awaited()
        call_args = mocks["event_bus"].emit.call_args
        assert "overflow" in call_args[0][2]

    async def test_report_has_duration(self, orchestrator):
        report = await orchestrator.run_recovery()
        assert report.duration_s >= 0.0

    async def test_redispatch_when_dispatch_fn_set(self, orchestrator, mocks):
        """With dispatch_fn set and redispatch available, uses redispatch."""
        mocks["dead_letter"].redispatch = AsyncMock(return_value=(3, 1))
        orchestrator.set_dispatch_fn(AsyncMock())
        report = await orchestrator.run_recovery()
        assert report.dead_letters_replayed == 3
        assert report.dead_letters_failed == 1
        mocks["dead_letter"].redispatch.assert_awaited_once()

    async def test_fallback_to_replay_without_dispatch_fn(self, orchestrator, mocks):
        """Without dispatch_fn, falls back to replay_pending."""
        orchestrator._dispatch_fn = None
        mocks["dead_letter"].replay_pending = AsyncMock(return_value=2)
        report = await orchestrator.run_recovery()
        assert report.dead_letters_replayed == 2
        mocks["dead_letter"].replay_pending.assert_awaited_once()
