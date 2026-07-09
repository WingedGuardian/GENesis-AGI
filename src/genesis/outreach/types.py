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
    # Ego notifications — informational messages that don't need user approval.
    # Routed through the outreach pipeline with governance (dedup, rate limit,
    # quiet hours) but no approval gate. Added in PR #530.
    NOTIFICATION = "notification"


class OutreachStatus(StrEnum):
    PENDING = "pending"
    REJECTED = "rejected"
    DRAFTED = "drafted"
    DELIVERED = "delivered"
    ENGAGED = "engaged"
    IGNORED = "ignored"
    FAILED = "failed"
    # Outbound external action held by the WS-8 autonomy capability gate —
    # not delivered, pending owner approval. Distinct from FAILED (this is not
    # an error) and PENDING (not queued for automatic retry).
    HELD = "held"


# Canonical set of engagement_outcome values that count as genuine POSITIVE
# engagement, for grading and metrics. Writers drifted this vocabulary over time:
# a reply writes 'useful', the dashboard /engage endpoint writes 'engaged', and
# behavioural signals write 'acted_on'/'acknowledged'. Every consumer that scores
# "did the user engage" MUST reference this ONE set — hardcoding it per-consumer is
# exactly what caused the drift (the reconciler and the awareness collector
# compared only 'engaged' and so mis-graded every real reply; harvest omitted
# 'engaged'; the dashboard snapshot omitted acted_on/acknowledged). This is
# deliberately NOT the inverse of the outreach spam-throttle, which counts
# 'ignored' (a 24h no-reply) and is governed separately.
POSITIVE_ENGAGEMENT_OUTCOMES: frozenset[str] = frozenset(
    {"useful", "engaged", "acted_on", "acknowledged"}
)

# SQL IN-list rendering of the positive set, sorted for deterministic queries.
# Values are trusted module constants (no user input) — safe to inline into a
# query string. Every SQL consumer references this instead of re-deriving it.
POSITIVE_ENGAGEMENT_SQL_IN: str = ", ".join(
    f"'{o}'" for o in sorted(POSITIVE_ENGAGEMENT_OUTCOMES)
)


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
    # When set, overrides the pipeline's default recipient for this
    # delivery. Used by thread-aware email routing to send replies
    # to the correct per-thread recipient.
    validated_recipient: str | None = None
    # Email thread this send belongs to, when known. Carried through to
    # _deliver so the WS-8 autonomy gate can classify reply-vs-cold for the
    # capability matrix. None for non-thread / cold sends.
    thread_id: str | None = None
    # When True, `submit()` skips the LLM ContentDrafter and delivers
    # `context` verbatim (still governed, deduped, formatted). Use for
    # machine-generated FACTUAL notifications (task status, health) that must
    # be conveyed exactly and must never be creatively rewritten. Governance
    # runs before the drafter, so this only removes the LLM step. Invariant:
    # `context` should carry the message when verbatim=True; if it is empty,
    # submit() falls back to `topic` so an empty string is never delivered.
    verbatim: bool = False
    # When set, the voice (spoken-aloud) fan-out speaks THIS text instead of the
    # delivered `formatted.text`. Lets a notification carry a short, factual
    # TL;DR for the ear (no file paths / tokens / commands read aloud) while
    # the text channel keeps full detail. None → voice speaks the full text
    # (unchanged behavior for every existing caller).
    voice_text: str | None = None


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
    # Destination of the delivered message (Telegram): lets send-and-wait
    # scope standalone-text resolution to the prompt's own chat+topic.
    chat_id: str | None = None
    thread_id: int | None = None


@dataclass(frozen=True)
class FreshEyesResult:
    approved: bool
    score: float
    reason: str
    model_used: str
