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

    def test_removal_past_the_depth_bound_still_blocks(self, guard_cmd: str) -> None:
        """A removal nested past shell_parse's depth bound must NOT slip through.

        This is the fail-open the bare-`analyze` chokepoint exists to catch. A
        bounded parse returns NO segments — deliberately, so a searching guard
        cannot read "stopped looking" as "nothing found" — and this guard decides
        by searching. Before the blind-spot branch that meant zero targets and a
        silent allow, on a guard whose entire job is blocking.
        """
        deep = "git worktree remove /tmp/nonexistent-worktree-xyz"
        for _ in range(8):  # comfortably past MAX_SUBSTITUTION_DEPTH (5)
            deep = f"echo $({deep})"
        result = _run_guard(guard_cmd, {"command": deep})
        assert result.returncode == 2, result.stdout + result.stderr
        assert "BLOCKED" in result.stderr

    def test_deep_nesting_without_a_removal_is_still_allowed(
        self, guard_cmd: str
    ) -> None:
        """Negative control for the test above.

        Without it, a fallback that simply blocked every unreadable command
        naming the subcommand would pass the acceptance test while being
        useless — the "a matcher that finds nothing is indistinguishable from a
        matcher that looks at nothing" failure, in its blocking direction.
        """
        deep = "git worktree list"
        for _ in range(8):
            deep = f"echo $({deep})"
        result = _run_guard(guard_cmd, {"command": deep})
        assert result.returncode == 0, result.stdout + result.stderr

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
# Observed over 51,052 (command, directory) pairs from this install's
# transcripts, each replayed from the directory it was typed in: 154 blocked
# before and 154 after — a DIFFERENT set, freeing 6 mention-only refusals and
# catching 6 real removals the old predicate allowed. Those transcripts hold
# real commands and cannot be published, so this is the scale at which the swap
# was observed on one install, not a result another reader can re-derive.
#
# An earlier draft of this comment said "48,363 … frees 4 … 150 -> 152". That
# was a superseded corpus (before the harness replayed each command from its own
# directory) and a superseded predicate (before the carrier fallback below). Two
# other files quoted two other versions of the same claim. One measurement, one
# set of numbers, or none.
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


class TestTheUntokenizableFallbackIsAlsoFailClosed:
    """When shlex cannot read the command the guard drops to the coarse
    extractor, and that extractor carried the SAME adjacency assumption the
    parsed route had already been fixed for: it required `git` immediately
    followed by the subcommand, so `git -C <dir> <sub> <op> <target>` inside an
    untokenizable command produced no target and fell OPEN.

    The fix DELETES the assumption rather than modelling git's option grammar in
    the fallback: knowing which global options take a value is exactly the
    open-set claim this branch refuses to make in a regex. Anchoring only on the
    subcommand and the operation can over-match, never under-match, and this
    branch is reached only for text nothing can parse — where over-blocking is
    the declared correct side.
    """

    # An unbalanced quote inside $'...' — shlex raises, bash runs it fine.
    _UNPARSEABLE = "; echo $'a\\'b)c'"

    def test_a_removal_behind_a_global_option_blocks(self, guard_cmd: str) -> None:
        inner = f"git -C /srv/genesis {_SUB} {_OP} /tmp/some-worktree{self._UNPARSEABLE}"
        result = _run_guard(guard_cmd, {"command": inner})
        assert result.returncode == 2, result.stdout + result.stderr

    def test_the_adjacent_spelling_that_already_blocked_still_blocks(self, guard_cmd: str) -> None:
        """TWIN — the clause above is a widening, so pin the case it widens FROM.
        A fix that replaced the pattern instead of relaxing it would satisfy the
        first test and silently drop this one."""
        inner = f"{_PHRASE} /tmp/some-worktree{self._UNPARSEABLE}"
        result = _run_guard(guard_cmd, {"command": inner})
        assert result.returncode == 2, result.stdout + result.stderr

    def test_a_parseable_mention_is_unaffected(self, guard_cmd: str) -> None:
        """TRUE-NEGATIVE CONTROL. The widened pattern is consulted on the parsed
        route too (it gates the carrier fallback), so a mention in a command that
        parses cleanly must still be allowed — otherwise the widening has
        quietly reverted the branch."""
        result = _run_guard(guard_cmd, {"command": f"grep -rn '{_SUB} {_OP}' scripts/"})
        assert result.returncode == 0, result.stderr


class TestCommandCarriersAreNotAHole:
    """A parser is NARROWER than the regex it replaced along an axis the
    migration never named: executables that carry a command STRING.

    `eval '<removal>'`, `ssh box "<removal>"`, `find -exec`, `parallel`,
    `watch`, `script -c` and a shell function body all EXECUTE the removal, and
    all tokenize perfectly — so the `untokenizable()` fallback cannot see them.
    The parser reads the carrier as the executable, skips the segment, finds no
    target, and allows it. MEASURED: 9 shapes the pre-parser version blocked and
    the first parser version let through.

    That is the same open-set trap this branch reverted three shell arms over,
    reached from the other side: the justification for keeping the parser here
    was that it has the real tokenizer, and the tokenizer does not model this.
    """

    @pytest.mark.parametrize(
        "inner",
        [
            f"eval '{_PHRASE} /tmp/wt-x'",
            f'eval "{_PHRASE} /tmp/wt-x"',
            f"eval {_PHRASE} /tmp/wt-x",
            f'ssh box "{_PHRASE} /tmp/wt-x"',
            f"find /tmp -name 'wt-*' -exec {_PHRASE} {{}} \\;",
            f"watch {_PHRASE} /tmp/wt-x",
            f"parallel {_PHRASE} ::: /tmp/wt-x",
            f"script -q -c '{_PHRASE} /tmp/wt-x' /dev/null",
            f"f() {{ {_PHRASE} $1; }}; f /tmp/wt-x",
        ],
    )
    def test_a_carried_removal_still_blocks(self, guard_cmd: str, inner: str) -> None:
        """REGRESSION PIN — each of these was rc=2 before the parser migration,
        rc=0 after it, and is rc=2 again now."""
        result = _run_guard(guard_cmd, {"command": inner})
        assert result.returncode == 2, result.stdout + result.stderr

    @pytest.mark.parametrize(
        "inner",
        [
            f"grep -rn '{_PHRASE}' scripts/",
            f'echo "{_PHRASE} is blocked here"',
            f"git commit -m 'docs: explain why {_PHRASE} is gated'",
        ],
    )
    def test_the_mention_wins_survive_the_carrier_fallback(
        self, guard_cmd: str, inner: str
    ) -> None:
        """TRUE-NEGATIVE CONTROL, and the reason the fallback is ordered as it is.

        The carrier test runs only when the parser found NO target, so a mention
        is unaffected — there is no carrier in it. Without this, closing the
        carrier hole by falling back whenever the phrase appears would pass every
        test above while silently reverting the whole branch.
        """
        result = _run_guard(guard_cmd, {"command": inner})
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize(
        "inner",
        [
            f"rg '{_SUB} {_OP}' -l | xargs wc -l",
            f"find . -name '*.sh' -exec grep -l '{_SUB} {_OP}' {{}} +",
            f'echo "$(find . -name x) mentions {_SUB} {_OP} here"',
            f"ssh box 'ls' && echo 'the {_SUB} {_OP} doc'",
            f"docker ps && echo '{_SUB} {_OP} runbook step'",
        ],
    )
    def test_a_mention_that_HAS_a_carrier_is_still_allowed(
        self, guard_cmd: str, inner: str
    ) -> None:
        """The cases the class above could not see, and the reason they exist.

        Every mention case above is carrier-FREE, so none of them can detect a
        change to the carrier gate — they were green on both sides of one. These
        are mention + carrier: two read-only searches and three lines of prose
        that happen to sit next to `rg`/`find`/`ssh`/`docker`. MEASURED: all five
        blocked when the phrase pattern was widened without a separate `git`
        conjunct, and the coarse extractor then INVENTED the target from the
        following word ("Cannot remove worktree 'runbook'").

        What separates them from the carried removals above is that a removal
        names the executable it is about to run. That is the conjunct, and this
        is the half of it that no true-positive test can pin.
        """
        result = _run_guard(guard_cmd, {"command": inner})
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_carried_removal_behind_a_global_flag_blocks(self, guard_cmd: str) -> None:
        """TRUE-POSITIVE TWIN of the conjunct above — a carried removal keeps its
        `git` token, including the `git -C <dir>` spelling the phrase pattern was
        widened to reach in the first place. MEASURED allow before this branch."""
        inner = f'ssh box "git -C /x {_SUB} {_OP} /tmp/wt"'
        result = _run_guard(guard_cmd, {"command": inner})
        assert result.returncode == 2, result.stdout + result.stderr

    @pytest.mark.parametrize(
        "inner",
        [
            f"/usr/bin/find /tmp -name 'wt-*' -exec {_PHRASE} {{}} \\;",
            f"/usr/bin/eval '{_PHRASE} /tmp/wt-x'",
            f'/usr/bin/ssh box "{_PHRASE} /tmp/wt-x"',
        ],
    )
    def test_a_carrier_named_by_its_path_is_still_a_carrier(
        self, guard_cmd: str, inner: str
    ) -> None:
        """REGRESSION PIN vs origin/main, which blocked all three.

        The carrier test was a regex over the RAW text requiring the name to
        follow the start of the string or one of a few separators, so `/` did not
        end the preceding word and a path-qualified carrier matched nothing. The
        parser has already resolved and BASENAMED the executable by this point
        (`/usr/bin/find` -> `find`), so asking IT closes the whole list at once
        rather than one name per review round — and no new name has to be
        guessed, which is the property that makes it a class fix.
        """
        result = _run_guard(guard_cmd, {"command": inner})
        assert result.returncode == 2, result.stdout + result.stderr

    @pytest.mark.parametrize(
        "inner",
        [
            f"bash -ce '{_PHRASE} /tmp/wt-x'",
            f"bash -cx '{_PHRASE} /tmp/wt-x'",
            f"sh -ce '{_PHRASE} /tmp/wt-x'",
        ],
    )
    def test_a_shell_bundle_whose_c_is_not_last_still_blocks(
        self, guard_cmd: str, inner: str
    ) -> None:
        """REGRESSION PIN vs origin/main, which blocked all three.

        The parser treated a bundle whose `c` was not the final letter as an
        INLINE script — `-ce` yielded the script "e" — so the real script was
        never recursed into and the removal was allowed. MEASURED against the
        real interpreters, 2026-09-06: `bash -ce '<cmd>'` and `bash -cx '<cmd>'`
        RUN <cmd> from the NEXT token, while the glued spelling the old branch
        modelled (`bash -c'<cmd>'`, `sh -c'<cmd>'`, `dash -c'<cmd>'`) is refused
        outright with "invalid option" / "Illegal option". The branch modelled a
        form that does not exist and lost one that does.
        """
        result = _run_guard(guard_cmd, {"command": inner})
        assert result.returncode == 2, result.stdout + result.stderr

    @pytest.mark.parametrize(
        "inner",
        [f"bash -c '{_PHRASE} /tmp/wt-x'", f"bash -lc '{_PHRASE} /tmp/wt-x'"],
    )
    def test_the_bundle_spellings_that_already_worked_still_work(
        self, guard_cmd: str, inner: str
    ) -> None:
        """TWIN of the clause above — `c` alone and `c` last in the bundle both
        took the next token before and must still. A fix that moved the whole
        branch could satisfy the pin above while breaking these."""
        result = _run_guard(guard_cmd, {"command": inner})
        assert result.returncode == 2, result.stdout + result.stderr



# ---------------------------------------------------------------------------
# A removal whose target the guard cannot read is refused, not waved through
# ---------------------------------------------------------------------------

_UNREAD = "cannot read which worktree"

# Real removals whose worktree arrives from outside the text: on stdin or from a
# list (`analyze` unwraps `xargs` to a `git` segment with no path yet). Each was
# rc=0 on main before this change (measured against main's own guard), and each
# is refused with the unread-target message, which names no target.
_UNREAD_TARGET_REMOVALS = [
    # A: a parsed removal that names no worktree
    f"echo /tmp/wt-x | xargs {_PHRASE}",
    f"xargs -a list {_PHRASE}",
    f"find wt -print0 | xargs -0 {_PHRASE} --force",
    # The cell only rule A catches: the parser unwraps a path-qualified `xargs` to
    # the `git` it runs, so the carrier test sees no carrier name in the text.
    f"echo /tmp/wt-x | /usr/bin/xargs {_PHRASE}",
    f"echo /tmp/wt-x | sudo xargs {_PHRASE}",
    f"echo /tmp/wt-x | '/usr/bin/xargs' {_PHRASE}",
    f'echo /tmp/wt-x | "xargs" {_PHRASE}',
    f"echo /tmp/wt-x | parallel {_PHRASE}",
    # A here-document body a shell runs: the parser reads it as commands.
    f"bash <<'EOF'\necho /tmp/wt-x | xargs {_PHRASE}\nEOF",
    # B: a carrier whose payload names no worktree
    f"echo /tmp/wt-x | eval 'xargs {_PHRASE}'",
    # U: text the tokenizer cannot read (an apostrophe in a comment line the shell
    # skips), with the target on stdin
    "# it's a note\necho /tmp/wt-x | xargs " + _PHRASE,
]

# Real removals whose whole command is text handed to a shell or `source`, which
# the parser never reads as a segment (a here-string's word is dropped). The path
# IS in the text, so the coarse reader finds it and the ordinary refusal runs,
# self-directory and cross-session checks included. Each was rc=0 on main.
_UNREAD_PROGRAM_REMOVALS = [
    # C: a shell that reads its program from somewhere the parser does not
    f"bash <<< '{_PHRASE} /tmp/wt-x'",
    f"echo '{_PHRASE} /tmp/wt-x' | bash",
    f"printf '%s' '{_PHRASE} /tmp/wt-x' | sh -s",
    f"echo '{_PHRASE} /tmp/wt-x' | sudo bash -x",
    f"zsh <<< '{_PHRASE} /tmp/wt-x'",
    f"bash - <<< '{_PHRASE} /tmp/wt-x'",
    f"bash <(echo '{_PHRASE} /tmp/wt-x')",
    f"bash /dev/stdin <<< '{_PHRASE} /tmp/wt-x'",
    # `+c` runs its payload, but the parser flattens only a `-` bundle's `-c`.
    f"bash +c '{_PHRASE} /tmp/wt-x'",
    f"timeout 9 bash <<< '{_PHRASE} /tmp/wt-x'",
    f"env bash -s <<< '{_PHRASE} /tmp/wt-x'",
    # `-s` reads stdin even when positional words follow; `-o` takes a value.
    f"bash -s arg <<< '{_PHRASE} /tmp/wt-x'",
    f"bash -o pipefail <<< '{_PHRASE} /tmp/wt-x'",
    # A here-document fed to one shell must not exempt a piped shell beside it.
    f"bash <<'EOF'\necho harmless\nEOF\necho '{_PHRASE} /tmp/wt-x' | bash",
    # C: `source` / `.`, which run a file's text as shell
    f"source /dev/stdin <<< '{_PHRASE} /tmp/wt-x'",
    f". <(echo '{_PHRASE} /tmp/wt-x')",
    f"source <(echo '{_PHRASE} /tmp/wt-x')",
    f". -- /dev/stdin <<< '{_PHRASE} /tmp/wt-x'",
    f"echo '{_PHRASE} /tmp/wt-x' | source -- /dev/stdin",
]

# A launcher that supplies the OPERATION itself from stdin or a list: the parsed
# segment is a `git worktree` naming no operation. Each was rc=0 on main.
_UNREAD_OPERATION_REMOVALS = [
    f"echo {_OP} /tmp/wt-x | xargs git {_SUB}",
    f"echo {_OP} /tmp/wt-x | xargs -r git -C /r {_SUB}",
    f"echo {_OP} | xargs -I{{}} git {_SUB} {{}} /tmp/wt-x",
    # parallel substitutes `{}` without any option, so only the first-word rule sees it.
    f"echo {_OP} | parallel git {_SUB} {{}} /tmp/wt-x",
    f"parallel git {_SUB} ::: {_OP} ::: /tmp/wt-x",
    # A wrapper's option value spelled `git` must not hide the supplier after it.
    f"echo {_OP} /tmp/wt-x | env 'G=/usr/bin/git' xargs git {_SUB}",
    f"echo {_OP} /tmp/wt-x | sudo -u 'git' xargs git {_SUB}",
    # A placeholder spelled like an option, or like an operation name.
    f"printf '{_OP}\\n' | xargs -I -h git {_SUB} -h /tmp/wt-x",
    f"printf '{_OP}\\n' | xargs -I list git {_SUB} list /tmp/wt-x",
]

# Refused on main too, but only because the coarse reader took an operator
# (`<`) for the target. Kept as regression cells: the verdict must survive.
_ALREADY_REFUSED_REMOVALS = [
    f"parallel {_PHRASE} < list",
    f"xargs -r {_PHRASE} < list",
    f"xargs git -C /r {_SUB} {_OP} < list",
    f"xargs {_PHRASE} <<< /tmp/wt-x",
    # An escape-built word inside $'...': refused by the escape blind spot.
    "echo $'it\\'s' /tmp/wt-x | xargs " + _PHRASE,
]


class TestATargetTheGuardCannotReadIsRefused:
    """Every direct removal is refused whatever it targets, so a removal whose
    target the guard cannot read is refused too — with a message that names
    only what is known. Replaces the earlier known-gap pin for the stdin case."""

    @pytest.mark.parametrize("inner", _UNREAD_TARGET_REMOVALS)
    def test_a_removal_whose_target_the_guard_cannot_read_is_refused(
        self, guard_cmd: str, inner: str
    ) -> None:
        result = _run_guard(guard_cmd, {"command": inner})
        assert result.returncode == 2, result.stdout + result.stderr
        assert _UNREAD in result.stderr, result.stderr

    @pytest.mark.parametrize("inner", _UNREAD_PROGRAM_REMOVALS)
    def test_a_removal_handed_to_a_shell_or_source_is_refused(
        self, guard_cmd: str, inner: str
    ) -> None:
        result = _run_guard(guard_cmd, {"command": inner})
        assert result.returncode == 2, result.stdout + result.stderr

    @pytest.mark.parametrize("inner", _UNREAD_OPERATION_REMOVALS)
    def test_an_operation_supplied_by_a_launcher_is_refused(
        self, guard_cmd: str, inner: str
    ) -> None:
        result = _run_guard(guard_cmd, {"command": inner})
        assert result.returncode == 2, result.stdout + result.stderr
        assert _UNREAD in result.stderr, result.stderr
        assert "operation" in result.stderr, result.stderr

    @pytest.mark.parametrize("inner", _ALREADY_REFUSED_REMOVALS)
    def test_a_removal_refused_before_is_still_refused(
        self, guard_cmd: str, inner: str
    ) -> None:
        result = _run_guard(guard_cmd, {"command": inner})
        assert result.returncode == 2, result.stdout + result.stderr

    @pytest.mark.parametrize(
        "inner",
        [
            f"rg '{_SUB} {_OP}' -l | xargs wc -l",
            f"echo '{_PHRASE} docs' | tee notes.txt",
            f'gh pr edit 1 --body "docs about {_PHRASE}"',
            f"git commit -m 'doc {_PHRASE} usage'",
            f"cat <<'EOF' > notes.md\nprose about {_PHRASE}\nEOF",
            f"bash -c 'echo {_PHRASE}'",
            f"bash scripts/setup.sh && git {_SUB} list",
            f"source .venv/bin/activate && git {_SUB} list",
            # A shell or `source` running a named FILE is not a carrier: the
            # removal is not in text it runs. MEASURED false refusals before.
            f'source .venv/bin/activate && grep "{_PHRASE}" .',
            f'bash scripts/x.sh && echo "the {_PHRASE} doc"',
            f'bash -x s.sh | grep "{_PHRASE}"',
            # A bare `git worktree` is not reached through a launcher, and a
            # variable assignment before `git` is not a launcher either.
            f"git {_SUB}",
            f"cat <<'EOF' > notes.md\nGIT_DIR=x {_PHRASE}\nEOF",
            # House-style PR body: markdown backticks in a quoted here-document
            # parse as a `git` segment naming no worktree. Only a removal reached
            # through a launcher is refused for naming none.
            "gh pr create --title t --body \"$(cat <<'EOF'\n## Why\n"
            f"A bare `{_PHRASE}` with no path removes nothing.\nEOF\n)\"",
            f"git commit -F - <<'EOF'\nfix: document `{_PHRASE}`\nEOF",
            # A wrapper that supplies no words is not a launcher for this rule.
            f"git commit -F - <<'EOF'\nfix: `sudo {_PHRASE}` names nothing\nEOF",
            f"sudo git {_SUB}",
        ],
    )
    def test_ordinary_commands_near_the_new_carriers_still_run(
        self, guard_cmd: str, inner: str
    ) -> None:
        """TRUE-NEGATIVE CONTROLS. A shell given `-c` is not a carrier (the parser
        reads its payload), nor is a shell or `source` given a file to run; text
        that no shell or launcher runs is not a removal; and a removal naming no
        worktree is refused only when a launcher reaches it."""
        result = _run_guard(guard_cmd, {"command": inner})
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize(
        "inner",
        [
            f"bash <<'EOF'\ngrep -rn \"{_PHRASE}\" docs/\nEOF",
            f"sh <<'EOF'\ngit grep -n '{_SUB} {_OP}'\nEOF",
        ],
    )
    def test_a_here_document_fed_shell_is_a_carrier_known_cost(
        self, guard_cmd: str, inner: str
    ) -> None:
        """KNOWN COST, pinned. Segments keep no redirects, so which shell a
        here-document feeds cannot be told from the parse, and a shell reading its
        program from stdin is a carrier even when the body only mentions the
        removal. Exempting here-document-fed shells from the raw text exempted a
        piped shell beside one (review, round 1)."""
        result = _run_guard(guard_cmd, {"command": inner})
        assert result.returncode == 2, result.stdout + result.stderr

    def test_prose_naming_a_supplied_removal_gets_an_honest_refusal(
        self, guard_cmd: str
    ) -> None:
        """KNOWN COST, pinned. The parser keeps no here-document state, so prose that
        names the removal under `xargs` in a here-document reads exactly like a body a
        shell runs, and is refused. The refusal must not claim that anything runs or
        name a target, and must give the remedy for text."""
        cmd = f"git commit -F - <<'EOF'\nfix: refuse `xargs {_PHRASE}`\nEOF"
        result = _run_guard(guard_cmd, {"command": cmd})
        assert result.returncode == 2, result.stdout + result.stderr
        assert _UNREAD in result.stderr
        assert "if it runs" in result.stderr
        assert "Write tool" in result.stderr
        assert "Cannot remove worktree '" not in result.stderr

    def test_the_refusal_does_not_claim_a_removal_or_invent_a_target(
        self, guard_cmd: str
    ) -> None:
        """The same refusal also fires on text that merely MENTIONS the removal
        next to a carrier, so its wording must be true for prose too: no target
        is named, and the remedy for text that is not a command is given."""
        result = _run_guard(guard_cmd, {"command": f"eval 'echo {_PHRASE}'"})
        assert result.returncode == 2, result.stdout + result.stderr
        assert _UNREAD in result.stderr
        assert "Cannot remove worktree '" not in result.stderr
        assert "Write tool" in result.stderr


# ---------------------------------------------------------------------------
# A NON-TRUNCATING blind spot must not swap the parse for the raw-text regex
# ---------------------------------------------------------------------------

_WT = "work" + "tree"
_RM = "rem" + "ove"


class TestBlindSpotDoesNotDegradeToProse:
    """The legacy extractor reads RAW TEXT, so it cannot tell a command from a
    sentence. It is the right fallback for a cause that leaves NO segments — a
    bound — and the wrong one for a cause that leaves the segments complete.

    MEASURED when this guard degraded on any blind spot: a `gh pr` whose verb
    was a variable, with a --body whose prose merely mentions removing a
    worktree, went ALLOW -> hard BLOCK. Sessions write that PR body constantly;
    this PR's own body does.
    """

    def test_prose_in_a_pr_body_is_not_blocked(self, guard_cmd: str) -> None:
        cmd = f'gh pr "$ACTION" 123 --body "docs about {_WT} {_RM} semantics"'
        result = _run_guard(guard_cmd, {"command": cmd})
        assert result.returncode == 0, (
            "prose describing a worktree removal was BLOCKED because the command "
            f"also carried a blind spot.\n{result.stderr}"
        )

    def test_the_same_prose_with_a_readable_verb_is_the_control(
        self, guard_cmd: str
    ) -> None:
        """CONTROL: identical text, verb spelled out. If this blocked too, the
        test above would be measuring the prose rule and not the blind path."""
        cmd = f'gh pr edit 123 --body "docs about {_WT} {_RM} semantics"'
        result = _run_guard(guard_cmd, {"command": cmd})
        assert result.returncode == 0, result.stderr

    def test_a_real_removal_alongside_a_blind_spot_still_blocks(
        self, guard_cmd: str
    ) -> None:
        """The other direction, so this is not a licence to stop looking: an
        ACTUAL removal must still be refused when a blind spot is present."""
        # Path carries NO machine identity: this repository is public, and a
        # home path embedding a username is on the never-leak list alongside IPs
        # and hostnames — fixtures included, which is where it keeps slipping
        # through. The test needs a worktree-SHAPED argument and nothing more;
        # it never creates or resolves the path.
        cmd = f"git pus{{h..h}} origin main && git {_WT} {_RM} /tmp/wt/x"
        result = _run_guard(guard_cmd, {"command": cmd})
        assert result.returncode == 2, (
            f"a real worktree removal was allowed.\n{result.stdout}{result.stderr}"
        )
