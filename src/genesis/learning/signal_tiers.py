"""Step 2.5 — Signal weight tier enforcement and protected behaviors."""

from __future__ import annotations

from genesis.learning.types import SignalWeightTier

PROTECTED_BEHAVIORS: frozenset[str] = frozenset({
    "pushback",
    "honesty",
    "transparency",
    "safety",
})

TIER_MAP: dict[str, SignalWeightTier] = {
    "user_explicit_feedback": SignalWeightTier.STRONG,
    "task_outcome": SignalWeightTier.STRONG,
    "repeated_correction": SignalWeightTier.STRONG,
    "engagement_signal": SignalWeightTier.MODERATE,
    "timing_signal": SignalWeightTier.MODERATE,
    "style_preference": SignalWeightTier.MODERATE,
    "inferred_preference": SignalWeightTier.WEAK,
    "single_observation": SignalWeightTier.WEAK,
    "ambient_context": SignalWeightTier.WEAK,
}


def can_erode(behavior: str, tier: SignalWeightTier) -> bool:
    """Return False if a weak signal tries to erode a protected behavior."""
    return not (behavior in PROTECTED_BEHAVIORS and tier == SignalWeightTier.WEAK)


def get_tier(signal_name: str) -> SignalWeightTier:
    """Look up the tier for a signal, defaulting to MODERATE."""
    return TIER_MAP.get(signal_name, SignalWeightTier.MODERATE)
