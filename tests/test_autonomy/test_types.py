"""Tests for genesis.autonomy.types — enums, dataclasses, lookup tables."""

from __future__ import annotations

import pytest

from genesis.autonomy.types import (
    CONTEXT_CEILING_MAP,
    DEFAULT_TASK_MODEL_MAP,
    ActionClass,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    AutonomyCategory,
    AutonomyLevel,
    AutonomyState,
    CompletionArtifact,
    ContextCeiling,
    EscalationReport,
    ProtectedPathRule,
    ProtectionLevel,
    RateLimitEvent,
    TaskModelConfig,
    WatchdogAction,
)

# ---------------------------------------------------------------------------
# Enum value tests
# ---------------------------------------------------------------------------

class TestProtectionLevel:
    def test_values(self):
        assert ProtectionLevel.CRITICAL == "critical"
        assert ProtectionLevel.SENSITIVE == "sensitive"
        assert ProtectionLevel.NORMAL == "normal"

    def test_from_string(self):
        assert ProtectionLevel("critical") is ProtectionLevel.CRITICAL

    def test_ordering(self):
        """Enum members are comparable as strings."""
        assert ProtectionLevel.CRITICAL < ProtectionLevel.NORMAL


class TestActionClass:
    def test_values(self):
        assert ActionClass.REVERSIBLE == "reversible"
        assert ActionClass.COSTLY_REVERSIBLE == "costly_reversible"
        assert ActionClass.IRREVERSIBLE == "irreversible"


class TestApprovalStatus:
    def test_all_statuses(self):
        statuses = {s.value for s in ApprovalStatus}
        assert statuses == {"pending", "approved", "rejected", "expired", "cancelled"}


class TestApprovalDecision:
    def test_all_decisions(self):
        decisions = {d.value for d in ApprovalDecision}
        assert decisions == {"act", "propose", "block"}


class TestAutonomyLevel:
    def test_int_values(self):
        assert AutonomyLevel.L1 == 1
        assert AutonomyLevel.L4 == 4

    def test_comparison(self):
        assert AutonomyLevel.L1 < AutonomyLevel.L4

    def test_from_int(self):
        assert AutonomyLevel(3) is AutonomyLevel.L3


class TestAutonomyCategory:
    def test_values(self):
        assert AutonomyCategory.DIRECT_SESSION == "direct_session"
        assert AutonomyCategory.BACKGROUND_COGNITIVE == "background_cognitive"
        assert AutonomyCategory.SUB_AGENT == "sub_agent"
        assert AutonomyCategory.OUTREACH == "outreach"


class TestContextCeiling:
    def test_ceiling_map_complete(self):
        """Every ceiling enum value has an entry in the map."""
        for ceiling in ContextCeiling:
            assert ceiling in CONTEXT_CEILING_MAP

    def test_background_capped_at_l3(self):
        assert CONTEXT_CEILING_MAP[ContextCeiling.BACKGROUND_COGNITIVE] == 3

    def test_sub_agent_capped_at_l2(self):
        assert CONTEXT_CEILING_MAP[ContextCeiling.SUB_AGENT] == 2

    def test_outreach_capped_at_l2(self):
        assert CONTEXT_CEILING_MAP[ContextCeiling.OUTREACH] == 2

    def test_direct_session_uncapped(self):
        assert CONTEXT_CEILING_MAP[ContextCeiling.DIRECT_SESSION] >= 4


class TestWatchdogAction:
    def test_values(self):
        assert WatchdogAction.RESTART == "restart"
        assert WatchdogAction.SKIP == "skip"


# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------

class TestProtectedPathRule:
    def test_creation(self):
        rule = ProtectedPathRule(
            pattern="src/genesis/channels/**",
            level=ProtectionLevel.CRITICAL,
            reason="Bridge code — relay infrastructure",
        )
        assert rule.pattern == "src/genesis/channels/**"
        assert rule.level is ProtectionLevel.CRITICAL

    def test_frozen(self):
        rule = ProtectedPathRule(pattern="*", level=ProtectionLevel.NORMAL)
        with pytest.raises(AttributeError):
            rule.pattern = "changed"  # type: ignore[misc]

    def test_default_reason(self):
        rule = ProtectedPathRule(pattern="*", level=ProtectionLevel.NORMAL)
        assert rule.reason == ""


class TestApprovalRequest:
    def test_creation(self):
        req = ApprovalRequest(
            id="test-1",
            action_type="send_email",
            action_class=ActionClass.COSTLY_REVERSIBLE,
            description="Send reply to support ticket",
        )
        assert req.status is ApprovalStatus.PENDING
        assert req.action_class is ActionClass.COSTLY_REVERSIBLE

    def test_defaults(self):
        req = ApprovalRequest(
            id="t", action_type="x", action_class=ActionClass.REVERSIBLE, description="d"
        )
        assert req.context == {}
        assert req.timeout_seconds is None
        assert req.resolved_at is None


class TestAutonomyState:
    def test_creation(self):
        state = AutonomyState(
            id="auto-1",
            category=AutonomyCategory.DIRECT_SESSION,
            current_level=AutonomyLevel.L3,
            earned_level=AutonomyLevel.L3,
        )
        assert state.current_level == 3
        assert state.earned_level == 3
        assert state.consecutive_corrections == 0

    def test_defaults(self):
        state = AutonomyState(id="a", category=AutonomyCategory.OUTREACH)
        assert state.current_level is AutonomyLevel.L1
        assert state.total_successes == 0


class TestCompletionArtifact:
    def test_creation(self):
        artifact = CompletionArtifact(
            task_id="task-1",
            what_attempted="Research topic X",
            what_produced="Summary document",
            success=True,
            learnings="Topic X is more complex than expected",
        )
        assert artifact.success is True
        assert artifact.error is None

    def test_failure(self):
        artifact = CompletionArtifact(
            task_id="task-2",
            what_attempted="Run migration",
            what_produced="",
            success=False,
            error="Permission denied",
        )
        assert artifact.success is False
        assert artifact.error == "Permission denied"


class TestEscalationReport:
    def test_creation(self):
        report = EscalationReport(
            task_id="task-1",
            attempts=["Tried approach A", "Tried approach B"],
            final_blocker="API key missing",
            help_needed="Need API key for service X",
        )
        assert len(report.attempts) == 2
        assert report.final_blocker == "API key missing"


class TestRateLimitEvent:
    def test_creation(self):
        event = RateLimitEvent(
            limit_type="session",
            resume_at="2026-03-14T03:00:00Z",
            raw_message="Rate limit exceeded, try again at 03:00 UTC",
        )
        assert event.limit_type == "session"


class TestTaskModelConfig:
    def test_creation(self):
        config = TaskModelConfig(task_type="research", model="sonnet", effort="high")
        assert config.model == "sonnet"

    def test_default_map_coverage(self):
        """Default map covers the expected task types."""
        expected = {
            "deep_reflection", "strategic_reflection", "surplus_brainstorm",
            "inbox_evaluation", "research", "code_modification",
        }
        assert set(DEFAULT_TASK_MODEL_MAP.keys()) == expected

    def test_strategic_uses_opus(self):
        assert DEFAULT_TASK_MODEL_MAP["strategic_reflection"].model == "opus"

    def test_surplus_uses_sonnet_medium(self):
        cfg = DEFAULT_TASK_MODEL_MAP["surplus_brainstorm"]
        assert cfg.model == "sonnet"
        assert cfg.effort == "medium"
