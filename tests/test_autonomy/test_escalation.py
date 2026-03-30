"""Tests for genesis.autonomy.escalation.EscalationManager."""

from __future__ import annotations

import pytest

from genesis.autonomy.escalation import EscalationManager


@pytest.fixture
def mgr():
    return EscalationManager()


def test_max_retries_l1(mgr):
    assert mgr.max_retries(1) == 2


def test_max_retries_l2(mgr):
    assert mgr.max_retries(2) == 2


def test_max_retries_l3(mgr):
    assert mgr.max_retries(3) == 4


def test_max_retries_l4(mgr):
    assert mgr.max_retries(4) == 4


def test_should_escalate_true(mgr):
    assert mgr.should_escalate(1, 2) is True


def test_should_escalate_false(mgr):
    assert mgr.should_escalate(1, 1) is False


def test_build_report_success(mgr):
    report = mgr.build_escalation_report(
        task_id="task-1",
        attempts=["tried A", "tried B"],
        final_blocker="API key missing",
        alternatives_considered=["use cached data"],
        help_needed="Please provide API key",
    )
    assert report.task_id == "task-1"
    assert len(report.attempts) == 2
    assert report.final_blocker == "API key missing"
    assert report.help_needed == "Please provide API key"


def test_build_report_missing_blocker_raises(mgr):
    with pytest.raises(ValueError, match="final_blocker"):
        mgr.build_escalation_report(
            task_id="task-1",
            attempts=[],
            final_blocker="",
            alternatives_considered=[],
            help_needed="need help",
        )


def test_build_report_missing_help_raises(mgr):
    with pytest.raises(ValueError, match="help_needed"):
        mgr.build_escalation_report(
            task_id="task-1",
            attempts=[],
            final_blocker="blocked",
            alternatives_considered=[],
            help_needed="",
        )


def test_format_message_includes_all_sections(mgr):
    report = mgr.build_escalation_report(
        task_id="task-42",
        attempts=["attempt 1", "attempt 2"],
        final_blocker="no credentials",
        alternatives_considered=["skip step", "use fallback"],
        help_needed="provide creds",
    )
    msg = mgr.format_escalation_message(report)
    assert "task-42" in msg
    assert "attempt 1" in msg
    assert "attempt 2" in msg
    assert "no credentials" in msg
    assert "skip step" in msg
    assert "use fallback" in msg
    assert "provide creds" in msg
    assert "What I tried" in msg
    assert "blocking" in msg.lower()
    assert "Alternatives" in msg
    assert "unblock" in msg.lower()
