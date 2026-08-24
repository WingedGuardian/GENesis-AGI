"""Regression tests for the inline mega-guard embedded in .claude/settings.json.

PR-Guards removed the force-push and worktree-remove arms from this inline blob
(they are duplicated by the tracked project guards git_push_guard.py and
worktree_cwd_guard.py, and the force-push arm carried a whole-command substring
FP: `git push origin main && rm -f x` false-matched). The 2026-08 git-discard
consolidation then removed reset --hard / clean -f too: they now have a tracked
python-guard backstop (git_discard_guard.py — precise, with a `# discard-override`
escape and NEW git checkout/restore coverage), wired in the Bash matcher and
delegated to by bash_safety_hook.sh. Only nohup / genesis-serve-worktree stay in
this inline blob (no dedicated python guard yet).

The test extracts the inline command straight from the tracked settings.json and
runs it, so it fails if the FP-prone arm is ever reintroduced.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SETTINGS = _REPO_ROOT / ".claude" / "settings.json"


def _inline_guard() -> str:
    data = json.loads(_SETTINGS.read_text())
    for entries in data["hooks"].values():
        for e in entries:
            for h in e.get("hooks", []):
                cmd = h.get("command", "")
                if "run_in_background" in cmd and "worktree" in cmd and "case " in cmd:
                    return cmd
    raise AssertionError("inline mega-guard not found in .claude/settings.json")


_GUARD = _inline_guard()


def _run(command: str) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_input": {"command": command}})
    return subprocess.run(
        ["bash", "-c", _GUARD],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_guard_is_syntactically_valid():
    r = subprocess.run(["bash", "-n", "-c", _GUARD], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


class TestKeptArms:
    """These have no tracked python backstop — they MUST stay in the inline blob."""

    def test_nohup_runtime_blocks(self):
        assert _run("nohup python -m genesis serve &").returncode == 2

    def test_genesis_serve_worktree_blocks(self):
        assert _run("PYTHONPATH=/x/worktree/src python -m genesis serve").returncode == 2


class TestRemovedArms:
    """Owned by tracked project guards now — the inline blob must NOT block
    them (and must not carry the substring FP)."""

    def test_force_push_not_inline_blocked(self):
        # git_push_guard.py (tracked project hook) hard-blocks force push to
        # origin; the inline blob no longer duplicates it.
        assert _run("git push --force origin main").returncode == 0

    def test_force_push_fp_gone(self):
        """The exact FP: a plain push followed by an unrelated `rm -f`."""
        assert _run("git push origin main && rm -f /tmp/x").returncode == 0

    def test_worktree_remove_not_inline_blocked(self):
        # worktree_cwd_guard.py (tracked project hook) blocks ALL `git worktree
        # remove`; the inline --force arm was redundant.
        assert _run("git worktree remove --force /tmp/wt").returncode == 0


class TestInlineDiscardFloor:
    """The inline blob keeps ONLY the reset --hard speed-bump (2026-08-24
    recoverability redesign). reset is recoverable (the snapshot net undoes it),
    so a crude substring block is a fine dependency-free nudge. `git clean` is NO
    LONGER handled here — it is UNrecoverable and needs a precise, quote-aware
    block that a naive inline regex can't give (it would false-block
    `git checkout clean-branch`, `git commit -m "clean up"`), so it moved to the
    tracked guard git_discard_guard.py (bash_safety global + project hook), like
    force-push/worktree-remove before it. The inline blob must NOT block clean."""

    def test_reset_hard_inline_blocked(self):
        assert _run("git reset --hard HEAD~1").returncode == 2

    def test_reset_soft_allowed(self):
        assert _run("git reset --soft HEAD~1").returncode == 0

    @pytest.mark.parametrize(
        "cmd",
        [
            "git clean -f",
            "git clean --force",
            "git clean -fd",
            "git clean",
            "git clean -nd",
            "git checkout clean-branch",  # would false-block under a naive match
            'git commit -m "clean up"',
        ],
    )
    def test_clean_not_inline_blocked(self, cmd):
        assert _run(cmd).returncode == 0


class TestUnrelatedAllowed:
    def test_plain_command(self):
        assert _run("ls -la").returncode == 0

    def test_normal_push(self):
        assert _run("git push origin feature/x").returncode == 0
