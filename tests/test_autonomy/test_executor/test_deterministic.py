"""Tests for genesis.autonomy.executor.deterministic."""

from __future__ import annotations

from pathlib import Path

import pytest

from genesis.autonomy.executor.deterministic import (
    execute_deterministic_step,
    validate_command,
)
from genesis.autonomy.executor.types import StepType


class TestValidateCommand:
    """Safety guardrail validation."""

    def test_safe_command_passes(self) -> None:
        assert validate_command("pytest tests/test_foo.py -v") is None

    def test_echo_passes(self) -> None:
        assert validate_command("echo hello") is None

    def test_ruff_passes(self) -> None:
        assert validate_command("ruff check src/") is None

    def test_git_status_passes(self) -> None:
        assert validate_command("git status") is None

    def test_git_diff_passes(self) -> None:
        assert validate_command("git diff --cached") is None

    def test_git_commit_passes(self) -> None:
        assert validate_command("git commit -m 'test'") is None

    def test_rm_rf_blocked(self) -> None:
        result = validate_command("rm -rf /")
        assert result is not None
        assert "blocked" in result.lower()

    def test_rm_fr_blocked(self) -> None:
        result = validate_command("rm -fr .")
        assert result is not None

    def test_rm_force_blocked(self) -> None:
        result = validate_command("rm --force file.txt")
        assert result is not None

    def test_drop_table_blocked(self) -> None:
        result = validate_command("sqlite3 db.sqlite 'DROP TABLE users'")
        assert result is not None

    def test_drop_database_blocked(self) -> None:
        result = validate_command("DROP DATABASE production")
        assert result is not None

    def test_git_push_force_blocked(self) -> None:
        result = validate_command("git push --force origin main")
        assert result is not None

    def test_git_reset_hard_blocked(self) -> None:
        result = validate_command("git reset --hard")
        assert result is not None

    def test_git_clean_f_blocked(self) -> None:
        result = validate_command("git clean -fd")
        assert result is not None

    def test_killall_blocked(self) -> None:
        result = validate_command("killall python")
        assert result is not None

    def test_pkill_9_blocked(self) -> None:
        result = validate_command("pkill -9 genesis")
        assert result is not None

    def test_chmod_777_blocked(self) -> None:
        result = validate_command("chmod -R 777 /")
        assert result is not None

    def test_truncate_table_blocked(self) -> None:
        result = validate_command("TRUNCATE TABLE sessions")
        assert result is not None

    def test_find_delete_blocked(self) -> None:
        result = validate_command("find . -name '*.pyc' -delete")
        assert result is not None

    def test_find_exec_rm_blocked(self) -> None:
        result = validate_command("find /tmp -exec rm {} ;")
        assert result is not None

    def test_bash_c_blocked(self) -> None:
        """Interpreter indirection is blocked."""
        # Use a command that doesn't match other patterns so the
        # interpreter check is what catches it
        result = validate_command("bash -c 'echo hello'")
        assert result is not None
        assert "interpreter" in result.lower()

    def test_python_c_blocked(self) -> None:
        result = validate_command("python -c 'import os; os.remove(\"/\")'")
        assert result is not None
        assert "interpreter" in result.lower()

    def test_eval_blocked(self) -> None:
        result = validate_command("eval 'dangerous command'")
        assert result is not None
        assert "interpreter" in result.lower()

    def test_node_blocked(self) -> None:
        result = validate_command("node -e 'process.exit(1)'")
        assert result is not None


@pytest.mark.asyncio
class TestExecuteDeterministicStep:
    """Test deterministic step execution."""

    async def test_echo_command_succeeds(self) -> None:
        step = {"idx": 0, "type": "bash", "command": "echo hello"}
        result = await execute_deterministic_step(step)
        assert result.status == "completed"
        assert "hello" in result.result
        assert result.cost_usd == 0.0
        assert result.model_used == "deterministic"

    async def test_missing_command_fails(self) -> None:
        step = {"idx": 0, "type": "bash"}
        result = await execute_deterministic_step(step)
        assert result.status == "failed"
        assert "no 'command' field" in result.result.lower()

    async def test_empty_command_fails(self) -> None:
        step = {"idx": 0, "type": "bash", "command": ""}
        result = await execute_deterministic_step(step)
        assert result.status == "failed"

    async def test_blocked_command_fails(self) -> None:
        step = {"idx": 0, "type": "bash", "command": "rm -rf /tmp/test"}
        result = await execute_deterministic_step(step)
        assert result.status == "failed"
        assert "blocked" in result.result.lower()

    async def test_nonzero_exit_code_fails(self) -> None:
        step = {"idx": 0, "type": "bash", "command": "false"}
        result = await execute_deterministic_step(step)
        assert result.status == "failed"
        assert result.blocker_description is not None
        assert "exit" in result.blocker_description.lower()

    async def test_cost_is_zero(self) -> None:
        step = {"idx": 0, "type": "bash", "command": "echo cost_test"}
        result = await execute_deterministic_step(step)
        assert result.cost_usd == 0.0

    async def test_duration_is_recorded(self) -> None:
        step = {"idx": 0, "type": "bash", "command": "echo fast"}
        result = await execute_deterministic_step(step)
        assert result.duration_s >= 0.0

    async def test_stderr_captured(self) -> None:
        # Use a command that writes to stderr without shell redirection
        # (since we use exec-mode, not shell)
        step = {"idx": 0, "type": "bash", "command": "ls /nonexistent_path_for_test"}
        result = await execute_deterministic_step(step)
        assert result.status == "failed"
        assert "STDERR" in result.result

    async def test_worktree_path_used(self, tmp_path: Path) -> None:
        step = {"idx": 0, "type": "bash", "command": "pwd"}
        result = await execute_deterministic_step(
            step, worktree_path=tmp_path,
        )
        assert result.status == "completed"
        assert str(tmp_path) in result.result

    async def test_test_step_type(self) -> None:
        step = {"idx": 0, "type": "test", "command": "echo test_passed"}
        result = await execute_deterministic_step(step)
        assert result.status == "completed"
        assert "test_passed" in result.result

    async def test_git_step_type(self) -> None:
        step = {"idx": 0, "type": "git", "command": "git --version"}
        result = await execute_deterministic_step(step)
        assert result.status == "completed"
        assert "git version" in result.result


class TestStepTypeProperties:
    """Verify the new StepType properties."""

    def test_bash_is_deterministic(self) -> None:
        assert StepType.BASH.is_deterministic is True

    def test_test_is_deterministic(self) -> None:
        assert StepType.TEST.is_deterministic is True

    def test_git_is_deterministic(self) -> None:
        assert StepType.GIT.is_deterministic is True

    def test_code_is_not_deterministic(self) -> None:
        assert StepType.CODE.is_deterministic is False

    def test_research_is_not_deterministic(self) -> None:
        assert StepType.RESEARCH.is_deterministic is False

    def test_deterministic_types_do_not_verify(self) -> None:
        for st in (StepType.BASH, StepType.TEST, StepType.GIT):
            assert st.verify_step is False, f"{st} should not require verification"
