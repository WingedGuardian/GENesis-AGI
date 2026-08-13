"""Data types for the Awareness Loop."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Signals that track user activity/outcomes (vs Genesis infrastructure).
# Single source of truth for user-vs-genesis audience attribution — consumed
# by the perception writer (relevance tagging on micro reflections) and the
# user ego's activity-pulse section. Keep in sync with the collectors'
# signal_name values (bare names, no namespace prefix).
USER_FACING_SIGNALS = frozenset(
    {
        "conversations_since_reflection",
        "task_completion_quality",
        "recon_findings_pending",
        "stale_pending_items",
        "user_goal_staleness",
        "user_session_pattern",
        # Outreach/marketing/networking is user-world (mirrors
        # _USER_WORLD_CATEGORIES, which excludes it from the Genesis ego).
        "outreach_engagement_data",
    }
)

# Signals expected in EVERY steady-state awareness tick — the canonical set the
# j9 signal-completeness metric scores against and the collector-parity guard
# enforces. Every steady-state collector's collect() always returns a
# SignalReading (value 0.0 when idle), so every name below appears in every tick;
# a name going MISSING means a collector was dropped from the learning-phase swap
# (runtime/init/learning.py::build_learning_collectors, installed via
# AwarenessLoop.replace_collectors — a full replacement of the bootstrap set).
# Pinned to the actual builder output by
# tests/test_learning/test_extension_wiring.py — update both together.
STEADY_STATE_SIGNALS = frozenset(
    {
        "conversations_since_reflection",
        "task_completion_quality",
        "outreach_engagement_data",
        "recon_findings_pending",
        "budget_pct_consumed",
        "software_error_spike",
        "critical_failure",
        "time_since_last_strategic",
        "container_memory_pct",
        "stale_pending_items",
        "micro_count_since_light",
        "light_count_since_deep",
        "sentinel_activity",
        "guardian_activity",
        "surplus_activity",
        "autonomy_activity",
        "genesis_version_changed",
        "cc_version_changed",
        "stale_browser_processes",
        "user_goal_staleness",
        "user_session_pattern",
        "scheduled_job_health",
        "scheduler_liveness",
    }
)

# Signals produced by the bootstrap collector set (runtime/init/awareness.py)
# that are intentionally NOT carried into the steady-state swap. event_loop_latency
# is deferred: it is spiky (a single >500ms hiccup reads high) and reaches the
# ungrounded ego/dashboard signal tables, so it is restored WITH grounding as part
# of the Micro-reflection redesign, not here. The parity guard
# (tests/test_learning/test_extension_wiring.py) treats these as allowed exceptions.
BOOTSTRAP_ONLY_SIGNALS = frozenset({"event_loop_latency"})


class Depth(StrEnum):
    """Reflection depth levels. Values match DB seed data in signal_weights.feeds_depths."""

    MICRO = "Micro"
    LIGHT = "Light"
    DEEP = "Deep"
    STRATEGIC = "Strategic"


@dataclass(frozen=True)
class SignalReading:
    """A single signal measurement."""

    name: str
    value: float  # 0.0–1.0 normalized
    source: str
    collected_at: str  # ISO datetime
    failed: bool = False  # True when collector returned a stub reading on error
    # Optional threshold context for LLM consumers (signal calibration)
    normal_max: float | None = None  # values at or below this are normal
    warning_threshold: float | None = None  # values at or above this warrant attention
    critical_threshold: float | None = None  # values at or above this are critical
    baseline_note: str | None = None  # human-readable "what's normal" for LLM context
    metadata: dict | None = None  # optional diagnostic metadata (latency values, stale jobs, etc.)


@dataclass(frozen=True)
class DepthScore:
    """Urgency score for one depth level."""

    depth: Depth
    raw_score: float
    time_multiplier: float
    final_score: float  # raw_score × time_multiplier
    threshold: float
    triggered: bool  # final_score >= threshold


@dataclass(frozen=True)
class TickResult:
    """Complete result of one awareness tick."""

    tick_id: str
    timestamp: str  # ISO datetime
    source: str  # "scheduled" | "critical_bypass"
    signals: list[SignalReading]
    scores: list[DepthScore]
    classified_depth: Depth | None
    trigger_reason: str | None
    escalation_source: str | None = None
    escalation_pending_id: str | None = None  # observation ID to resolve after dispatch
    signal_staleness: dict[str, int] | None = None  # signal_name → consecutive unchanged ticks
    db_available: bool = True  # False when DB operations failed (degraded tick)
