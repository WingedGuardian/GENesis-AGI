"""Outreach domain types — enums, requests, results."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class OutreachCategory(StrEnum):
    """Outreach message categories. Must match DB CHECK constraint on outreach_history."""
    BLOCKER = "blocker"
    ALERT = "alert"
    SURPLUS = "surplus"
    DIGEST = "digest"
    # Autonomous CLI approval prompts. Routed to the "Approvals" supergroup
    # topic with inline ✅ buttons. Added via the outreach_history CHECK
    # constraint migration in db/schema/_migrations.py (_migrate_add_columns).
    APPROVAL = "approval"
    # Content pipeline drafts for user review. Routed to the "Content Review"
    # supergroup topic. User approves before external publishing.
    CONTENT = "content"


class OutreachStatus(StrEnum):
    PENDING = "pending"
    REJECTED = "rejected"
    DRAFTED = "drafted"
    DELIVERED = "delivered"
    ENGAGED = "engaged"
    IGNORED = "ignored"
    FAILED = "failed"


class GovernanceVerdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    BYPASS = "bypass"


@dataclass(frozen=True)
class OutreachRequest:
    category: OutreachCategory
    topic: str
    context: str
    salience_score: float
    signal_type: str = ""
    channel: str | None = None
    drive_alignment: str | None = None
    labeled_surplus: bool = False
    source_id: str | None = None


@dataclass(frozen=True)
class GovernanceResult:
    verdict: GovernanceVerdict
    reason: str
    checks_passed: list[str] = field(default_factory=list)
    checks_failed: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OutreachResult:
    outreach_id: str
    status: OutreachStatus
    channel: str
    message_content: str
    delivery_id: str | None = None
    governance_result: GovernanceResult | None = None
    error: str | None = None


@dataclass(frozen=True)
class FreshEyesResult:
    approved: bool
    score: float
    reason: str
    model_used: str
