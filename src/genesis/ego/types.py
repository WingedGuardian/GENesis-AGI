"""Data types for the Genesis Ego subsystem."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class FocusCategory(StrEnum):
    """Focus categories for the unified cognitive loop.

    Signals carry a focus_category that determines context weighting
    and prompt specialization.
    """

    PROACTIVE = "proactive"
    DAILY_BRIEFING = "daily_briefing"
    REACTIVE = "reactive"
    GOAL_REVIEW = "goal_review"
    DISPATCH_OUTCOME = "dispatch_outcome"
    ESCALATION = "escalation"


class ProposalStatus(StrEnum):
    """Lifecycle states for an ego proposal."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
    FAILED = "failed"
    TABLED = "tabled"
    WITHDRAWN = "withdrawn"


# Neutral status labels — factual lifecycle terms without judgment language.
# Used in ego context to avoid LLM deference bias from loaded outcomes.
NEUTRAL_STATUS: dict[str, str] = {
    "pending": "pending",
    "approved": "approved",
    "rejected": "passed on",
    "expired": "expired",
    "tabled": "deferred",
    "withdrawn": "recycled",
    "executed": "completed",
    "failed": "attempted",
    "cancelled": "cancelled",
}


class ProposalUrgency(StrEnum):
    """Urgency levels for proposals."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True)
class EgoProposal:
    """A single action the ego wants to take, pending user approval."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    action_type: str = ""  # e.g., "investigate", "outreach", "maintenance"
    action_category: str = ""  # for per-category graduation tracking
    content: str = ""  # what the ego wants to do
    rationale: str = ""  # why
    confidence: float = 0.0  # 0.0-1.0
    urgency: str = ProposalUrgency.NORMAL
    alternatives: str = ""  # what else was considered
    memory_basis: str = ""  # non-obvious memory that informed this proposal
    status: str = ProposalStatus.PENDING
    user_response: str | None = None  # rejection reason, etc.
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    resolved_at: str | None = None
    cycle_id: str | None = None  # FK to ego_cycles


@dataclass(frozen=True)
class EgoCycle:
    """Record of one ego thinking cycle."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    output_text: str = ""  # full ego output (reasoning + decisions)
    proposals_json: str = "[]"  # JSON array of proposals from this cycle
    focus_summary: str = ""  # one-line summary for reflection injection
    model_used: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    compacted_into: str | None = None  # set when folded into compacted summary
    ego_source: str = ""  # 'user_ego_cycle' or 'genesis_ego_cycle'


# Goal-pursuit (Phase 6): a still-active goal that is stale AND has accumulated
# this many *executed* proposals is "stuck" (effort spent, no progress) rather
# than merely "stale" (untouched). Drives a higher-priority goal_review signal
# + a stuck-diagnosis prompt. A heuristic — framed to the ego as a hypothesis to
# diagnose, not a verdict. Single source for both cadence + context-builder.
GOAL_STUCK_EXECUTED_THRESHOLD = 2


@dataclass
class EgoConfig:
    """Runtime configuration for the ego subsystem.

    All fields are configurable via config/ego.yaml and dashboard.
    """

    enabled: bool = True
    cadence_minutes: int = 60  # base interval between cycles
    activity_threshold_minutes: int = 30  # min time since last user msg
    max_interval_minutes: int = 240  # backoff cap (4 hours)
    backoff_multiplier: float = 2.0  # interval doubles on idle cycles
    model: str = "opus"  # default model for ego cycles
    default_effort: str = "high"  # effort for regular cycles
    morning_report_effort: str = "low"  # effort for morning reports
    morning_report_enabled: bool = True  # set False for Genesis ego
    board_size: int = 3  # max active proposals on the board
    morning_report_hour: int = 8  # 24h format, local time
    morning_report_minute: int = 0
    # morning_report_timezone removed — uses genesis.env.user_timezone()
    consecutive_failure_limit: int = 3  # circuit breaker threshold
    failure_backoff_minutes: int = 60  # pause after N failures
    batch_digest: bool = True  # send proposals as daily batch
    shadow_morning_report: bool = True  # shadow mode for morning reports
    # Unified cognitive loop — goal review staleness threshold
    goal_review_staleness_days: int = 10  # trigger goal_review after N days without progress
    # Genesis ego (COO) independent scheduling — defaults match user ego
    genesis_cadence_minutes: int = 90  # base interval for genesis ego
    genesis_max_interval_minutes: int = 240  # backoff ceiling for genesis ego
    max_pending_proposals: int = 15  # auto-table oldest unranked when exceeded
    # Additive ego autonomy — cap on ACTIVE goals in the genesis ego's OWN
    # lane (origin='genesis_ego'). Pausing frees a slot; the paused tail is
    # deliberately unbounded (user decision 2026-07-16) and reported in the
    # morning report's own-goals count line.
    max_active_ego_goals: int = 5
    # Per-action-type model overrides for proposal dispatch.
    # Keys are action_type strings (e.g. "investigate"), values are model
    # names ("opus", "sonnet", "haiku").  Falls back to profile-based
    # selection when the action_type is not listed.
    dispatch_model_overrides: dict = field(default_factory=dict)
    # Inject the ego's own confidence calibration into its context so it can
    # self-correct (informational, never a mechanical rescale). Live-read each
    # cycle; default ON. Genesis ego only for now.
    calibration_injection_enabled: bool = True
    # LC3-B go-live gate: fold Outcome Bus tier-1 execution ground truth
    # (source='surplus') into the capability map as a 6th aggregator source.
    # Default OFF — until an operator flips this, the capability map is computed
    # from the original 5 sources and behaviour is unchanged. capability_map is
    # display-only (rendered into ego-context sections, no code gate), so even ON
    # only nudges the numbers the ego sees about itself. Live-read each refresh.
    outcome_bus_capability_feed: bool = False
    # Quiet-hours floor (circadian model): during the overnight window, throttle
    # PROACTIVE ticks to at most one per quiet_hours_min_interval_minutes. Morning
    # report, reactive, and escalation paths are never gated by this. Local time
    # via genesis.env.user_timezone(). A window with start==end is treated as off.
    quiet_hours_enabled: bool = True
    quiet_hours_start: int = 23  # local hour [0-23], inclusive
    quiet_hours_end: int = 7  # local hour [0-23], exclusive
    quiet_hours_min_interval_minutes: int = 240  # min gap between overnight ticks
    quiet_hours_mode: str = "floor"  # "floor" (throttle) | "suppress" (silence window)


