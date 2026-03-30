"""Tests for signal tier enforcement and protected behaviors."""

from genesis.learning.signal_tiers import (
    PROTECTED_BEHAVIORS,
    TIER_MAP,
    can_erode,
    get_tier,
)
from genesis.learning.types import SignalWeightTier


class TestProtectedBehaviors:
    def test_includes_pushback(self):
        assert "pushback" in PROTECTED_BEHAVIORS

    def test_includes_honesty(self):
        assert "honesty" in PROTECTED_BEHAVIORS

    def test_includes_transparency(self):
        assert "transparency" in PROTECTED_BEHAVIORS

    def test_includes_safety(self):
        assert "safety" in PROTECTED_BEHAVIORS

    def test_is_frozenset(self):
        assert isinstance(PROTECTED_BEHAVIORS, frozenset)


class TestCanErode:
    def test_weak_signal_cannot_erode_protected(self):
        for behavior in PROTECTED_BEHAVIORS:
            assert can_erode(behavior, SignalWeightTier.WEAK) is False

    def test_moderate_signal_can_erode_protected(self):
        for behavior in PROTECTED_BEHAVIORS:
            assert can_erode(behavior, SignalWeightTier.MODERATE) is True

    def test_strong_signal_can_erode_protected(self):
        for behavior in PROTECTED_BEHAVIORS:
            assert can_erode(behavior, SignalWeightTier.STRONG) is True

    def test_weak_signal_can_erode_unprotected(self):
        assert can_erode("verbosity", SignalWeightTier.WEAK) is True

    def test_any_tier_can_erode_unprotected(self):
        for tier in SignalWeightTier:
            assert can_erode("formatting", tier) is True


class TestGetTier:
    def test_known_signal(self):
        assert get_tier("user_explicit_feedback") == SignalWeightTier.STRONG

    def test_weak_signal(self):
        assert get_tier("inferred_preference") == SignalWeightTier.WEAK

    def test_unknown_defaults_to_moderate(self):
        assert get_tier("totally_unknown") == SignalWeightTier.MODERATE

    def test_tier_map_has_entries(self):
        assert len(TIER_MAP) > 0
