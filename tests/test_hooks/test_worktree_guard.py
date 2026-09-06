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

    def test_a_target_arriving_on_stdin_is_a_known_gap_in_every_version(
        self, guard_cmd: str
    ) -> None:
        """NOT a regression — MEASURED allow on the pre-parser version too.

        Piping a path into `xargs` puts the target BEFORE the phrase, so the
        coarse extractor (which reads forward from its match) finds nothing
        either. Neither predicate has ever caught this shape. Asserted as
        current behaviour so the gap is visible rather than implicit; if this
        starts failing the gap was closed and this test should go.
        """
        inner = f"echo /tmp/wt-x | xargs {_PHRASE}"
        result = _run_guard(guard_cmd, {"command": inner})
        assert result.returncode == 0, result.stdout + result.stderr
