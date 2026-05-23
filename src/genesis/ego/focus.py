"""Focus selector — perceive phase of the unified cognitive loop.

Selects what the ego should pay attention to from a batch of signals.
Uses lightweight LLM classification via ``router.route_call()`` when
there are multiple competing signals, and shortcuts (no LLM call) for
single-signal and critical-preemption cases.

Context weights are a lookup table keyed on focus_type — the LLM
chooses WHAT to focus on, not HOW MUCH context to include. This
removes the lowest-confidence piece from the critical path. Weights
are defined here but not wired to ``build()`` until PR 3.

Part of PR 1: Signal System + Focus Selector (unified cognitive loop).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from genesis.ego.signals import EgoSignal

logger = logging.getLogger(__name__)

_CALL_SITE = "40_ego_focus_selection"


# ---------------------------------------------------------------------------
# Router protocol (same pattern as learning/triage/classifier.py:21-24)
# ---------------------------------------------------------------------------

class _Router(Protocol):
    async def route_call(
        self, call_site_id: str, messages: list[dict[str, Any]], **kwargs: Any
    ) -> Any: ...


# ---------------------------------------------------------------------------
# FocusResult — output of the perceive phase
# ---------------------------------------------------------------------------

@dataclass
class FocusResult:
    """Result of focus selection — what the ego should think about."""

    focus_type: str  # "proactive", "daily_briefing", "reactive", etc.
    focus_id: str | None = None  # specific target (e.g., goal_id)
    rationale: str = ""  # why this focus was selected
    signals_consumed: list[str] = field(default_factory=list)
    context_weights: dict[str, str] = field(default_factory=dict)
    perceive_cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# Context weights lookup table
# ---------------------------------------------------------------------------

# Section names match user_context.py build() order.
# Weight levels:
#   "always" — never overridden, always full depth
#   "deep"   — full detail, all data
#   "light"  — 1-2 line summary (implemented in PR 3)
#   "skip"   — omit entirely
#
# "always" sections: user_model, ego_notepad, directives, output_contract
# These are NEVER overridden regardless of focus type.

_ALL_SECTIONS = (
    "user_model",
    "ego_notepad",
    "goals",
    "directives",
    "world_snapshot",
    "activity_pulse",
    "recent_conversations",
    "backlog_summary",
    "escalations",
    "capabilities",
    "follow_ups",
    "proposal_history",
    "proposal_board",
    "execution_outcomes",
    "goal_progress",
    "capability_performance",
    "autonomy_readiness",
    "recurring_patterns",
    "output_contract",
)

# Sections that are always included at full depth regardless of weights.
_ALWAYS_SECTIONS = frozenset({
    "user_model", "ego_notepad", "directives", "output_contract",
})


def _make_weights(overrides: dict[str, str]) -> dict[str, str]:
    """Build a full weight dict: always sections stay always, rest uses overrides."""
    weights: dict[str, str] = {}
    for section in _ALL_SECTIONS:
        if section in _ALWAYS_SECTIONS:
            weights[section] = "always"
        else:
            weights[section] = overrides.get(section, "deep")
    return weights


# Default: all sections at "deep" (backward compatible with current behavior).
_DEFAULT_WEIGHTS = _make_weights({})

FOCUS_CONTEXT_WEIGHTS: dict[str, dict[str, str]] = {
    # Proactive: breadth scan, everything deep (same as current)
    "proactive": _DEFAULT_WEIGHTS,

    # Daily briefing: needs full breadth for the morning report
    "daily_briefing": _DEFAULT_WEIGHTS,

    # Reactive: health/escalations deep, others light
    "reactive": _make_weights({
        "escalations": "deep",
        "execution_outcomes": "deep",
        "proposal_board": "deep",
        "goals": "light",
        "goal_progress": "light",
        "world_snapshot": "light",
        "activity_pulse": "light",
        "recent_conversations": "light",
        "backlog_summary": "light",
        "follow_ups": "light",
        "proposal_history": "light",
        "capabilities": "skip",
        "capability_performance": "skip",
        "autonomy_readiness": "skip",
        "recurring_patterns": "skip",
    }),

    # Goal review: goals/progress/outcomes deep, others light
    "goal_review": _make_weights({
        "goals": "deep",
        "goal_progress": "deep",
        "execution_outcomes": "deep",
        "follow_ups": "deep",
        "proposal_board": "deep",
        "world_snapshot": "light",
        "activity_pulse": "light",
        "recent_conversations": "light",
        "backlog_summary": "light",
        "escalations": "light",
        "proposal_history": "light",
        "capabilities": "skip",
        "capability_performance": "skip",
        "autonomy_readiness": "skip",
        "recurring_patterns": "skip",
    }),

    # Dispatch outcome: outcomes/goals deep, most others skip
    "dispatch_outcome": _make_weights({
        "execution_outcomes": "deep",
        "goals": "deep",
        "goal_progress": "deep",
        "proposal_board": "deep",
        "follow_ups": "light",
        "world_snapshot": "skip",
        "activity_pulse": "skip",
        "recent_conversations": "skip",
        "backlog_summary": "skip",
        "escalations": "light",
        "proposal_history": "skip",
        "capabilities": "skip",
        "capability_performance": "skip",
        "autonomy_readiness": "skip",
        "recurring_patterns": "skip",
    }),

    # Escalation: escalations deep, system context deep, others light
    "escalation": _make_weights({
        "escalations": "deep",
        "execution_outcomes": "deep",
        "proposal_board": "deep",
        "goals": "light",
        "goal_progress": "light",
        "world_snapshot": "light",
        "activity_pulse": "light",
        "recent_conversations": "light",
        "backlog_summary": "light",
        "follow_ups": "light",
        "proposal_history": "light",
        "capabilities": "skip",
        "capability_performance": "skip",
        "autonomy_readiness": "skip",
        "recurring_patterns": "skip",
    }),
}


# ---------------------------------------------------------------------------
# Focus selector
# ---------------------------------------------------------------------------

# Regex to extract JSON from LLM response (non-greedy to avoid matching
# across multiple JSON objects or trailing text with braces).
_JSON_RE = re.compile(r"\{.*?\}", re.DOTALL)


class FocusSelector:
    """Selects what the ego should focus on from a batch of signals.

    Optimizations that skip the LLM call:
    - Single signal → select directly
    - Critical signal present → preempt (highest-priority critical)
    - LLM call only fires for multi-signal arbitration

    Parameters
    ----------
    router:
        Router instance for ``route_call()``. Same interface used by
        triage classifier, delta assessor, and 8+ other subsystems.
    """

    def __init__(self, router: _Router) -> None:
        self._router = router

    async def select(
        self,
        signals: list[EgoSignal],
        recent_focuses: list[dict[str, str]] | None = None,
    ) -> FocusResult | None:
        """Select focus from a batch of signals.

        Parameters
        ----------
        signals:
            Drained signals from the SignalQueue (already priority-sorted).
        recent_focuses:
            Last N focus outcomes for context (avoids repetition).
            Each dict has keys: focus_type, focus_id, rationale, created_at.

        Returns None if no actionable signals.
        """
        if not signals:
            return None

        # --- Shortcut: single signal → direct select ---
        if len(signals) == 1:
            sig = signals[0]
            return self._signal_to_focus(sig, rationale="only signal in queue")

        # --- Shortcut: critical signal → preempt ---
        critical = [s for s in signals if s.priority == "critical"]
        if critical:
            sig = critical[0]  # Already priority-sorted
            return self._signal_to_focus(
                sig,
                rationale=f"critical signal preemption ({len(signals)} total)",
                consumed_ids=[s.id for s in signals],
            )

        # --- Multi-signal: LLM classification ---
        return await self._llm_select(signals, recent_focuses or [])

    async def _llm_select(
        self,
        signals: list[EgoSignal],
        recent_focuses: list[dict[str, str]],
    ) -> FocusResult:
        """Use router.route_call() for multi-signal focus selection."""
        prompt = self._build_prompt(signals, recent_focuses)
        messages = [{"role": "user", "content": prompt}]

        try:
            result = await self._router.route_call(_CALL_SITE, messages)

            if not result.success or not result.content:
                logger.warning(
                    "Focus selector route_call failed — falling back to highest priority",
                )
                return self._fallback(signals, cost=getattr(result, "cost_usd", 0.0))

            parsed = self._parse_response(result.content)
            if parsed is None:
                logger.warning(
                    "Focus selector parse failed — falling back to highest priority",
                )
                return self._fallback(signals, cost=getattr(result, "cost_usd", 0.0))

            # Build FocusResult from parsed response
            focus_type = parsed.get("focus_type", signals[0].focus_category)
            focus_id = parsed.get("focus_id")
            rationale = parsed.get("rationale", "LLM-selected focus")
            consumed = parsed.get("signals_consumed", [s.id for s in signals])

            return FocusResult(
                focus_type=focus_type,
                focus_id=focus_id,
                rationale=rationale,
                signals_consumed=consumed if isinstance(consumed, list) else [consumed],
                context_weights=self.get_context_weights(focus_type),
                perceive_cost_usd=getattr(result, "cost_usd", 0.0),
            )

        except Exception:
            logger.warning(
                "Focus selector error — falling back to highest priority",
                exc_info=True,
            )
            return self._fallback(signals)

    def _build_prompt(
        self,
        signals: list[EgoSignal],
        recent_focuses: list[dict[str, str]],
    ) -> str:
        """Build classification prompt for the focus selector."""
        parts = [
            "You are the focus selector for an autonomous AI agent's cognitive loop.",
            "Given the pending signals below, choose which one the agent should focus on.",
            "",
            "## Pending Signals",
            "",
        ]

        for i, sig in enumerate(signals, 1):
            parts.append(
                f"{i}. [{sig.priority.upper()}] "
                f"({sig.focus_category}) {sig.summary}"
            )
            if sig.focus_id:
                parts.append(f"   Target: {sig.focus_id}")

        if recent_focuses:
            parts.append("")
            parts.append("## Recent Focuses (avoid repetition)")
            parts.append("")
            for rf in recent_focuses[-5:]:
                parts.append(
                    f"- {rf.get('focus_type', '?')}: "
                    f"{rf.get('rationale', '?')[:80]}"
                )

        parts.extend([
            "",
            "## Instructions",
            "",
            "Choose the signal that deserves the agent's attention right now.",
            "Consider: priority, urgency, staleness, and avoid repeating recent focuses.",
            "",
            "Respond with JSON only:",
            '{"focus_type": "<category>", "focus_id": "<target or null>", '
            '"rationale": "<brief reason>", "signals_consumed": ["<signal_ids>"]}',
        ])

        return "\n".join(parts)

    def _parse_response(self, content: str) -> dict | None:
        """Parse JSON from LLM response (same fallback pattern as session.py)."""
        # Try direct parse
        try:
            return json.loads(content)
        except (json.JSONDecodeError, TypeError):
            pass

        # Try regex extraction
        match = _JSON_RE.search(content)
        if match:
            try:
                return json.loads(match.group())
            except (json.JSONDecodeError, TypeError):
                pass

        return None

    def _signal_to_focus(
        self,
        signal: EgoSignal,
        rationale: str,
        consumed_ids: list[str] | None = None,
    ) -> FocusResult:
        """Convert a single signal to a FocusResult (no LLM call)."""
        return FocusResult(
            focus_type=signal.focus_category,
            focus_id=signal.focus_id,
            rationale=rationale,
            signals_consumed=consumed_ids or [signal.id],
            context_weights=self.get_context_weights(signal.focus_category),
            perceive_cost_usd=0.0,
        )

    def _fallback(
        self,
        signals: list[EgoSignal],
        cost: float = 0.0,
    ) -> FocusResult:
        """Fallback: pick the highest-priority signal (signals are pre-sorted)."""
        sig = signals[0]
        return FocusResult(
            focus_type=sig.focus_category,
            focus_id=sig.focus_id,
            rationale=f"fallback — highest priority signal ({sig.priority})",
            signals_consumed=[s.id for s in signals],
            context_weights=self.get_context_weights(sig.focus_category),
            perceive_cost_usd=cost,
        )

    @staticmethod
    def get_context_weights(focus_type: str) -> dict[str, str]:
        """Look up context weights for a focus type.

        Returns all-"deep" defaults for unknown focus types.
        """
        return FOCUS_CONTEXT_WEIGHTS.get(focus_type, _DEFAULT_WEIGHTS)
