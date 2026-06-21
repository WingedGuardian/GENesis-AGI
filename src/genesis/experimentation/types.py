"""Types for the cognitive experimentation harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CognitiveVariant:
    """One arm of a cognitive A/B — a named overlay of one cognitive knob.

    Exactly one knob field should be set per variant for a clean experiment:
    - ``system_prompt``: the reflection-prompt A/B (Target 1) — the full
      (deep) reflection system prompt for this arm.
    - ``signal_weight_overrides``: the awareness signal-weight A/B (Target 2,
      a deterministic divergence report) — ``{signal_name: weight}`` overrides.
    """

    name: str
    description: str = ""
    system_prompt: str | None = None
    signal_weight_overrides: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArmResult:
    """Per-arm outcome of an experiment run."""

    variant_name: str
    case_scores: list[float]  # per-case judge score (0-1) — the primary signal
    case_results: list[bool]  # per-case pass/fail at the rubric threshold — context
    n_pass: int
    mean_score: float
    run_id: str | None = None  # eval_runs.id when persisted


@dataclass(frozen=True)
class ExperimentResult:
    """The full result of a control-vs-treatment experiment."""

    experiment_name: str
    control: ArmResult
    treatment: ArmResult
    winrate: dict[str, Any]  # genesis.eval.stats.compute_winrate output
    n_cases: int
    errors: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
