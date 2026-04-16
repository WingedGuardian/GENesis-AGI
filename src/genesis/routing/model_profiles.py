"""Model intelligence profiles — structured model capabilities for routing decisions.

Loads config/model_profiles.yaml and provides a matcher for finding the best model
for a given task type, respecting tier requirements, cost constraints, and
circuit breaker health.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Tier ordering for scoring: S=4, A=3, B=2, C=1
TIER_SCORES: dict[str, int] = {"S": 4, "A": 3, "B": 2, "C": 1}

# Cost tier ordering for filtering
COST_TIERS: dict[str, int] = {"free": 0, "cheap": 1, "moderate": 2, "expensive": 3}

# Task-type weights: which capabilities matter most for each task type.
# Format: {task_type: (reasoning_weight, instruction_weight, anti_syc_weight)}
TASK_WEIGHTS: dict[str, tuple[float, float, float]] = {
    "deep_reflection": (3.0, 1.0, 1.5),
    "strategic_reflection": (3.0, 1.0, 2.0),
    "foreground_conversation": (1.5, 2.0, 1.5),
    "inbox_assessment": (1.5, 2.0, 1.0),
    "adversarial_counterargument": (2.0, 1.0, 3.0),
    "quality_calibration": (2.5, 1.0, 2.5),
    "fresh_eyes_review": (2.0, 1.0, 2.5),
    "task_retrospective": (2.0, 1.5, 1.0),
    "outcome_classification": (1.5, 2.0, 1.0),
    "memory_consolidation": (1.0, 2.0, 0.5),
    "fact_extraction": (1.0, 2.0, 0.5),
    "triage": (1.5, 1.5, 0.5),
    "tagging": (1.0, 2.0, 0.5),
    "surplus_brainstorm": (1.5, 1.0, 1.0),
    "outreach_draft": (1.0, 2.0, 0.5),
    "content_draft": (1.0, 2.0, 0.5),
    "micro_reflection": (1.0, 1.5, 0.5),
    "light_reflection": (1.5, 1.5, 1.0),
    "code_work": (2.5, 1.5, 1.0),
    "task_execution": (2.0, 2.0, 1.0),
    "research_synthesis": (2.0, 1.5, 1.0),
}

# Default weights when task_type not in TASK_WEIGHTS
_DEFAULT_WEIGHTS = (1.5, 1.5, 1.0)


@dataclass(frozen=True)
class ModelProfile:
    """Structured capabilities for a single model."""

    name: str
    display_name: str
    provider: str
    api_id: str
    intelligence_tier: str
    reasoning: str
    instruction_following: str
    anti_sycophancy: str
    context_window: int
    cost_tier: str
    cost_per_mtok_in: float
    cost_per_mtok_out: float
    latency: str
    strengths: tuple[str, ...] = ()
    weaknesses: tuple[str, ...] = ()
    best_for: tuple[str, ...] = ()
    avoid_for: tuple[str, ...] = ()
    free_tier: dict = field(default_factory=lambda: {"available": False})
    last_reviewed: str = ""
    review_source: str = "manual"


class ModelProfileRegistry:
    """Loads model profiles from YAML and provides a capability-aware matcher."""

    def __init__(self, profiles_path: Path | str) -> None:
        self._path = Path(profiles_path)
        self._profiles: dict[str, ModelProfile] = {}

    def load(self) -> None:
        """Load profiles from the YAML file."""
        if not self._path.exists():
            logger.warning("Model profiles not found: %s", self._path)
            return

        raw = yaml.safe_load(self._path.read_text())
        if not isinstance(raw, dict) or "profiles" not in raw:
            logger.warning("Model profiles YAML missing 'profiles' key")
            return

        self._profiles.clear()
        for name, p in raw["profiles"].items():
            try:
                self._profiles[name] = ModelProfile(
                    name=name,
                    display_name=p.get("display_name", name),
                    provider=p.get("provider", "unknown"),
                    api_id=p.get("api_id", ""),
                    intelligence_tier=p.get("intelligence_tier", "C"),
                    reasoning=p.get("reasoning", "C"),
                    instruction_following=p.get("instruction_following", "C"),
                    anti_sycophancy=p.get("anti_sycophancy", "C"),
                    context_window=p.get("context_window", 0),
                    cost_tier=p.get("cost_tier", "moderate"),
                    cost_per_mtok_in=float(p.get("cost_per_mtok_in", 0)),
                    cost_per_mtok_out=float(p.get("cost_per_mtok_out", 0)),
                    latency=p.get("latency", "moderate"),
                    strengths=tuple(p.get("strengths") or []),
                    weaknesses=tuple(p.get("weaknesses") or []),
                    best_for=tuple(p.get("best_for") or []),
                    avoid_for=tuple(p.get("avoid_for") or []),
                    free_tier=p.get("free_tier") or {"available": False},
                    last_reviewed=p.get("last_reviewed", ""),
                    review_source=p.get("review_source", "manual"),
                )
            except Exception:
                logger.exception("Failed to parse profile '%s'", name)

        logger.info("Loaded %d model profiles from %s", len(self._profiles), self._path)

    def get(self, model_name: str) -> ModelProfile | None:
        """Get a profile by name."""
        return self._profiles.get(model_name)

    def all_profiles(self) -> dict[str, ModelProfile]:
        """Return all loaded profiles."""
        return dict(self._profiles)

    def match(
        self,
        task_type: str,
        *,
        min_reasoning: str = "C",
        min_anti_sycophancy: str = "C",
        max_cost_tier: str = "expensive",
        require_free: bool = False,
        exclude_providers: list[str] | None = None,
        circuit_breakers: object | None = None,
    ) -> list[ModelProfile]:
        """Find models matching requirements, ranked by suitability for task_type.

        Args:
            task_type: The type of work (e.g. 'deep_reflection', 'triage').
            min_reasoning: Minimum reasoning tier (S/A/B/C).
            min_anti_sycophancy: Minimum anti-sycophancy tier.
            max_cost_tier: Maximum acceptable cost tier.
            require_free: If True, only include models with free tier available.
            exclude_providers: Provider names to exclude.
            circuit_breakers: Optional CircuitBreakerRegistry; unhealthy providers excluded.

        Returns:
            List of ModelProfile sorted by score (best first).
        """
        exclude = set(exclude_providers or [])
        min_r = TIER_SCORES.get(min_reasoning, 1)
        min_a = TIER_SCORES.get(min_anti_sycophancy, 1)
        max_c = COST_TIERS.get(max_cost_tier, 3)

        candidates: list[tuple[float, ModelProfile]] = []
        for profile in self._profiles.values():
            # Filter: avoid_for
            if task_type in profile.avoid_for:
                continue

            # Filter: provider exclusion
            if profile.provider in exclude:
                continue

            # Filter: minimum tiers
            if TIER_SCORES.get(profile.reasoning, 1) < min_r:
                continue
            if TIER_SCORES.get(profile.anti_sycophancy, 1) < min_a:
                continue

            # Filter: cost
            if COST_TIERS.get(profile.cost_tier, 3) > max_c:
                continue

            # Filter: free tier
            if require_free and not profile.free_tier.get("available", False):
                continue

            # Filter: circuit breaker health
            if circuit_breakers is not None:
                try:
                    cb = circuit_breakers.get(profile.provider)
                    if cb is not None and not cb.is_available():
                        continue
                except Exception:
                    pass  # If CB check fails, don't exclude

            # Score
            weights = TASK_WEIGHTS.get(task_type, _DEFAULT_WEIGHTS)
            score = (
                TIER_SCORES.get(profile.reasoning, 1) * weights[0]
                + TIER_SCORES.get(profile.instruction_following, 1) * weights[1]
                + TIER_SCORES.get(profile.anti_sycophancy, 1) * weights[2]
            )

            # Bonus for best_for match
            if task_type in profile.best_for:
                score += 2.0

            candidates.append((score, profile))

        # Sort by score descending, then by cost ascending as tiebreaker
        candidates.sort(
            key=lambda x: (-x[0], COST_TIERS.get(x[1].cost_tier, 3)),
        )
        return [profile for _, profile in candidates]

    def stale_profiles(self, days: int = 30) -> list[ModelProfile]:
        """Find profiles not reviewed within the given number of days."""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        stale: list[ModelProfile] = []
        for profile in self._profiles.values():
            if not profile.last_reviewed:
                stale.append(profile)
                continue
            try:
                reviewed = datetime.strptime(
                    profile.last_reviewed, "%Y-%m-%d"
                ).replace(tzinfo=UTC)
                if reviewed < cutoff:
                    stale.append(profile)
            except ValueError:
                stale.append(profile)
        return stale
