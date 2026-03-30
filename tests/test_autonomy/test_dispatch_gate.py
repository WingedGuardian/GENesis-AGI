"""Tests for pre-dispatch validation gate."""
from genesis.autonomy.dispatch_gate import check_dispatch_preconditions
from genesis.autonomy.types import ActionClass, ApprovalDecision


def test_blocks_above_ceiling():
    """BACKGROUND_COGNITIVE ceiling is L3, request L4 → BLOCK."""
    result = check_dispatch_preconditions(
        "background_cognitive", required_level=4,
        action_class=ActionClass.REVERSIBLE, earned_level=4,
    )
    assert result == ApprovalDecision.BLOCK


def test_allows_within_ceiling():
    result = check_dispatch_preconditions(
        "background_cognitive", required_level=2,
        action_class=ActionClass.REVERSIBLE, earned_level=3,
    )
    assert result == ApprovalDecision.ACT


def test_irreversible_requires_proposal():
    result = check_dispatch_preconditions(
        "direct_session", required_level=2,
        action_class=ActionClass.IRREVERSIBLE, earned_level=4,
    )
    assert result == ApprovalDecision.PROPOSE


def test_blocks_above_earned_level():
    result = check_dispatch_preconditions(
        "direct_session", required_level=3,
        action_class=ActionClass.REVERSIBLE, earned_level=2,
    )
    assert result == ApprovalDecision.BLOCK


def test_costly_reversible_high_level_proposes():
    result = check_dispatch_preconditions(
        "direct_session", required_level=3,
        action_class=ActionClass.COSTLY_REVERSIBLE, earned_level=4,
    )
    assert result == ApprovalDecision.PROPOSE


def test_unknown_category_blocks():
    result = check_dispatch_preconditions(
        "nonexistent", required_level=1,
        action_class=ActionClass.REVERSIBLE, earned_level=4,
    )
    assert result == ApprovalDecision.BLOCK
