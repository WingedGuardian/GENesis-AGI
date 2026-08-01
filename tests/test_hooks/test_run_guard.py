"""Tests for hook_input.run_guard — crash→fail-closed for irreversible-action guards.

CC's PreToolUse contract: exit 2 = block; ANY other exit = non-blocking error →
the tool RUNS. So an uncaught exception (exit 1) in a guard is a silent
fail-open. run_guard converts an UNEXPECTED crash into exit 2 for the guards
protecting irreversible actions, while passing through normal 0/2 results.

Also sanity-checks each rewired guard end-to-end through __main__ (the exact
path CC invokes) so the wiring change cannot silently alter allow/block
behavior.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_HOOKS_DIR = _WORKTREE / "scripts" / "hooks"
_PYTHON = sys.executable

# Import hook_input directly from the hooks dir.
_spec = importlib.util.spec_from_file_location("hook_input", _HOOKS_DIR / "hook_input.py")
_hook_input = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_hook_input)
run_guard = _hook_input.run_guard


# ---------------------------------------------------------------------------
# Unit tests: run_guard semantics
# ---------------------------------------------------------------------------


class TestRunGuardUnit:
    def test_passes_through_allow(self):
        with pytest.raises(SystemExit) as exc:
            run_guard(lambda: 0, "t")
        assert exc.value.code == 0

    def test_passes_through_block(self):
        with pytest.raises(SystemExit) as exc:
            run_guard(lambda: 2, "t")
        assert exc.value.code == 2

    def test_none_return_treated_as_allow(self):
        with pytest.raises(SystemExit) as exc:
            run_guard(lambda: None, "t")
        assert exc.value.code == 0

    def test_unexpected_crash_fails_closed(self, capsys):
        def boom():
            raise RuntimeError("simulated guard bug")

        with pytest.raises(SystemExit) as exc:
            run_guard(boom, "myguard")
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "GUARD ERROR (myguard)" in err
        assert "failing CLOSED" in err
        assert "simulated guard bug" in err

    def test_keyerror_crash_fails_closed(self):
        def boom():
            raise KeyError("missing")

        with pytest.raises(SystemExit) as exc:
            run_guard(boom, "t")
        assert exc.value.code == 2

    def test_system_exit_propagates_untouched(self):
        """A guard that sys.exit()s inside main keeps its own code."""

        def exits():
            sys.exit(3)

        with pytest.raises(SystemExit) as exc:
            run_guard(exits, "t")
        assert exc.value.code == 3

    def test_keyboard_interrupt_propagates(self):
        def interrupted():
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            run_guard(interrupted, "t")


# ---------------------------------------------------------------------------
# Subprocess sanity: each rewired guard still allows/blocks through __main__
# ---------------------------------------------------------------------------


def _run_hook(script: str, command: str) -> subprocess.CompletedProcess:
    """Run a Bash-matcher guard exactly as CC does: stdin JSON payload."""
    payload = json.dumps({"tool_input": {"command": command}, "tool_name": "Bash"})
    return subprocess.run(
        [_PYTHON, str(_HOOKS_DIR / script)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=15,
    )


class TestRewiredGuardsEndToEnd:
    """The run_guard rewiring must not change normal allow/block behavior."""

    def test_destructive_guard_still_blocks_broad_rm(self):
        result = _run_hook("destructive_command_guard.py", "rm -rf /")
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_destructive_guard_still_allows_deep_rm(self):
        result = _run_hook("destructive_command_guard.py", "rm -rf /home/user/project/subdir")
        assert result.returncode == 0

    def test_destructive_guard_allows_unrelated(self):
        result = _run_hook("destructive_command_guard.py", "ls -la")
        assert result.returncode == 0

    def test_protected_paths_guard_allows_unrelated(self):
        result = _run_hook("protected_paths_guard.py", "echo hello")
        assert result.returncode == 0

    def test_worktree_guard_allows_unrelated(self):
        result = _run_hook("worktree_cwd_guard.py", "git status")
        assert result.returncode == 0

    def test_worktree_guard_still_blocks_removal(self):
        result = _run_hook("worktree_cwd_guard.py", "git worktree remove /tmp/some-worktree")
        assert result.returncode == 2

    def test_git_push_guard_allows_unrelated(self):
        result = _run_hook("git_push_guard.py", "echo nothing to see")
        assert result.returncode == 0

    def test_git_push_guard_still_blocks_no_verify(self):
        result = _run_hook("git_push_guard.py", "git commit --no-verify -m x")
        assert result.returncode == 2
        assert "no-verify" in result.stderr


class TestGitPushGuardCrashFailsClosed:
    """REGRESSION: main()'s old blanket `except Exception: pass` turned ANY
    orchestration bug into a silent allow. Now a bug propagates to run_guard →
    exit 2."""

    @pytest.fixture
    def crashing_wrapper(self, tmp_path):
        wrapper = tmp_path / "wrapper.py"
        wrapper.write_text(
            f"""
import sys
sys.path.insert(0, "{_HOOKS_DIR}")
import importlib.util
spec = importlib.util.spec_from_file_location(
    "git_push_guard", "{_HOOKS_DIR / "git_push_guard.py"}"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

def boom(cmd):
    raise RuntimeError("simulated orchestration bug")
mod.analyze = boom

from hook_input import run_guard
run_guard(mod.main, "git_push_guard")
"""
        )
        return wrapper

    def test_push_with_crashed_analyzer_blocks(self, crashing_wrapper):
        payload = json.dumps({"tool_input": {"command": "git push origin main"}})
        result = subprocess.run(
            [_PYTHON, str(crashing_wrapper)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 2, (
            f"crash during push analysis must BLOCK, got {result.returncode}: {result.stderr}"
        )
        assert "GUARD ERROR" in result.stderr

    def test_malformed_payload_still_fails_open(self):
        """The DOCUMENTED parse-ambiguity fail-open is preserved: garbage stdin
        → exit 0 (hook_input coerces to {} → no command → allow)."""
        result = subprocess.run(
            [_PYTHON, str(_HOOKS_DIR / "git_push_guard.py")],
            input="not json {{{",
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
