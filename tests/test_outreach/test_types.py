"""Tests for outreach domain types."""

from genesis.outreach.types import (
    FreshEyesResult,
    GovernanceResult,
    GovernanceVerdict,
    OutreachCategory,
    OutreachRequest,
    OutreachResult,
    OutreachStatus,
)


def test_outreach_category_values():
    assert OutreachCategory.BLOCKER == "blocker"
    assert OutreachCategory.ALERT == "alert"
    assert OutreachCategory.SURPLUS == "surplus"
    assert OutreachCategory.DIGEST == "digest"


def test_outreach_status_values():
    assert OutreachStatus.DELIVERED == "delivered"
    assert OutreachStatus.REJECTED == "rejected"
    assert OutreachStatus.IGNORED == "ignored"


def test_governance_verdict_values():
    assert GovernanceVerdict.ALLOW == "allow"
    assert GovernanceVerdict.DENY == "deny"
    assert GovernanceVerdict.BYPASS == "bypass"


def test_outreach_request_frozen():
    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="Test insight",
        context="Some context",
        salience_score=0.8,
        signal_type="surplus_insight",
    )
    assert req.category == OutreachCategory.SURPLUS
    assert req.labeled_surplus is False


def test_governance_result():
    result = GovernanceResult(
        verdict=GovernanceVerdict.ALLOW,
        reason="all checks passed",
        checks_passed=["salience", "quiet_hours"],
        checks_failed=[],
    )
    assert result.verdict == GovernanceVerdict.ALLOW
    assert len(result.checks_passed) == 2


def test_outreach_result():
    result = OutreachResult(
        outreach_id="abc-123",
        status=OutreachStatus.DELIVERED,
        channel="telegram",
        message_content="Hello",
        delivery_id="msg-456",
    )
    assert result.delivery_id == "msg-456"


def test_fresh_eyes_result():
    result = FreshEyesResult(
        approved=True,
        score=4.0,
        reason="Relevant and actionable",
        model_used="gemini-free",
    )
    assert result.approved is True
    assert result.score == 4.0
