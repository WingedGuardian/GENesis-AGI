"""Regression guard + smoke for the `.claude/hooks/genesis-hook` launcher.

The launcher's worktree venv-fallback used `git worktree list --porcelain | head`
under `set -euo pipefail`. `head` closes the pipe after one line, so when git's
output exceeds the pipe buffer (many worktrees) git dies with SIGPIPE; pipefail
+ set -e then kill the launcher silently (exit 141, no stderr), breaking EVERY
hook in that worktree. Fixed by resolving the main worktree via
`git rev-parse --git-common-dir` (no pipe). These tests lock that in.
"""

import os
import subprocess
from pathlib import Path

_WRAPPER = Path(__file__).resolve().parents[2] / ".claude" / "hooks" / "genesis-hook"


def _code_only() -> str:
    """Wrapper text with comment-only lines stripped (test code, not comments)."""
    return "\n".join(
        ln for ln in _WRAPPER.read_text().splitlines()
        if not ln.lstrip().startswith("#")
    )


def test_no_sigpipe_prone_pipe_in_code():
    """The fragile `git worktree list … | head` must not be in executable code.

    (Would FAIL on the pre-fix launcher — that's the regression this guards.)
    """
    code = _code_only()
    assert "git worktree list" not in code, "fragile worktree-list pipeline returned"
    assert "| head" not in code, "early-closing pipe under pipefail returned"


def test_uses_git_common_dir_for_main_root():
    """The venv fallback resolves the main worktree via the no-pipe rev-parse."""
    assert "git rev-parse --git-common-dir" in _code_only()


def test_wrapper_never_sigpipes_on_invocation():
    """Invoking the launcher must never die with SIGPIPE (exit 141).

    GENESIS_CC_SESSION=1 makes the hook exit early, so this exercises the
    wrapper's venv resolution (the fixed path) without hook side effects. We
    only assert it is not 141 — exit 0 (venv found) or 1 (clear "venv not
    found" error) are both acceptable across environments.
    """
    env = {**os.environ, "GENESIS_CC_SESSION": "1"}
    for _ in range(10):
        proc = subprocess.run(
            [str(_WRAPPER), "hooks/session_observer_hook.py"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=env,
        )
        assert proc.returncode != 141, f"SIGPIPE (141)! stderr={proc.stderr!r}"
