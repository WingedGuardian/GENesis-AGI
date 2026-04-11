"""Tests for composite resilience state machine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from genesis.resilience.state import (
    CCStatus,
    CloudStatus,
    EmbeddingStatus,
    MemoryStatus,
    ResilienceStateMachine,
)
from genesis.routing.types import DegradationLevel


class TestBasicTransitions:
    def test_initial_state_all_normal(self):
        sm = ResilienceStateMachine()
        assert sm.current.cloud == CloudStatus.NORMAL
        assert sm.current.memory == MemoryStatus.NORMAL
        assert sm.current.embedding == EmbeddingStatus.NORMAL
        assert sm.current.cc == CCStatus.NORMAL
        assert not sm.is_any_degraded()

    def test_cloud_transition(self):
        sm = ResilienceStateMachine()
        transitions = sm.update_cloud(CloudStatus.FALLBACK)
        assert len(transitions) == 1
        assert transitions[0].axis == "cloud"
        assert transitions[0].old_value == "NORMAL"
        assert transitions[0].new_value == "FALLBACK"
        assert not transitions[0].suppressed
        assert sm.current.cloud == CloudStatus.FALLBACK
        assert sm.is_any_degraded()

    def test_no_transition_on_same_state(self):
        sm = ResilienceStateMachine()
        transitions = sm.update_cloud(CloudStatus.NORMAL)
        assert transitions == []

    def test_memory_transition(self):
        sm = ResilienceStateMachine()
        sm.update_memory(MemoryStatus.FTS_ONLY)
        assert sm.current.memory == MemoryStatus.FTS_ONLY
        assert sm.is_any_degraded()

    def test_embedding_transition(self):
        sm = ResilienceStateMachine()
        sm.update_embedding(EmbeddingStatus.QUEUED)
        assert sm.current.embedding == EmbeddingStatus.QUEUED

    def test_cc_transition(self):
        sm = ResilienceStateMachine()
        sm.update_cc(CCStatus.THROTTLED)
        assert sm.current.cc == CCStatus.THROTTLED

    def test_multi_axis_degradation(self):
        sm = ResilienceStateMachine()
        sm.update_cloud(CloudStatus.REDUCED)
        sm.update_memory(MemoryStatus.DOWN)
        assert sm.current.cloud == CloudStatus.REDUCED
        assert sm.current.memory == MemoryStatus.DOWN
        assert sm.is_any_degraded()

    def test_recovery(self):
        sm = ResilienceStateMachine()
        sm.update_cloud(CloudStatus.OFFLINE)
        sm.update_cloud(CloudStatus.NORMAL)
        assert sm.current.cloud == CloudStatus.NORMAL
        assert not sm.is_any_degraded()

    def test_transitions_recorded(self):
        sm = ResilienceStateMachine()
        sm.update_cloud(CloudStatus.FALLBACK)
        sm.update_cloud(CloudStatus.REDUCED)
        assert len(sm.current.transitions) == 2


class TestLegacyMapping:
    def test_cloud_normal(self):
        sm = ResilienceStateMachine()
        assert sm.current.to_legacy_degradation_level() == DegradationLevel.NORMAL

    def test_cloud_fallback(self):
        sm = ResilienceStateMachine()
        sm.update_cloud(CloudStatus.FALLBACK)
        assert sm.current.to_legacy_degradation_level() == DegradationLevel.FALLBACK

    def test_cloud_reduced(self):
        sm = ResilienceStateMachine()
        sm.update_cloud(CloudStatus.REDUCED)
        assert sm.current.to_legacy_degradation_level() == DegradationLevel.REDUCED

    def test_cloud_essential(self):
        sm = ResilienceStateMachine()
        sm.update_cloud(CloudStatus.ESSENTIAL)
        assert sm.current.to_legacy_degradation_level() == DegradationLevel.ESSENTIAL

    def test_cloud_offline(self):
        sm = ResilienceStateMachine()
        sm.update_cloud(CloudStatus.OFFLINE)
        assert sm.current.to_legacy_degradation_level() == DegradationLevel.ESSENTIAL

    def test_memory_impaired_overrides_normal(self):
        sm = ResilienceStateMachine()
        sm.update_memory(MemoryStatus.FTS_ONLY)
        assert sm.current.to_legacy_degradation_level() == DegradationLevel.MEMORY_IMPAIRED

    def test_memory_impaired_does_not_override_worse_cloud(self):
        sm = ResilienceStateMachine()
        sm.update_cloud(CloudStatus.ESSENTIAL)
        sm.update_memory(MemoryStatus.FTS_ONLY)
        # ESSENTIAL (L3) vs MEMORY_IMPAIRED (L4) — L4 > L3, so MEMORY_IMPAIRED wins
        assert sm.current.to_legacy_degradation_level() == DegradationLevel.MEMORY_IMPAIRED

    def test_embedding_local_compute_down(self):
        sm = ResilienceStateMachine()
        sm.update_embedding(EmbeddingStatus.UNAVAILABLE)
        assert sm.current.to_legacy_degradation_level() == DegradationLevel.LOCAL_COMPUTE_DOWN

    def test_embedding_overrides_memory(self):
        sm = ResilienceStateMachine()
        sm.update_memory(MemoryStatus.DOWN)
        sm.update_embedding(EmbeddingStatus.UNAVAILABLE)
        # LOCAL_COMPUTE_DOWN (L5) > MEMORY_IMPAIRED (L4)
        assert sm.current.to_legacy_degradation_level() == DegradationLevel.LOCAL_COMPUTE_DOWN


class TestFlappingProtection:
    def _make_clock(self, start: datetime | None = None):
        """Return a clock function that can be advanced."""
        current = [start or datetime(2026, 3, 11, 12, 0, 0, tzinfo=UTC)]
        def clock():
            return current[0]
        def advance(seconds: int):
            current[0] += timedelta(seconds=seconds)
        return clock, advance

    def test_no_flapping_under_threshold(self):
        clock, advance = self._make_clock()
        sm = ResilienceStateMachine(clock=clock)
        # 3 transitions in 15 min — should NOT trigger flapping
        sm.update_cloud(CloudStatus.FALLBACK)
        advance(60)
        sm.update_cloud(CloudStatus.NORMAL)
        advance(60)
        sm.update_cloud(CloudStatus.FALLBACK)
        assert sm.current.cloud == CloudStatus.FALLBACK

    def test_flapping_triggers_stabilization(self):
        clock, advance = self._make_clock()
        sm = ResilienceStateMachine(clock=clock)
        # 4 transitions → triggers on the 4th
        sm.update_cloud(CloudStatus.FALLBACK)
        advance(30)
        sm.update_cloud(CloudStatus.NORMAL)
        advance(30)
        sm.update_cloud(CloudStatus.FALLBACK)
        advance(30)
        # 4th transition triggers flapping
        sm.update_cloud(CloudStatus.NORMAL)
        # Should hold the worse state (FALLBACK < NORMAL, so FALLBACK)
        assert sm.current.cloud == CloudStatus.FALLBACK

    def test_stabilization_suppresses_improvement(self):
        clock, advance = self._make_clock()
        sm = ResilienceStateMachine(clock=clock)
        # Trigger flapping
        sm.update_cloud(CloudStatus.FALLBACK)
        advance(30)
        sm.update_cloud(CloudStatus.NORMAL)
        advance(30)
        sm.update_cloud(CloudStatus.FALLBACK)
        advance(30)
        sm.update_cloud(CloudStatus.NORMAL)  # triggers flapping, holds FALLBACK
        advance(60)
        # During stabilization, improvement is suppressed
        transitions = sm.update_cloud(CloudStatus.NORMAL)
        assert len(transitions) == 1
        assert transitions[0].suppressed
        assert sm.current.cloud == CloudStatus.FALLBACK

    def test_stabilization_allows_worsening(self):
        clock, advance = self._make_clock()
        sm = ResilienceStateMachine(clock=clock)
        # Trigger flapping
        sm.update_cloud(CloudStatus.FALLBACK)
        advance(30)
        sm.update_cloud(CloudStatus.NORMAL)
        advance(30)
        sm.update_cloud(CloudStatus.FALLBACK)
        advance(30)
        sm.update_cloud(CloudStatus.NORMAL)  # triggers flapping, holds FALLBACK
        advance(60)
        # During stabilization, worsening is allowed
        sm.update_cloud(CloudStatus.OFFLINE)
        assert sm.current.cloud == CloudStatus.OFFLINE

    def test_stabilization_expires(self):
        clock, advance = self._make_clock()
        sm = ResilienceStateMachine(clock=clock)
        # Trigger flapping
        sm.update_cloud(CloudStatus.FALLBACK)
        advance(30)
        sm.update_cloud(CloudStatus.NORMAL)
        advance(30)
        sm.update_cloud(CloudStatus.FALLBACK)
        advance(30)
        sm.update_cloud(CloudStatus.NORMAL)  # triggers flapping
        # Advance past 10-minute stabilization
        advance(601)
        sm.update_cloud(CloudStatus.NORMAL)
        assert sm.current.cloud == CloudStatus.NORMAL

    def test_independent_axes_dont_interfere(self):
        clock, advance = self._make_clock()
        sm = ResilienceStateMachine(clock=clock)
        # Flap cloud axis
        sm.update_cloud(CloudStatus.FALLBACK)
        advance(30)
        sm.update_cloud(CloudStatus.NORMAL)
        advance(30)
        sm.update_cloud(CloudStatus.FALLBACK)
        advance(30)
        sm.update_cloud(CloudStatus.NORMAL)  # flapping on cloud
        # Memory axis should still work normally
        sm.update_memory(MemoryStatus.FTS_ONLY)
        assert sm.current.memory == MemoryStatus.FTS_ONLY
        sm.update_memory(MemoryStatus.NORMAL)
        assert sm.current.memory == MemoryStatus.NORMAL


class TestCCFlappingOptOut:
    """Part 9a — CC axis must opt out of flapping protection.

    CC rate limits are transient API events that can bounce 4+ times in 15
    min under normal load. The flapping protection designed for cascading
    provider failures (cloud/memory/embedding) latches RATE_LIMITED for
    10 min when triggered, blocking recovery signals and causing
    downstream false alarms (see Part 9 false-alarm incident). For the
    CC axis, all transitions must apply unconditionally.
    """

    def _make_clock(self, start: datetime | None = None):
        current = [start or datetime(2026, 3, 11, 12, 0, 0, tzinfo=UTC)]
        def clock():
            return current[0]
        def advance(seconds: int):
            current[0] += timedelta(seconds=seconds)
        return clock, advance

    def test_cc_recovery_never_suppressed(self):
        """Force a rapid burst of CC transitions including the recovery
        transition. With flapping protection opted out, the recovery
        must apply with suppressed=False.
        """
        clock, advance = self._make_clock()
        sm = ResilienceStateMachine(clock=clock)
        # 5 transitions in 5 seconds — would trigger flapping on any
        # protected axis, holding the worse state.
        sm.update_cc(CCStatus.RATE_LIMITED)
        advance(1)
        sm.update_cc(CCStatus.NORMAL)
        advance(1)
        sm.update_cc(CCStatus.RATE_LIMITED)
        advance(1)
        sm.update_cc(CCStatus.NORMAL)
        advance(1)
        sm.update_cc(CCStatus.RATE_LIMITED)
        advance(1)
        # The recovery MUST apply.
        transitions = sm.update_cc(CCStatus.NORMAL)
        assert len(transitions) == 1
        assert transitions[0].suppressed is False
        assert transitions[0].new_value == "NORMAL"
        assert sm.current.cc == CCStatus.NORMAL

    def test_cc_no_stabilization_lockout(self):
        """The CC axis must never enter a stabilization hold — flapping
        protection is bypassed entirely, so stabilize_until stays None
        regardless of transition burst.
        """
        clock, advance = self._make_clock()
        sm = ResilienceStateMachine(clock=clock)
        for _ in range(10):
            sm.update_cc(CCStatus.RATE_LIMITED)
            advance(1)
            sm.update_cc(CCStatus.NORMAL)
            advance(1)
        assert sm._flap["cc"].stabilize_until is None

    def test_cloud_recovery_still_suppressed_during_stabilization(self):
        """Regression guard: the other axes MUST still get flapping
        protection. Removing the opt-out for cloud/memory/embedding would
        silently undo the protection for genuinely cascading failures.
        """
        clock, advance = self._make_clock()
        sm = ResilienceStateMachine(clock=clock)
        # Trigger flapping on cloud
        sm.update_cloud(CloudStatus.FALLBACK)
        advance(30)
        sm.update_cloud(CloudStatus.NORMAL)
        advance(30)
        sm.update_cloud(CloudStatus.FALLBACK)
        advance(30)
        sm.update_cloud(CloudStatus.NORMAL)  # 4th → enters stabilization
        advance(60)
        # Recovery suppressed
        transitions = sm.update_cloud(CloudStatus.NORMAL)
        assert len(transitions) == 1
        assert transitions[0].suppressed is True
        assert sm.current.cloud == CloudStatus.FALLBACK

    def test_cc_transitions_still_recorded(self):
        """Even with flapping off, every transition should append to the
        state.transitions log for observability.
        """
        clock, advance = self._make_clock()
        sm = ResilienceStateMachine(clock=clock)
        sm.update_cc(CCStatus.RATE_LIMITED)
        advance(1)
        sm.update_cc(CCStatus.NORMAL)
        cc_transitions = [t for t in sm.current.transitions if t.axis == "cc"]
        assert len(cc_transitions) == 2
        assert all(t.suppressed is False for t in cc_transitions)
