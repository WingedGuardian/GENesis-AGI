"""Data types for the Genesis Ego subsystem."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class ProposalStatus(StrEnum):
    """Lifecycle states for an ego proposal."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
    FAILED = "failed"


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
    proposal_expiry_minutes: int = 240  # 4 hours
    daily_budget_cap_usd: float = 10.0  # max daily ego spend
    morning_report_hour: int = 8  # 24h format, local time
    morning_report_minute: int = 0
    morning_report_timezone: str = "America/New_York"
    consecutive_failure_limit: int = 3  # circuit breaker threshold
    failure_backoff_minutes: int = 60  # pause after N failures
    batch_digest: bool = True  # send proposals as daily batch
    shadow_morning_report: bool = True  # shadow mode for morning reports


# -- Structured output contract for the ego LLM --
# The ego's system prompt mandates JSON output matching this schema.
# session.py parses and validates against this.

EGO_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["proposals", "focus_summary", "follow_ups"],
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["action_type", "action_category", "content",
                             "rationale", "confidence"],
                "properties": {
                    "action_type": {"type": "string"},
                    "action_category": {"type": "string"},
                    "content": {"type": "string"},
                    "rationale": {"type": "string"},
                    "confidence": {"type": "number"},
                    "urgency": {"type": "string",
                                "enum": ["low", "normal", "high", "critical"]},
                    "alternatives": {"type": "string"},
                },
            },
        },
        "focus_summary": {
            "type": "string",
            "description": "One-line summary of current focus for reflection injection",
        },
        "follow_ups": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Open threads to check next cycle",
        },
        "morning_report": {
            "type": "string",
            "description": "Morning report content (only on morning trigger)",
        },
    },
}
