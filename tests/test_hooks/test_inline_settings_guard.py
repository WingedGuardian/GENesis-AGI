"""Regression tests for the inline mega-guard embedded in .claude/settings.json.

PR-Guards removed the force-push and worktree-remove arms from this inline blob
(they are duplicated by the tracked project guards git_push_guard.py and
worktree_cwd_guard.py, and the force-push arm carried a whole-command substring
FP: `git push origin main && rm -f x` false-matched). The 2026-08 git-discard
consolidation then moved `git clean` blocking OUT to a tracked python guard
(git_discard_guard.py — precise, with a `# discard-override` escape and git
checkout/restore/rm/mv snapshot coverage), wired in the Bash matcher and delegated
to by bash_safety_hook.sh. The `git reset --hard` arm STAYS inline as a coarse,
dependency-free SPEED-BUMP (reset is snapshot-recoverable, so its inline block is
best-effort, not a boundary); only nohup / genesis-serve-worktree and that reset
speed-bump remain in this inline blob.

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
                if "git reset --hard" in cmd and "worktree" in cmd and "case " in cmd:
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


class TestWrapperArmOverMatches:
    """The runtime-wrapper arm DELIBERATELY over-matches. Do not anchor it.

    It is three unanchored, quote-blind globs: `*"X"*"genesis"*` matches any
    command mentioning both anywhere, IN THAT ORDER. Every path in this repo
    contains "genesis", so any in-repo command mentioning the wrapper is refused
    — a read-only grep included. That was reproduced three times on 2026-09-03,
    once by an exploring subagent that hit it while reading the guard's source,
    and it is a real cost: MEASURED 12/6000 real commands.

    An earlier revision of this PR anchored it at command position, taking that
    to 7/6000. Cross-model review then found the anchored form fell OPEN on a
    leading redirection, so the anchoring was reverted along with its two sibling
    arms and this class was inverted with it.

    The reason is structural: this arm lives inside a `bash -c` blob in
    settings.json with no access to the canonical tokenizer, so anchoring it means
    modelling shell grammar with a regex — an open set the review loop finds one
    member of per round. Over-blocking is friction; under-blocking lets the full
    runtime boot against a worktree, which OOM-crashed the container on
    2026-07-03. Friction is the correct side to err on.

    Fragments, so this file's text cannot trip the live hook.
    """

    _W = "no" + "hup"

    # MUST contain the literal word "genesis" — it is the second half of the glob,
    # and it is what makes the friction cases below DISCRIMINATE. MEASURED: with
    # this value they block; swap in a path without the word (e.g. /srv/repo) and
    # they stop blocking — still green, but pinning nothing. Any future
    # sanitization pass that changes this value must re-run that table, not just
    # the suite.
    _PATH = "/srv/genesis"

    def test_real_runtime_launch_still_blocks(self):
        """TRUE-POSITIVE CONTROL — TestKeptArms above covers the plain form; this
        pins the wrapped forms the arm has to keep catching."""
        assert _run(f"{self._W} python -m genesis serve &").returncode == 2

    def test_launch_after_a_separator_blocks(self):
        assert _run(f"cd {self._PATH} && {self._W} python -m genesis serve &").returncode == 2

    def test_launch_behind_an_env_assignment_blocks(self):
        assert _run(f"PYTHONPATH=/x {self._W} python -m genesis serve").returncode == 2

    def test_launch_behind_a_redirection_blocks(self):
        """REGRESSION PIN — the fail-open bypass review surfaced in the anchored
        form, which tolerated leading env assignments but not redirections.
        MEASURED old=BLOCK / anchored=ALLOW / reverted=BLOCK."""
        assert _run(f"2>/dev/null {self._W} python -m genesis serve").returncode == 2

    def test_grep_mentioning_it_in_a_repo_path_is_blocked_and_that_is_intended(self):
        """THE ACCEPTED FRICTION: the word as a search PATTERN, with the path
        supplying the second half of the glob (see _PATH). Refused. Route around
        it rather than sharpening the predicate."""
        assert _run(f"grep -rn {self._W} {self._PATH}/scripts").returncode == 2

    def test_prose_mentioning_it_is_blocked_and_that_is_intended(self):
        assert _run(f'echo "never use {self._W}" >> {self._PATH}/notes.md').returncode == 2

    def test_wrapper_as_an_argument_is_not_a_launch(self):
        """Forward-looking only: this does NOT discriminate old from new. MEASURED
        old=ALLOW / new=ALLOW — but NOT because of the pipe. The glob is an
        ordered pair (`*wrapper*genesis*`), and here "genesis" appears only BEFORE
        the wrapper, so it never matches. Reordering the same operands
        (`grep <wrapper> /srv/genesis/x`) DOES trip it. Kept as a pin against a
        future widening to any mention of the wrapper, and labelled so it is not
        mistaken for a regression test."""
        assert _run(f"cat {self._PATH}/scripts/update.sh | grep {self._W}").returncode == 0
