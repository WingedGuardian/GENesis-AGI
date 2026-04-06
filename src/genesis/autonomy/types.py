"""Autonomy domain types — protection levels, action classes, approval states."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum

# ---------------------------------------------------------------------------
# Protection levels — what can Genesis modify and from where?
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Enforcement spectrum — where does a rule live?
# ---------------------------------------------------------------------------

class EnforcementLayer(IntEnum):
    """7-layer enforcement spectrum from strictest to most permissive.

    Provides a taxonomy for classifying all Genesis enforcement mechanisms.
    When adding a new rule or constraint, use this to decide where it belongs:

    HARD_BLOCK (7)     — Framework intercept, exit code 2. Unpromptable.
                         E.g. behavioral linter blocks, destructive command blocks.
    PERMISSION_GATE (6) — Requires explicit user action to proceed.
                         E.g. CRITICAL protected path blocks.
    PROPOSAL_GATE (5)  — Requires approval before acting.
                         E.g. autonomy classification for costly_reversible/irreversible.
    ADVISORY (4)       — Soft context injection, LLM can override.
                         E.g. procedure advisories, system prompt steering rules.
    DETECTION (3)      — Observation only, no enforcement.
                         E.g. confidence gates (shadow mode), content sanitization.
    AMBIENT (2)        — Always-on background classification.
                         E.g. keyword action classification, pattern matching.
    BASELINE (1)       — No enforcement — the default state.
    """

    HARD_BLOCK = 7
    PERMISSION_GATE = 6
    PROPOSAL_GATE = 5
    ADVISORY = 4
    DETECTION = 3
    AMBIENT = 2
    BASELINE = 1


# ---------------------------------------------------------------------------
# Protection levels — what can Genesis modify and from where?
# ---------------------------------------------------------------------------

class ProtectionLevel(StrEnum):
    """Path protection classification.

    CRITICAL — cannot be modified from any relay/chat channel.
    SENSITIVE — modifiable with explicit user approval + self-review.
    NORMAL — standard operation, no special gates.
    """

    CRITICAL = "critical"
    SENSITIVE = "sensitive"
    NORMAL = "normal"


# ---------------------------------------------------------------------------
# Action irreversibility — how bad is a mistake?
# ---------------------------------------------------------------------------

class ActionClass(StrEnum):
    """Irreversibility classification for autonomous actions.

    REVERSIBLE — can be undone trivially (edit file, create branch).
    COSTLY_REVERSIBLE — can be undone but with cost (send message, push code).
    IRREVERSIBLE — cannot be undone (pay money, submit form, delete external data).
    """

    REVERSIBLE = "reversible"
    COSTLY_REVERSIBLE = "costly_reversible"
    IRREVERSIBLE = "irreversible"


# ---------------------------------------------------------------------------
# Approval states
# ---------------------------------------------------------------------------

class ApprovalStatus(StrEnum):
    """Lifecycle of an approval request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ApprovalDecision(StrEnum):
    """What the autonomy gate decides for an action."""

    ACT = "act"          # Proceed without approval
    PROPOSE = "propose"  # Ask user for approval before acting
    BLOCK = "block"      # Cannot be performed in this context


# ---------------------------------------------------------------------------
# Autonomy levels and categories
# ---------------------------------------------------------------------------

class AutonomyLevel(IntEnum):
    """Autonomy hierarchy L1–L4 (V3 scope). L5–L7 deferred to V5."""

    L1 = 1  # Simple tool use — fully autonomous
    L2 = 2  # Known-pattern tasks — mostly autonomous
    L3 = 3  # Novel tasks — propose + execute with checkpoint
    L4 = 4  # Proactive outreach — threshold-gated + governance


class AutonomyCategory(StrEnum):
    """Categories of autonomous behavior, each tracked independently."""

    DIRECT_SESSION = "direct_session"
    BACKGROUND_COGNITIVE = "background_cognitive"
    SUB_AGENT = "sub_agent"
    OUTREACH = "outreach"


class ContextCeiling(StrEnum):
    """Maximum autonomy level per execution context."""

    DIRECT_SESSION = "direct_session"       # No cap (earned level)
    BACKGROUND_COGNITIVE = "background_cognitive"  # L3 max
    SUB_AGENT = "sub_agent"                 # L2 for irreversible, earned for reversible
    OUTREACH = "outreach"                   # L2 until engagement proves calibration


# Fixed ceiling values per context
CONTEXT_CEILING_MAP: dict[ContextCeiling, int] = {
    ContextCeiling.DIRECT_SESSION: 7,       # No real cap
    ContextCeiling.BACKGROUND_COGNITIVE: 3,  # L3 max
    ContextCeiling.SUB_AGENT: 2,            # L2 for irreversible
    ContextCeiling.OUTREACH: 2,             # L2 until proven
}


# ---------------------------------------------------------------------------
# Watchdog types
# ---------------------------------------------------------------------------

class WatchdogAction(StrEnum):
    """Actions the watchdog can take."""

    RESTART = "restart"           # Restart bridge
    SKIP = "skip"                # Config validation failed, don't restart
    NOTIFY = "notify"            # Alert user, don't restart
    BACKOFF = "backoff"          # Too many restarts, wait


# ---------------------------------------------------------------------------
# Structured data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProtectedPathRule:
    """A single protection rule: glob pattern → protection level."""

    pattern: str
    level: ProtectionLevel
    reason: str = ""


@dataclass(frozen=True)
class ApprovalRequest:
    """A request for user approval before executing an action."""

    id: str
    action_type: str
    action_class: ActionClass
    description: str
    context: dict = field(default_factory=dict)
    status: ApprovalStatus = ApprovalStatus.PENDING
    timeout_seconds: int | None = None
    created_at: str = ""
    resolved_at: str | None = None
    resolved_by: str | None = None


@dataclass(frozen=True)
class AutonomyState:
    """Current autonomy state for a category."""

    id: str
    category: AutonomyCategory
    current_level: AutonomyLevel = AutonomyLevel.L1
    earned_level: AutonomyLevel = AutonomyLevel.L1
    consecutive_corrections: int = 0
    total_successes: int = 0
    total_corrections: int = 0


@dataclass(frozen=True)
class CompletionArtifact:
    """Structured output from a completed task — verification gate input."""

    task_id: str
    what_attempted: str
    what_produced: str
    success: bool
    learnings: str = ""
    error: str | None = None
    outputs: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EscalationReport:
    """Report surfaced to user when Genesis hits a blocker."""

    task_id: str
    attempts: list[str] = field(default_factory=list)
    final_blocker: str = ""
    alternatives_considered: list[str] = field(default_factory=list)
    help_needed: str = ""


@dataclass(frozen=True)
class RateLimitEvent:
    """Detected CC rate limit — relayed to user."""

    limit_type: str   # "session", "weekly", "monthly"
    resume_at: str     # ISO timestamp or human-readable
    raw_message: str = ""


@dataclass(frozen=True)
class TaskModelConfig:
    """Default model/effort for a task type. User override always wins."""

    task_type: str
    model: str       # CCModel value
    effort: str      # EffortLevel value


# Default model/effort per task type (lookup table).
# User can always override for user-initiated tasks.
DEFAULT_TASK_MODEL_MAP: dict[str, TaskModelConfig] = {
    "deep_reflection": TaskModelConfig("deep_reflection", "sonnet", "high"),
    "strategic_reflection": TaskModelConfig("strategic_reflection", "opus", "high"),
    "inbox_evaluation": TaskModelConfig("inbox_evaluation", "sonnet", "medium"),
    "research": TaskModelConfig("research", "sonnet", "high"),
    "code_modification": TaskModelConfig("code_modification", "opus", "high"),
}
