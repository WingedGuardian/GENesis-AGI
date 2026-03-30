"""Degradation level tracking and call-site filtering."""

from __future__ import annotations

from typing import TYPE_CHECKING

from genesis.routing.types import DegradationLevel

if TYPE_CHECKING:
    from genesis.resilience.state import ResilienceStateMachine

# L2 (Reduced) skips these call sites
_L2_SKIP = {"12_surplus_brainstorm", "19_outreach_draft", "13_morning_report"}

# L3 (Essential) only keeps these call sites
_L3_KEEP = {"2_triage", "3_micro_reflection", "21_embeddings", "22_tagging"}


def should_skip_call_site(call_site_id: str, level: DegradationLevel) -> bool:
    """Return True if call_site_id should be skipped at the given degradation level."""
    if level in (DegradationLevel.NORMAL, DegradationLevel.FALLBACK):
        return False
    if level == DegradationLevel.REDUCED:
        return call_site_id in _L2_SKIP
    if level == DegradationLevel.ESSENTIAL:
        return call_site_id not in _L3_KEEP
    # L4/L5 — handled by circuit breaker, not call-site filtering
    return False


class DegradationTracker:
    def __init__(self, *, resilience_state: ResilienceStateMachine | None = None):
        self._level = DegradationLevel.NORMAL
        self._resilience_state = resilience_state

    @property
    def current_level(self) -> DegradationLevel:
        return self._level

    def update(self, level: DegradationLevel) -> None:
        self._level = level

    def update_from_resilience(self) -> None:
        """Update degradation level from composite resilience state."""
        if self._resilience_state is None:
            return
        self._level = self._resilience_state.current.to_legacy_degradation_level()

    def should_skip(self, call_site_id: str) -> bool:
        return should_skip_call_site(call_site_id, self._level)
