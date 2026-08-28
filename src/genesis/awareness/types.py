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

    def __post_init__(self) -> None:
        # The free-text fields are rendered VERBATIM, one line per signal, into the
        # reflection prompt (awareness/signal_format.py) AND the user-ego prompt
        # (ego/user_context.py reloads the raw note from awareness_ticks.signals_json)
        # under a "these are the ONLY signals you may cite" instruction. A newline (or
        # Unicode line separator, or bidi override) forges or conceals an authoritative
        # signal line. Normalizing here — the single construction choke point every
        # reader flows through — closes that LINE-FORGING class for every render path
        # (present and future) and keeps the stored DB row clean. No-op on already-
        # single-line values.
        #
        # Scope: this does NOT resist semantic injection via purely-printable text on a
        # signal's own legitimate line (e.g. a crafted campaign-derived job name); that
        # is defended at the input boundary (campaign-name validation), not here.
        # `metadata` is intentionally NOT sanitized — no render path surfaces it (the
        # tick serializer and signal_format both drop it), a contract locked by
        # tests/test_awareness/test_signal_format.py::test_metadata_not_rendered.
        from genesis.security.sanitizer import strip_control_chars

        object.__setattr__(self, "name", strip_control_chars(self.name))
        object.__setattr__(self, "source", strip_control_chars(self.source))
        if self.baseline_note is not None:
            object.__setattr__(self, "baseline_note", strip_control_chars(self.baseline_note))


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
