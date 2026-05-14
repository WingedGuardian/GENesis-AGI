"""Data types for the Genesis Ego subsystem."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class CycleType(StrEnum):
    """Types of ego thinking cycles."""

    PROACTIVE = "proactive"        # Regular brainstorming (uses config model/effort)
    MORNING_REPORT = "morning_report"  # Daily briefing (always Sonnet, Low)
    REACTIVE = "reactive"          # User message response (uses config model/effort)
    ESCALATION = "escalation"      # Health/escalation eval (always Sonnet, Medium)


# Per-cycle-type model/effort OVERRIDES.  Only cycle types listed here
# bypass the ego's config.model / config.default_effort.  PROACTIVE is
# intentionally absent — it respects the per-ego config so genesis ego
# can run Sonnet while user ego runs Opus.
CYCLE_TYPE_DEFAULTS: dict[CycleType, tuple[str, str]] = {
    CycleType.MORNING_REPORT: ("sonnet", "low"),
    CycleType.ESCALATION: ("sonnet", "medium"),
    CycleType.REACTIVE: ("opus", "high"),  # fires need the best model
}


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
    ego_thinking_budget_usd: float = 10.0  # daily cap for ego cycle costs (shadow API pricing)
    ego_dispatch_budget_usd: float = 2.50  # daily cap for dispatched sessions
    morning_report_hour: int = 8  # 24h format, local time
    morning_report_minute: int = 0
    # morning_report_timezone removed — uses genesis.env.user_timezone()
    consecutive_failure_limit: int = 3  # circuit breaker threshold
    failure_backoff_minutes: int = 60  # pause after N failures
    batch_digest: bool = True  # send proposals as daily batch
    shadow_morning_report: bool = True  # shadow mode for morning reports


