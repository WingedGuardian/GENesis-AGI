"""Tests for DegradationTracker resilience integration."""

from __future__ import annotations

from genesis.resilience.state import (
    CloudStatus,
    EmbeddingStatus,
    MemoryStatus,
    ResilienceStateMachine,
)
from genesis.routing.degradation import DegradationTracker
from genesis.routing.types import DegradationLevel


def test_legacy_update_still_works():
    """Legacy update() method sets level directly."""
    tracker = DegradationTracker()
    tracker.update(DegradationLevel.REDUCED)
    assert tracker.current_level == DegradationLevel.REDUCED


def test_no_arg_construction():
    """Default construction with no resilience_state works."""
    tracker = DegradationTracker()
    assert tracker.current_level == DegradationLevel.NORMAL


def test_update_from_resilience_normal():
    """Normal resilience state maps to NORMAL degradation."""
    rsm = ResilienceStateMachine()
    tracker = DegradationTracker(resilience_state=rsm)
    tracker.update_from_resilience()
    assert tracker.current_level == DegradationLevel.NORMAL


def test_update_from_resilience_cloud_fallback():
    """Cloud FALLBACK maps to L1."""
    rsm = ResilienceStateMachine()
    rsm.update_cloud(CloudStatus.FALLBACK)
    tracker = DegradationTracker(resilience_state=rsm)
    tracker.update_from_resilience()
    assert tracker.current_level == DegradationLevel.FALLBACK


def test_update_from_resilience_memory_impaired():
    """Memory FTS_ONLY overrides to MEMORY_IMPAIRED."""
    rsm = ResilienceStateMachine()
    rsm.update_memory(MemoryStatus.FTS_ONLY)
    tracker = DegradationTracker(resilience_state=rsm)
    tracker.update_from_resilience()
    assert tracker.current_level == DegradationLevel.MEMORY_IMPAIRED


def test_update_from_resilience_embedding_down():
    """Embedding UNAVAILABLE maps to LOCAL_COMPUTE_DOWN."""
    rsm = ResilienceStateMachine()
    rsm.update_embedding(EmbeddingStatus.UNAVAILABLE)
    tracker = DegradationTracker(resilience_state=rsm)
    tracker.update_from_resilience()
    assert tracker.current_level == DegradationLevel.LOCAL_COMPUTE_DOWN


def test_update_from_resilience_no_state():
    """update_from_resilience with no resilience_state is a no-op."""
    tracker = DegradationTracker()
    tracker.update(DegradationLevel.REDUCED)
    tracker.update_from_resilience()
    assert tracker.current_level == DegradationLevel.REDUCED
