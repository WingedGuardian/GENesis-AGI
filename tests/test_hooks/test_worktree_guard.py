"""Tests for worktree_cwd_guard.py — cross-session worktree protection.

Tests the enhanced guard hook that:
1. Blocks removal if another process has CWD inside the target worktree
2. Blocks removal if the current session's CWD is the target (self-brick)
3. Blocks ALL direct worktree removal (redirects to lifecycle manager)
4. Handles ExitWorktree tool (--exit-worktree mode)
5. Hard-blocks EnterWorktree relocation (--enter-worktree mode)

Exit codes: 0 = allowed, 2 = blocked.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest


def _find_guard_script() -> str:
    """Resolve path to worktree_cwd_guard.py and return a command string."""
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        script = ancestor / "scripts" / "hooks" / "worktree_cwd_guard.py"
        if script.exists():
            venv_python = ancestor / ".venv" / "bin" / "python"
            python = str(venv_python) if venv_python.exists() else "python3"
            return f"{python} {script}"
    raise FileNotFoundError("Could not find worktree_cwd_guard.py")


@pytest.fixture(scope="module")
def guard_cmd() -> str:
    return _find_guard_script()


def _run_guard(
    cmd: str,
    tool_input: dict,
    extra_args: str = "",
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    """Run the guard hook with the real CC payload piped on stdin."""
    full_cmd = f"{cmd} {extra_args}".strip()
    # Deliver via the real contract: full payload on stdin, tool args nested
    # under tool_input; scrub the dead legacy env var.
    payload = json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": tool_input}
    )
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_TOOL_INPUT"}
    return subprocess.run(
        full_cmd,
        shell=True,
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        cwd=cwd,
    )


# ---------------------------------------------------------------------------
# Bash mode: non-worktree commands pass through
# ---------------------------------------------------------------------------


class TestBashPassthrough:
    def test_non_worktree_command_allowed(self, guard_cmd: str) -> None:
        result = _run_guard(guard_cmd, {"command": "ls -la"})
        assert result.returncode == 0

    def test_worktree_add_allowed(self, guard_cmd: str) -> None:
        result = _run_guard(guard_cmd, {"command": "git worktree add /tmp/foo"})
        assert result.returncode == 0

    def test_worktree_list_allowed(self, guard_cmd: str) -> None:
        result = _run_guard(guard_cmd, {"command": "git worktree list"})
        assert result.returncode == 0

    def test_empty_command_allowed(self, guard_cmd: str) -> None:
        result = _run_guard(guard_cmd, {"command": ""})
        assert result.returncode == 0

    def test_empty_input_allowed(self, guard_cmd: str) -> None:
        result = _run_guard(guard_cmd, {})
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Bash mode: all git worktree remove is blocked
# ---------------------------------------------------------------------------


class TestBashBlockAll:
    def test_worktree_remove_blocked(self, guard_cmd: str) -> None:
        """Any git worktree remove is blocked (lifecycle manager only)."""
        result = _run_guard(
            guard_cmd,
            {"command": "git worktree remove /tmp/nonexistent-worktree-xyz"},
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_worktree_remove_relative_blocked(self, guard_cmd: str) -> None:
        result = _run_guard(
            guard_cmd,
            {"command": "git worktree remove .claude/worktrees/some-branch"},
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_block_message_mentions_lifecycle(self, guard_cmd: str) -> None:
        result = _run_guard(
            guard_cmd,
            {"command": "git worktree remove /tmp/nonexistent-worktree-xyz"},
        )
        assert "lifecycle" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Bash mode: self-CWD detection (original behavior preserved)
# ---------------------------------------------------------------------------


class TestBashSelfCwd:
    def test_remove_own_cwd_blocked(self, guard_cmd: str) -> None:
        """Removing your own CWD gives the specific brick-prevention message."""
        cwd = os.getcwd()
        result = _run_guard(guard_cmd, {"command": f"git worktree remove {cwd}"})
        assert result.returncode == 2
        assert "current working directory" in result.stderr


# ---------------------------------------------------------------------------
# Bash mode: cross-session detection
# ---------------------------------------------------------------------------


class TestBashCrossSession:
    def test_blocks_when_process_in_target(self, guard_cmd: str) -> None:
        """Block removal when another process has CWD inside the target."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Spawn a sleep process with CWD in the target directory
            proc = subprocess.Popen(
                ["sleep", "60"],
                cwd=tmpdir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                # Give the process a moment to start
                time.sleep(0.1)
                result = _run_guard(
                    guard_cmd,
                    {"command": f"git worktree remove {tmpdir}"},
                )
                assert result.returncode == 2
                assert "BLOCKED" in result.stderr
                assert str(proc.pid) in result.stderr
            finally:
                proc.terminate()
                proc.wait(timeout=5)

    def test_allows_when_no_process_in_target(self, guard_cmd: str) -> None:
        """When no process is in the target, still blocked (lifecycle redirect).

        This verifies the block-all behavior — even without conflicts,
        direct removal is blocked in favor of the lifecycle manager.
        """
        result = _run_guard(
            guard_cmd,
            {"command": "git worktree remove /tmp/nonexistent-dir-abc123"},
        )
        assert result.returncode == 2
        assert "lifecycle" in result.stderr.lower()


# ---------------------------------------------------------------------------
# ExitWorktree mode
# ---------------------------------------------------------------------------


class TestExitWorktree:
    def test_keep_action_allowed(self, guard_cmd: str) -> None:
        """ExitWorktree with action 'keep' always passes."""
        result = _run_guard(
            guard_cmd,
            {"action": "keep"},
            extra_args="--exit-worktree",
        )
        assert result.returncode == 0

    def test_remove_action_blocked_no_conflict(self, guard_cmd: str) -> None:
        """ExitWorktree 'remove' blocked even when no other processes present.

        Uses a tmpdir as CWD so no other process has CWD inside it.
        Should get the 'use keep instead' message.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _run_guard(
                guard_cmd,
                {"action": "remove"},
                extra_args="--exit-worktree",
                cwd=tmpdir,
            )
            assert result.returncode == 2
            assert "BLOCKED" in result.stderr
            assert "keep" in result.stderr.lower()
            assert "lifecycle" in result.stderr.lower()

    def test_remove_with_cross_session_conflict(self, guard_cmd: str) -> None:
        """ExitWorktree remove shows PIDs when other processes are in CWD."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Spawn a sleep process with CWD in the tmpdir
            proc = subprocess.Popen(
                ["sleep", "60"],
                cwd=tmpdir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                time.sleep(0.1)
                result = _run_guard(
                    guard_cmd,
                    {"action": "remove"},
                    extra_args="--exit-worktree",
                    cwd=tmpdir,
                )
                assert result.returncode == 2
                assert "BLOCKED" in result.stderr
                assert str(proc.pid) in result.stderr
            finally:
                proc.terminate()
                proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# EnterWorktree mode — relocation block (keeps sessions findable)
# ---------------------------------------------------------------------------


class TestEnterWorktree:
    def test_enter_with_name_blocked(self, guard_cmd: str) -> None:
        """EnterWorktree creating a named worktree is hard-blocked."""
        result = _run_guard(
            guard_cmd,
            {"name": "my-feature"},
            extra_args="--enter-worktree",
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr
        assert "my-feature" in result.stderr

    def test_enter_with_path_blocked(self, guard_cmd: str) -> None:
        """EnterWorktree switching into an existing worktree is hard-blocked."""
        result = _run_guard(
            guard_cmd,
            {"path": ".claude/worktrees/existing"},
            extra_args="--enter-worktree",
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_enter_empty_input_blocked(self, guard_cmd: str) -> None:
        """EnterWorktree with no args (auto-named) is still hard-blocked."""
        result = _run_guard(guard_cmd, {}, extra_args="--enter-worktree")
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_block_message_redirects_to_findable_pattern(
        self,
        guard_cmd: str,
    ) -> None:
        """Message must point to the non-relocating alternative + /resume."""
        result = _run_guard(
            guard_cmd,
            {"name": "x"},
            extra_args="--enter-worktree",
        )
        err = result.stderr.lower()
        assert "git worktree add" in err
        assert "/resume" in err

    def test_enter_blocked_with_empty_stdin(self, guard_cmd: str) -> None:
        """Hard block holds even with no payload on stdin (no fail-open)."""
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_TOOL_INPUT"}
        result = subprocess.run(
            f"{guard_cmd} --enter-worktree",
            shell=True,
            input="",
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_enter_blocked_with_malformed_stdin(self, guard_cmd: str) -> None:
        """Hard block holds even when the stdin payload is not valid JSON."""
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_TOOL_INPUT"}
        result = subprocess.run(
            f"{guard_cmd} --enter-worktree",
            shell=True,
            input="not-json",
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr


# ---------------------------------------------------------------------------
# Fail-open behavior
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_malformed_json_allowed(self, guard_cmd: str) -> None:
        """Malformed CLAUDE_TOOL_INPUT → fail-open."""
        env = {**os.environ, "CLAUDE_TOOL_INPUT": "not-json"}
        result = subprocess.run(
            guard_cmd,
            shell=True,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0

    def test_missing_env_var_allowed(self, guard_cmd: str) -> None:
        """Missing CLAUDE_TOOL_INPUT → fail-open."""
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_TOOL_INPUT"}
        result = subprocess.run(
            guard_cmd,
            shell=True,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0


# --- mention vs execution (2026-09-03) -------------------------------------
#
# The predicate was `\bgit\s+worktree\s+remove\b` over the raw command, and the
# target was then read by splitting the text that FOLLOWED the match. That is
# quote-blind: the phrase inside a grep pattern, a heredoc body or a commit
# message matched, and the next word became the "target", so a read-only search
# was refused with "use the lifecycle manager". It also MISSED real removals,
# because the regex required `git` immediately followed by `worktree` —
# `git -C <path> worktree remove <target>` slipped straight through.
#
# MEASURED over 48,363 real commands from this install's transcripts: the swap
# frees 4 mention-only blocks AND closes 6 genuine removals that were previously
# allowed. The blocked count ROSE, 150 -> 152, which is the correct outcome — it
# is a different and strictly better set, not a smaller one.
#
# Built from fragments so this file's own text cannot trip the guard it tests.
_SUB = "worktree"
_OP = "remove"
_PHRASE = f"git {_SUB} {_OP}"


class TestMentionIsNotExecution:
    """Both directions. An allow-only suite passes just as well against a guard
    that has stopped working, so every mention case is paired with a removal that
    must still block."""

    @pytest.mark.parametrize(
        "inner",
        [
            f"grep -rn '{_PHRASE}' scripts/",
            f'echo "{_PHRASE} is blocked here"',
            f"git commit -m 'docs: explain why {_PHRASE} is gated'",
            f"cat > /tmp/wt_note.md <<'EOF'\nNever run {_PHRASE} by hand.\nEOF",
        ],
    )
    def test_mention_only_is_allowed(self, guard_cmd: str, inner: str) -> None:
        result = _run_guard(guard_cmd, {"command": inner})
        assert result.returncode == 0, result.stderr

    def test_real_removal_still_blocks(self, guard_cmd: str) -> None:
        """TRUE-POSITIVE CONTROL."""
        result = _run_guard(guard_cmd, {"command": f"{_PHRASE} /tmp/some-worktree"})
        assert result.returncode == 2, result.stdout + result.stderr

    def test_removal_behind_a_global_flag_now_blocks(self, guard_cmd: str) -> None:
        """REGRESSION PIN for a fail-OPEN hole the old regex had: it required
        `git` immediately followed by the subcommand, so a global flag in between
        hid a real removal. Six such commands were found in the corpus.

        NOTE this case passes with the index bug below too — `/srv/genesis` is
        not the subcommand's name, so `argv.index()` happens to land correctly.
        It pins the regex fix, not the index fix.
        """
        inner = f"git -C /srv/genesis {_SUB} {_OP} /tmp/some-worktree"
        result = _run_guard(guard_cmd, {"command": inner})
        assert result.returncode == 2, result.stdout + result.stderr

    def test_global_flag_operand_equal_to_the_subcommand_name_blocks(self, guard_cmd: str) -> None:
        """REGRESSION PIN for the fail-open cross-model review found.

        The operand extraction re-found the subcommand with
        `argv.index(_SUBCOMMAND)`, which returns the FIRST matching token. Here
        the `-C` operand IS the literal subcommand name, so the index landed on
        the operand, the tail started one token early, `after_sub[0]` was the
        subcommand name rather than the operation, and a REAL removal was
        ALLOWED.

        The directory does not need to exist — the guard classifies argv before
        touching the filesystem, and the `-C` operand is never resolved.
        """
        inner = f"git -C {_SUB} {_SUB} {_OP} /tmp/some-worktree"
        result = _run_guard(guard_cmd, {"command": inner})
        assert result.returncode == 2, result.stdout + result.stderr

    def test_subcommand_name_as_a_removal_target_still_blocks(self, guard_cmd: str) -> None:
        """The mirror shape: the TARGET path equal to the subcommand name.

        Guards against a fix that simply searched for the LAST occurrence
        instead of the first — that would land on the target here and skip the
        segment just as silently.
        """
        inner = f"git {_SUB} {_OP} {_SUB}"
        result = _run_guard(guard_cmd, {"command": inner})
        assert result.returncode == 2, result.stdout + result.stderr
