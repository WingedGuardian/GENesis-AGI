"""Tests for genesis.autonomy.steering — SteerMessage and output formatters."""

from __future__ import annotations

import json

from genesis.autonomy.steering import SteerMessage
from genesis.autonomy.types import ApprovalDecision, EnforcementLayer


class TestSteerMessageExitCode:
    """to_exit_code() returns 0 for allow, 2 for block."""

    def test_block_returns_2(self) -> None:
        msg = _make_msg(decision=ApprovalDecision.BLOCK)
        assert msg.to_exit_code() == 2

    def test_act_returns_0(self) -> None:
        msg = _make_msg(decision=ApprovalDecision.ACT)
        assert msg.to_exit_code() == 0

    def test_propose_returns_0(self) -> None:
        msg = _make_msg(decision=ApprovalDecision.PROPOSE)
        assert msg.to_exit_code() == 0


class TestSteerMessageStderr:
    """to_stderr() produces human-readable block/warning messages."""

    def test_block_says_blocked(self) -> None:
        msg = _make_msg(decision=ApprovalDecision.BLOCK)
        stderr = msg.to_stderr()
        assert "BLOCKED" in stderr

    def test_act_says_warning(self) -> None:
        msg = _make_msg(decision=ApprovalDecision.ACT, severity="medium")
        stderr = msg.to_stderr()
        assert "WARNING" in stderr

    def test_includes_rule_id(self) -> None:
        msg = _make_msg(rule_id="no-hide-problems")
        stderr = msg.to_stderr()
        assert "no-hide-problems" in stderr

    def test_includes_file_path(self) -> None:
        msg = _make_msg(file_path="src/foo.py")
        stderr = msg.to_stderr()
        assert "src/foo.py" in stderr

    def test_includes_context(self) -> None:
        msg = _make_msg(context="Hiding error state")
        stderr = msg.to_stderr()
        assert "Hiding error state" in stderr

    def test_includes_suggestion(self) -> None:
        msg = _make_msg(suggestion="Fix the root cause")
        stderr = msg.to_stderr()
        assert "Fix the root cause" in stderr

    def test_includes_escape_hatch(self) -> None:
        msg = _make_msg(
            can_suppress=True,
            suppress_key="# behavioral-lint: ignore test-rule",
        )
        stderr = msg.to_stderr()
        assert "behavioral-lint: ignore test-rule" in stderr

    def test_omits_escape_when_not_suppressible(self) -> None:
        msg = _make_msg(can_suppress=False)
        stderr = msg.to_stderr()
        assert "Escape" not in stderr


class TestSteerMessageHookJson:
    """to_hook_json() produces valid CC PreToolUse hook contract."""

    def test_structure(self) -> None:
        msg = _make_msg()
        hook = msg.to_hook_json()
        assert "hookSpecificOutput" in hook
        inner = hook["hookSpecificOutput"]
        assert inner["hookEventName"] == "PreToolUse"
        assert inner["permissionDecision"] == "allow"
        assert "additionalContext" in inner

    def test_advisory_contains_title(self) -> None:
        msg = _make_msg(title="Test Title")
        hook = msg.to_hook_json()
        assert "Test Title" in hook["hookSpecificOutput"]["additionalContext"]

    def test_advisory_contains_context(self) -> None:
        msg = _make_msg(context="Some context")
        hook = msg.to_hook_json()
        assert "Some context" in hook["hookSpecificOutput"]["additionalContext"]

    def test_json_serializable(self) -> None:
        msg = _make_msg()
        # Must not raise
        serialized = msg.to_hook_json_str()
        parsed = json.loads(serialized)
        assert "hookSpecificOutput" in parsed


class TestSteerMessageImmutable:
    """SteerMessage is frozen (immutable)."""

    def test_frozen(self) -> None:
        msg = _make_msg()
        try:
            msg.rule_id = "changed"  # type: ignore[misc]
            raise AssertionError("Should have raised FrozenInstanceError")
        except AttributeError:
            pass  # Expected for frozen dataclass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_msg(
    *,
    layer: EnforcementLayer = EnforcementLayer.HARD_BLOCK,
    rule_id: str = "test-rule",
    decision: ApprovalDecision = ApprovalDecision.BLOCK,
    severity: str = "critical",
    title: str = "Test Title",
    context: str = "Test context",
    suggestion: str = "Test suggestion",
    tool_name: str | None = None,
    file_path: str | None = None,
    can_suppress: bool = False,
    suppress_key: str = "",
) -> SteerMessage:
    return SteerMessage(
        layer=layer,
        rule_id=rule_id,
        decision=decision,
        severity=severity,
        title=title,
        context=context,
        suggestion=suggestion,
        tool_name=tool_name,
        file_path=file_path,
        can_suppress=can_suppress,
        suppress_key=suppress_key,
    )
