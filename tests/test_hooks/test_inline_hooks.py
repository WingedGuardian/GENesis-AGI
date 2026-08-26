"""Tests for inline PreToolUse hooks defined in .claude/settings.json.

These hooks are the last line of defense against dangerous operations in
Claude Code sessions. They run as bash commands with CLAUDE_TOOL_INPUT set
to the JSON-serialized tool input.

Exit codes:
    0 = allowed (hook passes)
    2 = blocked (hook rejects the tool call)
"""

from __future__ import annotations

import pytest

from tests.test_hooks.conftest import run_hook

# ---------------------------------------------------------------------------
# Bash hook: pip install -e / --editable to worktree paths
# ---------------------------------------------------------------------------


class TestBashHookPipEditable:
    """Block pip install -e pointing to worktree directories."""

    def test_pip_install_e_worktree_blocked(self, bash_hook_command: str) -> None:
        """pip install -e ./.claude/worktrees/foo -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {"command": "pip install -e ./.claude/worktrees/foo"},
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_pip_install_editable_worktree_blocked(self, bash_hook_command: str) -> None:
        """pip install --editable ./.claude/worktrees/foo -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {"command": "pip install --editable ./.claude/worktrees/foo"},
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr
        assert "PYTHONPATH" in result.stderr  # suggests alternative

    def test_pip_install_e_absolute_worktree_blocked(self, bash_hook_command: str) -> None:
        """pip install -e /home/ubuntu/genesis/.claude/worktrees/my-branch -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {"command": ("pip install -e /home/ubuntu/genesis/.claude/worktrees/my-branch")},
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_pip_install_normal_package_allowed(self, bash_hook_command: str) -> None:
        """pip install requests -> allowed (no worktree, no -e)."""
        result = run_hook(bash_hook_command, {"command": "pip install requests"})
        assert result.returncode == 0

    def test_pip_install_e_non_worktree_allowed(self, bash_hook_command: str) -> None:
        """pip install -e ./src -> allowed (not a worktree path)."""
        result = run_hook(bash_hook_command, {"command": "pip install -e ./src"})
        assert result.returncode == 0

    def test_pip_install_e_with_extras_worktree_blocked(self, bash_hook_command: str) -> None:
        """pip install -e '.claude/worktrees/x[dev]' -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {"command": "pip install -e .claude/worktrees/x[dev]"},
        )
        assert result.returncode == 2


# ---------------------------------------------------------------------------
# Bash hook: genesis serve from/against a worktree
# ---------------------------------------------------------------------------


class TestBashHookWorktreeServe:
    """Block booting the full Genesis runtime from/against a worktree.

    Children inherit PYTHONPATH and path-keyed subsystems (LSP, indexers)
    treat the worktree as a new project — OOM-crashed the container on
    2026-07-03. (Like the pip -e tests above, the allowed cases assume a
    non-worktree cwd; the hook's cwd check is exercised in real sessions.)
    """

    def test_serve_with_worktree_pythonpath_blocked(self, bash_hook_command: str) -> None:
        """PYTHONPATH=<worktree>/src python -m genesis serve -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {
                "command": (
                    "PYTHONPATH=/home/ubuntu/genesis/.claude/worktrees/foo/src "
                    "python -m genesis serve --port 5000"
                )
            },
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr
        assert "merge-then-verify" in result.stderr  # suggests the alternative

    def test_serve_with_worktree_cd_blocked(self, bash_hook_command: str) -> None:
        """cd into a worktree && genesis serve -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {"command": ("cd .claude/worktrees/my-branch && python -m genesis serve --port 5050")},
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_systemctl_restart_genesis_server_allowed(self, bash_hook_command: str) -> None:
        """systemctl --user restart genesis-server -> allowed (not 'genesis serve')."""
        result = run_hook(
            bash_hook_command,
            {"command": "systemctl --user restart genesis-server"},
        )
        assert result.returncode == 0

    def test_journalctl_genesis_server_allowed(self, bash_hook_command: str) -> None:
        """journalctl --user -u genesis-server -> allowed."""
        result = run_hook(
            bash_hook_command,
            {"command": "journalctl --user -u genesis-server -n 50"},
        )
        assert result.returncode == 0

    def test_plain_serve_without_worktree_allowed(self, bash_hook_command: str) -> None:
        """python -m genesis serve (no worktree reference) -> allowed by THIS
        guard (the lock-file discipline for bare serves is a separate rule)."""
        result = run_hook(
            bash_hook_command,
            {"command": "python -m genesis serve --port 5000"},
        )
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Bash hook: git worktree remove --force / -f
# ---------------------------------------------------------------------------


class TestBashHookWorktreeForceRemove:
    """The INLINE mega-guard no longer owns `git worktree remove --force` (2026-08,
    PR-Guards): it duplicated worktree_cwd_guard.py (which blocks ALL `git worktree
    remove`, force or not — see tests/test_hooks/test_worktree_guard.py) and
    scripts/bash_safety_hook.sh (which still blocks the --force form). The inline
    guard now passes these through (returncode 0)."""

    def test_worktree_remove_force_not_inline_blocked(self, bash_hook_command: str) -> None:
        result = run_hook(
            bash_hook_command,
            {"command": "git worktree remove --force .claude/worktrees/foo"},
        )
        assert result.returncode == 0

    def test_worktree_remove_f_not_inline_blocked(self, bash_hook_command: str) -> None:
        result = run_hook(
            bash_hook_command,
            {"command": "git worktree remove -f .claude/worktrees/foo"},
        )
        assert result.returncode == 0

    def test_worktree_remove_without_force_allowed(self, bash_hook_command: str) -> None:
        """git worktree remove .claude/worktrees/foo -> allowed by the inline guard."""
        result = run_hook(
            bash_hook_command,
            {"command": "git worktree remove .claude/worktrees/foo"},
        )
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Bash hook: rm -rf on broad paths
# ---------------------------------------------------------------------------


class TestBashHookRmRf:
    """Block rm -rf on broad/dangerous paths.

    The rm-rf guard is a separate Python script (destructive_command_guard.py)
    that uses depth-based path analysis. Paths must be at least 4 components
    deep to be allowed. Special targets (., .., /) are always blocked.
    """

    def test_rm_rf_root_blocked(self, rm_rf_hook_command: str) -> None:
        """rm -rf / -> BLOCKED (always-block target)."""
        result = run_hook(rm_rf_hook_command, {"command": "rm -rf /"})
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_rm_rf_home_blocked(self, rm_rf_hook_command: str) -> None:
        """rm -rf ~ -> BLOCKED (depth 2 < 4)."""
        result = run_hook(rm_rf_hook_command, {"command": "rm -rf ~"})
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_rm_rf_dot_blocked(self, rm_rf_hook_command: str) -> None:
        """rm -rf . -> BLOCKED (always-block target)."""
        result = run_hook(rm_rf_hook_command, {"command": "rm -rf ."})
        assert result.returncode == 2

    def test_rm_rf_dotdot_blocked(self, rm_rf_hook_command: str) -> None:
        """rm -rf .. -> BLOCKED (always-block target)."""
        result = run_hook(rm_rf_hook_command, {"command": "rm -rf .."})
        assert result.returncode == 2

    def test_rm_rf_absolute_path_blocked(self, rm_rf_hook_command: str) -> None:
        """rm -rf /tmp/foo -> BLOCKED (depth 2 < 4)."""
        result = run_hook(rm_rf_hook_command, {"command": "rm -rf /tmp/foo"})
        assert result.returncode == 2

    def test_rm_rf_shallow_relative_blocked(self, rm_rf_hook_command: str) -> None:
        """rm -rf ./src -> BLOCKED (depth 1 < 4)."""
        result = run_hook(
            rm_rf_hook_command,
            {"command": "rm -rf ./src"},
        )
        assert result.returncode == 2

    def test_rm_rf_home_subpath_blocked(self, rm_rf_hook_command: str) -> None:
        """rm -rf ~/Downloads -> BLOCKED (depth 3 < 4)."""
        result = run_hook(rm_rf_hook_command, {"command": "rm -rf ~/Downloads"})
        assert result.returncode == 2

    def test_rm_rf_bare_dirname_blocked(self, rm_rf_hook_command: str) -> None:
        """rm -rf somedir -> BLOCKED (depth 1 < 4)."""
        result = run_hook(rm_rf_hook_command, {"command": "rm -rf somedir"})
        assert result.returncode == 2

    def test_rm_rf_deep_path_allowed(self, rm_rf_hook_command: str) -> None:
        """rm -rf ./some/specific/deep/path -> allowed (depth 4 >= 4).

        Paths with 4+ components are considered specific enough to allow.
        """
        result = run_hook(
            rm_rf_hook_command,
            {"command": "rm -rf ./some/specific/deep/path"},
        )
        assert result.returncode == 0

    def test_rm_r_no_force_allowed(self, rm_rf_hook_command: str) -> None:
        """rm -r / -> allowed (no -f flag, pattern requires both -r and -f)."""
        result = run_hook(rm_rf_hook_command, {"command": "rm -r /"})
        assert result.returncode == 0

    def test_rm_single_file_allowed(self, rm_rf_hook_command: str) -> None:
        """rm foo.txt -> allowed (no -rf)."""
        result = run_hook(rm_rf_hook_command, {"command": "rm foo.txt"})
        assert result.returncode == 0

    # -- Token-parse regressions: the 4 bypasses confirmed live by the
    # -- 2026-07-10 P1 triage (single-token regex missed all of these),
    # -- plus multi-operand and separator handling.

    def test_rm_split_flags_blocked(self, rm_rf_hook_command: str) -> None:
        """rm -r -f / -> BLOCKED (flags accumulate across tokens)."""
        result = run_hook(rm_rf_hook_command, {"command": "rm -r -f /"})
        assert result.returncode == 2

    def test_rm_long_flags_blocked(self, rm_rf_hook_command: str) -> None:
        """rm --recursive --force . -> BLOCKED."""
        result = run_hook(rm_rf_hook_command, {"command": "rm --recursive --force ."})
        assert result.returncode == 2

    def test_rm_capital_r_blocked(self, rm_rf_hook_command: str) -> None:
        """rm -Rf ~ -> BLOCKED (-R is recursive too)."""
        result = run_hook(rm_rf_hook_command, {"command": "rm -Rf ~"})
        assert result.returncode == 2

    def test_rm_double_dash_blocked(self, rm_rf_hook_command: str) -> None:
        """rm -rf -- / -> BLOCKED ('--' ends flags, operands still checked)."""
        result = run_hook(rm_rf_hook_command, {"command": "rm -rf -- /"})
        assert result.returncode == 2

    def test_rm_broad_second_operand_blocked(self, rm_rf_hook_command: str) -> None:
        """rm -rf deep/ok/nested/path / -> BLOCKED (each operand checked)."""
        result = run_hook(
            rm_rf_hook_command,
            {"command": "rm -rf ./some/specific/deep/path /"},
        )
        assert result.returncode == 2

    def test_rm_after_separator_blocked(self, rm_rf_hook_command: str) -> None:
        """echo ok && rm -r -f ~ -> BLOCKED (rm found past separators)."""
        result = run_hook(rm_rf_hook_command, {"command": "echo ok && rm -r -f ~"})
        assert result.returncode == 2

    def test_rm_unparseable_falls_back_to_regex(self, rm_rf_hook_command: str) -> None:
        """Unclosed quote (shlex fails) + classic spelling -> legacy block."""
        result = run_hook(rm_rf_hook_command, {"command": "rm -rf / 'unclosed"})
        assert result.returncode == 2

    def test_rm_split_flags_deep_path_allowed(self, rm_rf_hook_command: str) -> None:
        """rm -r -f on a 4+-deep path -> allowed (parity with -rf)."""
        result = run_hook(
            rm_rf_hook_command,
            {"command": "rm -r -f ./some/specific/deep/path"},
        )
        assert result.returncode == 0

    # -- 2026-07-10 review findings: leading-'..' traversal + abbreviated
    # -- GNU long flags were both live bypasses.

    @pytest.mark.parametrize(
        "target",
        [
            "../../../etc",
            "../../../../../../../../etc",  # bottoms out at /etc from root
            "../foo/bar/baz/qux",  # depth 4 textually, still traverses up
            "a/b/../../../../etc",  # interior '..' escapes past the base
        ],
    )
    def test_rm_rf_upward_traversal_blocked(self, rm_rf_hook_command: str, target: str) -> None:
        """rm -rf on any path whose normalized form keeps a '..' -> BLOCKED.

        A relative '..' cannot be depth-bounded without the real cwd, so
        the guard refuses (`../../../etc` used to report depth 4 and pass
        while resolving to /etc)."""
        result = run_hook(rm_rf_hook_command, {"command": f"rm -rf {target}"})
        assert result.returncode == 2

    def test_rm_rf_abbrev_long_flags_blocked(self, rm_rf_hook_command: str) -> None:
        """rm --rec --f / -> BLOCKED (GNU unambiguous prefix abbreviations)."""
        result = run_hook(rm_rf_hook_command, {"command": "rm --rec --f /"})
        assert result.returncode == 2

    def test_rm_non_destructive_long_flags_allowed(self, rm_rf_hook_command: str) -> None:
        """--dir/--verbose are not recursive+force -> deep path allowed."""
        result = run_hook(
            rm_rf_hook_command,
            {"command": "rm --dir --verbose /a/b/c/d/e"},
        )
        assert result.returncode == 0

    # -- Regression: shell metacharacters (redirections, newlines, line
    # -- continuations) were parsed as rm operands and spuriously blocked a
    # -- SAFE deep path, once #1227 revived the guard (found live 2026-07-24).
    # -- The fix must never let a dangerous rm through, so each 'allow' case is
    # -- paired with the dangerous variant that must still block.

    @pytest.mark.parametrize(
        "cmd",
        [
            "rm -rf /home/u/proj/node_modules 2>/dev/null",  # stderr redirect
            "rm -rf /a/b/c/d >log 2>&1",  # stdout redirect + fd dup
            "rm -rf /a/b/c/d 2>>errors.log",  # append redirect
            "rm -rf /a/b/c/d &>/dev/null",  # both-streams redirect
            "rm -rf /a/b/c/d <input",  # input redirect (glued)
        ],
    )
    def test_rm_rf_redirect_deep_path_allowed(self, rm_rf_hook_command: str, cmd: str) -> None:
        """A glued redirect after a safe deep path must not be read as a target."""
        result = run_hook(rm_rf_hook_command, {"command": cmd})
        assert result.returncode == 0, f"redirect false-positive: {cmd!r}\n{result.stderr}"

    @pytest.mark.parametrize(
        "cmd",
        [
            "rm -rf / 2>/dev/null",  # dangerous target, redirect stripped
            "rm -rf ~ >log",
            "rm -rf ../../../etc 2>/dev/null",  # upward traversal + redirect
        ],
    )
    def test_rm_rf_dangerous_with_redirect_still_blocked(
        self, rm_rf_hook_command: str, cmd: str
    ) -> None:
        """Stripping the redirect must not weaken the block on a real target."""
        result = run_hook(rm_rf_hook_command, {"command": cmd})
        assert result.returncode == 2, f"redirect strip weakened block: {cmd!r}"

    @pytest.mark.parametrize(
        "cmd",
        [
            'rm -r -f "a\\">"',  # backslash-escaped quote inside "..." (shlex: a">)
            'rm --recursive --force "a\\">"',  # same, long flags
            'rm -Rf "a>b"',  # operand does NOT start with a redirect op
        ],
    )
    def test_rm_rf_non_redirect_shaped_operand_still_blocked(
        self, rm_rf_hook_command: str, cmd: str
    ) -> None:
        """An operand that merely CONTAINS a redirect char but does not START with
        one (e.g. shlex resolves `"a\\">"` to `a">`) is a normal path and is still
        depth-checked and blocked.

        Regression: earlier fixes hand-rolled a pre-shlex redirect parser that
        diverged from shlex's quote/escape rules across three review rounds
        (2026-07-24), each a bypass. The final design delegates ALL quoting to
        shlex and skips a token only when it *starts with* a redirect operator."""
        result = run_hook(rm_rf_hook_command, {"command": cmd})
        assert result.returncode == 2, f"non-redirect-shaped operand allowed: {cmd!r}"

    def test_rm_rf_redirect_then_real_target_still_blocked(self, rm_rf_hook_command: str) -> None:
        """CRITICAL: skipping a redirect-shaped token must NEVER skip the FOLLOWING
        token — `rm -rf ">" /etc` must still depth-check and block `/etc`."""
        result = run_hook(rm_rf_hook_command, {"command": 'rm -rf ">" /etc'})
        assert result.returncode == 2

    def test_rm_rf_digit_glued_background_second_rm_blocked(self, rm_rf_hook_command: str) -> None:
        """A background `&` glued to a digit-ending word must still split, so a
        second `rm -rf /` after it is caught (not swallowed as one glued token).

        The separator lookbehind is `(?<![<>])` (no `\\d`): a bare digit before
        `&` is never part of a redirection, so the `&` is correctly spaced."""
        result = run_hook(rm_rf_hook_command, {"command": "rm -rf /a/b/c/d5&rm -rf /"})
        assert result.returncode == 2

    def test_rm_rf_redirect_target_deep_allowed(self, rm_rf_hook_command: str) -> None:
        """A quoted redirect TARGET glued to the operator after a safe deep path
        is still recognized as a redirect and skipped."""
        result = run_hook(
            rm_rf_hook_command,
            {"command": 'rm -rf /home/u/proj/build >"my file"'},
        )
        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize(
        "cmd", ['rm -rf ">log"', 'rm -r -f ">"', 'rm -rf ">etc"', 'rm -rf "<in"']
    )
    def test_rm_rf_redirect_shaped_filename_allowed(
        self, rm_rf_hook_command: str, cmd: str
    ) -> None:
        """DESIGN (user decision 2026-07-24, "simple + catastrophe-safe"): shlex
        erases the quoted/unquoted distinction, so a token that STARTS WITH a
        redirect operator is uniformly treated as a redirect and skipped — even a
        quoted file literally named `>etc`/`>`. This is deliberately accepted:
        such a token starts with `<`/`>`/`&>`, which no catastrophic target
        (`. .. / ~ *` or any real path) ever does, so nothing dangerous is
        missed; and blocking these would re-break the real `rm -rf /deep >log`
        false-positive. See _REDIR_TOKEN's safety proof in the guard."""
        result = run_hook(rm_rf_hook_command, {"command": cmd})
        assert result.returncode == 0, result.stderr

    def test_rm_rf_newline_separated_deep_allowed(self, rm_rf_hook_command: str) -> None:
        """A follow-on command after a newline must not fold into rm's operands.

        shlex drops a bare newline as whitespace, so before the fix `echo done`
        was depth-checked as a shallow rm target."""
        result = run_hook(
            rm_rf_hook_command,
            {"command": "rm -rf /home/u/proj/build\necho done"},
        )
        assert result.returncode == 0, result.stderr

    def test_rm_rf_line_continuation_deep_allowed(self, rm_rf_hook_command: str) -> None:
        r"""A `\`-newline continuation inside one rm invocation stays one path."""
        result = run_hook(
            rm_rf_hook_command,
            {"command": "rm -rf \\\n/home/u/proj/build"},
        )
        assert result.returncode == 0, result.stderr

    def test_rm_rf_background_deep_allowed(self, rm_rf_hook_command: str) -> None:
        """A trailing `&` (background) must not be read as an rm target."""
        result = run_hook(rm_rf_hook_command, {"command": "rm -rf /a/b/c/d &"})
        assert result.returncode == 0, result.stderr


# NOTE: the run_in_background pipe check moved OUT of the inline mega-guard into a
# dedicated Python hook (scripts/hooks/background_pipe_guard.py, quote/redirect-aware
# via shell_parse.has_top_level_pipe) — see tests/test_hooks/test_background_pipe_guard.py.
# The inline guard no longer reads run_in_background.


# ---------------------------------------------------------------------------
# Bash hook: git push --force / -f
# ---------------------------------------------------------------------------


class TestBashHookGitPushForce:
    """The INLINE mega-guard no longer owns force-push detection (2026-08,
    PR-Guards): that whole-command substring check carried a false positive
    (`git push origin main && rm -f x`), so it was removed. Force push is now
    owned by the tracked git_push_guard.py (argv-based, hard-blocks force to
    origin — see tests/test_hooks/test_push_create_override.py) and by
    scripts/bash_safety_hook.sh (segment-scoped — see
    tests/test_scripts/test_bash_safety_hook.py). These assert the inline guard
    PASSES force-push commands through (returncode 0); it is not the owner."""

    def test_git_push_force_not_inline_blocked(self, bash_hook_command: str) -> None:
        result = run_hook(bash_hook_command, {"command": "git push --force origin main"})
        assert result.returncode == 0

    def test_git_push_f_not_inline_blocked(self, bash_hook_command: str) -> None:
        result = run_hook(bash_hook_command, {"command": "git push -f"})
        assert result.returncode == 0

    def test_git_push_f_with_remote_not_inline_blocked(self, bash_hook_command: str) -> None:
        result = run_hook(bash_hook_command, {"command": "git push -f origin feature"})
        assert result.returncode == 0

    def test_git_push_u_then_f_not_inline_blocked(self, bash_hook_command: str) -> None:
        result = run_hook(bash_hook_command, {"command": "git push -u origin -f main"})
        assert result.returncode == 0

    def test_git_push_force_with_lease_not_inline_blocked(self, bash_hook_command: str) -> None:
        result = run_hook(bash_hook_command, {"command": "git push --force-with-lease origin main"})
        assert result.returncode == 0

    def test_push_then_rm_f_fp_gone(self, bash_hook_command: str) -> None:
        """The exact FP that motivated the removal: a plain push next to an
        unrelated `rm -f` no longer false-blocks at the inline guard."""
        result = run_hook(bash_hook_command, {"command": "git push origin main && rm -f /tmp/x"})
        assert result.returncode == 0

    def test_git_push_normal_allowed(self, bash_hook_command: str) -> None:
        """git push origin feature-branch -> allowed (no force)."""
        result = run_hook(
            bash_hook_command,
            {"command": "git push origin feature-branch"},
        )
        assert result.returncode == 0

    def test_git_push_branch_name_with_dash_f_allowed(self, bash_hook_command: str) -> None:
        """A branch name containing '-f' is NOT a force flag -> allowed.

        Regression: the old pattern *"-f"* matched the '-f' inside a branch
        name like 'fix/...-false-positives'; requiring a space before the flag
        (*" -f"*) fixes the false-positive without letting a real -f through.
        """
        result = run_hook(
            bash_hook_command,
            {"command": "git push origin fix/guard-false-positives"},
        )
        assert result.returncode == 0

    def test_git_push_branch_add_foo_allowed(self, bash_hook_command: str) -> None:
        """'-f' inside 'add-foo' must not trip the force gate -> allowed."""
        result = run_hook(
            bash_hook_command,
            {"command": "git push origin feature/add-foo"},
        )
        assert result.returncode == 0

    def test_git_push_u_allowed(self, bash_hook_command: str) -> None:
        """git push -u origin feature -> allowed (-u is not -f)."""
        result = run_hook(
            bash_hook_command,
            {"command": "git push -u origin feature"},
        )
        assert result.returncode == 0

    def test_git_push_no_args_allowed(self, bash_hook_command: str) -> None:
        """git push -> allowed."""
        result = run_hook(bash_hook_command, {"command": "git push"})
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Bash hook: git reset --hard
# ---------------------------------------------------------------------------


class TestBashHookGitResetHard:
    """Block git reset --hard."""

    def test_git_reset_hard_blocked(self, bash_hook_command: str) -> None:
        """git reset --hard -> BLOCKED."""
        result = run_hook(bash_hook_command, {"command": "git reset --hard"})
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr
        assert "git stash" in result.stderr  # suggests alternative

    def test_git_reset_hard_with_ref_blocked(self, bash_hook_command: str) -> None:
        """git reset --hard HEAD~3 -> BLOCKED."""
        result = run_hook(bash_hook_command, {"command": "git reset --hard HEAD~3"})
        assert result.returncode == 2

    def test_git_reset_hard_origin_blocked(self, bash_hook_command: str) -> None:
        """git reset --hard origin/main -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {"command": "git reset --hard origin/main"},
        )
        assert result.returncode == 2

    def test_git_reset_soft_allowed(self, bash_hook_command: str) -> None:
        """git reset --soft HEAD~1 -> allowed."""
        result = run_hook(bash_hook_command, {"command": "git reset --soft HEAD~1"})
        assert result.returncode == 0

    def test_git_reset_mixed_allowed(self, bash_hook_command: str) -> None:
        """git reset HEAD~1 -> allowed (default mixed mode)."""
        result = run_hook(bash_hook_command, {"command": "git reset HEAD~1"})
        assert result.returncode == 0

    def test_git_reset_no_args_allowed(self, bash_hook_command: str) -> None:
        """git reset -> allowed (unstages all)."""
        result = run_hook(bash_hook_command, {"command": "git reset"})
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Bash hook: git clean -f / -fd
# ---------------------------------------------------------------------------


class TestBashHookGitClean:
    """Block git clean with force flags."""

    def test_git_clean_f_blocked(self, bash_hook_command: str) -> None:
        """git clean -f -> BLOCKED."""
        result = run_hook(bash_hook_command, {"command": "git clean -f"})
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_git_clean_fd_blocked(self, bash_hook_command: str) -> None:
        """git clean -fd -> BLOCKED."""
        result = run_hook(bash_hook_command, {"command": "git clean -fd"})
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_git_clean_fdx_blocked(self, bash_hook_command: str) -> None:
        """git clean -fdx -> BLOCKED (contains 'git clean -fd')."""
        result = run_hook(bash_hook_command, {"command": "git clean -fdx"})
        assert result.returncode == 2

    def test_git_clean_fx_blocked(self, bash_hook_command: str) -> None:
        """git clean -fx -> BLOCKED (contains 'git clean -f')."""
        result = run_hook(bash_hook_command, {"command": "git clean -fx"})
        assert result.returncode == 2

    def test_git_clean_n_allowed(self, bash_hook_command: str) -> None:
        """git clean -n -> allowed (dry run, no -f)."""
        result = run_hook(bash_hook_command, {"command": "git clean -n"})
        assert result.returncode == 0

    def test_git_clean_nd_allowed(self, bash_hook_command: str) -> None:
        """git clean -nd -> allowed (dry run with directories)."""
        result = run_hook(bash_hook_command, {"command": "git clean -nd"})
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Bash hook: benign commands (should all pass)
# ---------------------------------------------------------------------------


class TestBashHookBenignCommands:
    """Normal commands must not be blocked."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "ls -la",
            "python -m pytest",
            "git status",
            "git diff --cached",
            "git log --oneline -10",
            "git add src/genesis/foo.py",
            "git commit -m 'fix: something'",
            "cat /etc/hostname",
            "ruff check .",
            "pip install requests httpx",
            "pip install -r requirements.txt",
            "source ~/genesis/.venv/bin/activate",
            "curl -s http://localhost:6333/collections",
            "echo hello world",
            "cd /home/ubuntu/genesis && pytest -v",
            "PYTHONPATH=.claude/worktrees/foo/src pytest tests/",
        ],
        ids=[
            "ls",
            "pytest",
            "git-status",
            "git-diff",
            "git-log",
            "git-add",
            "git-commit",
            "cat",
            "ruff",
            "pip-install-packages",
            "pip-install-requirements",
            "source-venv",
            "curl",
            "echo",
            "cd-and-pytest",
            "pythonpath-worktree",
        ],
    )
    def test_benign_command_allowed(self, bash_hook_command: str, cmd: str) -> None:
        """Normal commands pass through the hook."""
        result = run_hook(bash_hook_command, {"command": cmd})
        assert result.returncode == 0, (
            f"Benign command was blocked: {cmd!r}\nstderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# Bash hook: error message quality
# ---------------------------------------------------------------------------


class TestBashHookErrorMessages:
    """Verify hook stderr contains actionable guidance."""

    def test_pip_editable_suggests_pythonpath(self, bash_hook_command: str) -> None:
        result = run_hook(
            bash_hook_command,
            {"command": "pip install -e .claude/worktrees/branch"},
        )
        assert result.returncode == 2
        assert "PYTHONPATH" in result.stderr
        assert "worktree" in result.stderr.lower()

    # (force-push is no longer owned by the inline guard — its PR-suggesting
    # message now lives in git_push_guard.py / bash_safety_hook.sh.)

    def test_reset_hard_suggests_stash(self, bash_hook_command: str) -> None:
        result = run_hook(bash_hook_command, {"command": "git reset --hard"})
        assert result.returncode == 2
        assert "stash" in result.stderr

    def test_git_clean_suggests_user(self, bash_hook_command: str) -> None:
        result = run_hook(bash_hook_command, {"command": "git clean -f"})
        assert result.returncode == 2
        assert "user" in result.stderr.lower()

    def test_rm_rf_suggests_confirm(self, rm_rf_hook_command: str) -> None:
        """rm -rf on shallow path suggests asking the user to confirm."""
        result = run_hook(rm_rf_hook_command, {"command": "rm -rf /tmp"})
        assert result.returncode == 2
        assert "user" in result.stderr.lower() or "confirm" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Bash hook: edge cases and combined commands
# ---------------------------------------------------------------------------


class TestBashHookEdgeCases:
    """Edge cases for the Bash hook."""

    def test_empty_command_allowed(self, bash_hook_command: str) -> None:
        """Empty command -> allowed."""
        result = run_hook(bash_hook_command, {"command": ""})
        assert result.returncode == 0

    def test_multiline_command_with_blocked(self, bash_hook_command: str) -> None:
        """Multiline command containing git reset --hard -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {"command": "echo hello\ngit reset --hard\necho done"},
        )
        assert result.returncode == 2

    def test_multiline_command_with_rm_rf_blocked(self, rm_rf_hook_command: str) -> None:
        """Multiline command containing rm -rf / -> BLOCKED."""
        result = run_hook(
            rm_rf_hook_command,
            {"command": "echo hello\nrm -rf /\necho done"},
        )
        assert result.returncode == 2

    def test_chained_command_with_blocked(self, bash_hook_command: str) -> None:
        """Command chained with && containing an inline-owned blocked op -> BLOCKED.

        (Uses `git reset --hard`, still owned by the inline guard; force-push
        moved to git_push_guard/bash_safety.)"""
        result = run_hook(
            bash_hook_command,
            {"command": "ls -la && git reset --hard"},
        )
        assert result.returncode == 2

    def test_piped_command_with_blocked(self, bash_hook_command: str) -> None:
        """Piped command containing an inline-owned blocked op -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {"command": "echo yes | git reset --hard"},
        )
        assert result.returncode == 2

    def test_subshell_with_blocked(self, bash_hook_command: str) -> None:
        """Subshell containing blocked op -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {"command": "$(git reset --hard)"},
        )
        assert result.returncode == 2

    def test_malformed_json_input(self, bash_hook_command: str) -> None:
        """Malformed JSON on stdin -> graceful (jq fails, CMD empty, no crash)."""
        import os
        import subprocess

        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_TOOL_INPUT"}
        result = subprocess.run(
            bash_hook_command,
            shell=True,
            input="not-json{{{",
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Should not crash — either exit 0 (pass) or handle gracefully
        assert result.returncode in (0, 2)

    def test_missing_command_field(self, bash_hook_command: str) -> None:
        """Payload without a command field -> jq returns empty, hook passes."""
        result = run_hook(bash_hook_command, {"url": "https://example.com"})
        assert result.returncode == 0

    def test_no_stdin_payload(self, bash_hook_command: str) -> None:
        """Empty stdin (no payload) -> hook handles gracefully."""
        import os
        import subprocess

        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_TOOL_INPUT"}
        result = subprocess.run(
            bash_hook_command,
            shell=True,
            input="",
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Should not crash
        assert result.returncode in (0, 2)


# ---------------------------------------------------------------------------
# WebFetch hook: YouTube URL blocking
# ---------------------------------------------------------------------------


class TestWebFetchHookYouTubeBlocking:
    """Block YouTube URLs in WebFetch."""

    def test_youtube_watch_blocked(self, webfetch_hook_command: str) -> None:
        """https://www.youtube.com/watch?v=abc -> BLOCKED."""
        result = run_hook(
            webfetch_hook_command,
            {"url": "https://www.youtube.com/watch?v=abc"},
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr
        assert "YouTube" in result.stderr

    def test_youtube_short_url_blocked(self, webfetch_hook_command: str) -> None:
        """https://youtu.be/abc123 -> BLOCKED."""
        result = run_hook(webfetch_hook_command, {"url": "https://youtu.be/abc123"})
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_youtube_no_www_blocked(self, webfetch_hook_command: str) -> None:
        """https://youtube.com/watch?v=xyz -> BLOCKED."""
        result = run_hook(
            webfetch_hook_command,
            {"url": "https://youtube.com/watch?v=xyz"},
        )
        assert result.returncode == 2

    def test_youtube_uppercase_blocked(self, webfetch_hook_command: str) -> None:
        """https://www.YOUTUBE.COM/watch?v=abc -> BLOCKED (case-insensitive)."""
        result = run_hook(
            webfetch_hook_command,
            {"url": "https://www.YOUTUBE.COM/watch?v=abc"},
        )
        assert result.returncode == 2

    def test_youtube_mixed_case_blocked(self, webfetch_hook_command: str) -> None:
        """https://YouTube.com/playlist?list=PL... -> BLOCKED."""
        result = run_hook(
            webfetch_hook_command,
            {"url": "https://YouTube.com/playlist?list=PLabc"},
        )
        assert result.returncode == 2

    def test_youtube_embed_blocked(self, webfetch_hook_command: str) -> None:
        """https://www.youtube.com/embed/abc -> BLOCKED."""
        result = run_hook(
            webfetch_hook_command,
            {"url": "https://www.youtube.com/embed/abc123"},
        )
        assert result.returncode == 2

    def test_youtu_be_mixed_case_blocked(self, webfetch_hook_command: str) -> None:
        """https://YOUTU.BE/abc -> BLOCKED."""
        result = run_hook(webfetch_hook_command, {"url": "https://YOUTU.BE/abc123"})
        assert result.returncode == 2


# ---------------------------------------------------------------------------
# WebFetch hook: allowed URLs
# ---------------------------------------------------------------------------


class TestWebFetchHookAllowedUrls:
    """Non-YouTube URLs must pass through."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com",
            "https://github.com/example-org/example-repo",
            "https://docs.python.org/3/library/asyncio.html",
            "https://api.openai.com/v1/models",
            "https://www.google.com/search?q=python",
            "https://stackoverflow.com/questions/12345",
            "https://vimeo.com/12345",  # video site, but not YouTube
            "https://dailymotion.com/video/abc",
            "https://httpbin.org/get",
        ],
        ids=[
            "example",
            "github",
            "python-docs",
            "openai-api",
            "google",
            "stackoverflow",
            "vimeo",
            "dailymotion",
            "httpbin",
        ],
    )
    def test_non_youtube_allowed(self, webfetch_hook_command: str, url: str) -> None:
        """Non-YouTube URLs pass through the hook."""
        result = run_hook(webfetch_hook_command, {"url": url})
        assert result.returncode == 0, (
            f"Non-YouTube URL was blocked: {url!r}\nstderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# WebFetch hook: error message quality
# ---------------------------------------------------------------------------


class TestWebFetchHookErrorMessages:
    """Verify YouTube block message contains actionable guidance."""

    def test_suggests_yt_dlp(self, webfetch_hook_command: str) -> None:
        """Error message suggests yt-dlp as alternative."""
        result = run_hook(
            webfetch_hook_command,
            {"url": "https://www.youtube.com/watch?v=test"},
        )
        assert result.returncode == 2
        assert "yt-dlp" in result.stderr

    def test_mentions_ssl(self, webfetch_hook_command: str) -> None:
        """Error message explains the SSL root cause."""
        result = run_hook(
            webfetch_hook_command,
            {"url": "https://www.youtube.com/watch?v=test"},
        )
        assert "SSL" in result.stderr

    def test_shows_transcript_example(self, webfetch_hook_command: str) -> None:
        """Error message includes transcript extraction example."""
        result = run_hook(
            webfetch_hook_command,
            {"url": "https://www.youtube.com/watch?v=test"},
        )
        assert "write-auto-sub" in result.stderr or "transcript" in result.stderr.lower()


# ---------------------------------------------------------------------------
# WebFetch hook: edge cases
# ---------------------------------------------------------------------------


class TestWebFetchHookEdgeCases:
    """Edge cases for the WebFetch hook."""

    def test_empty_url_allowed(self, webfetch_hook_command: str) -> None:
        """Empty URL -> allowed (no match)."""
        result = run_hook(webfetch_hook_command, {"url": ""})
        assert result.returncode == 0

    def test_missing_url_field(self, webfetch_hook_command: str) -> None:
        """JSON without 'url' field -> jq returns null, hook passes."""
        result = run_hook(webfetch_hook_command, {"command": "ls"})
        assert result.returncode == 0

    def test_youtube_in_query_param_blocked(self, webfetch_hook_command: str) -> None:
        """URL with youtube.com in the domain -> BLOCKED even with params."""
        result = run_hook(
            webfetch_hook_command,
            {"url": "https://www.youtube.com/results?search_query=python"},
        )
        assert result.returncode == 2

    def test_url_containing_youtube_as_substring_blocked(self, webfetch_hook_command: str) -> None:
        """notyoutube.com contains 'youtube.com' substring -> BLOCKED.

        The grep pattern matches any URL containing the substring
        'youtube.com', including domains like notyoutube.com. This is
        a known acceptable false positive — it's better to over-block
        than to miss actual YouTube URLs.
        """
        result = run_hook(
            webfetch_hook_command,
            {"url": "https://notyoutube.com/video"},
        )
        # This IS blocked because grep matches the substring
        assert result.returncode == 2


# ---------------------------------------------------------------------------
# Settings.json structure validation
# ---------------------------------------------------------------------------


class TestSettingsStructure:
    """Validate that .claude/settings.json has the expected hook structure."""

    def test_has_pretooluse_hooks(self, settings: dict) -> None:
        """settings.json contains PreToolUse hooks section."""
        assert "hooks" in settings
        assert "PreToolUse" in settings["hooks"]
        assert isinstance(settings["hooks"]["PreToolUse"], list)

    def test_has_bash_matcher(self, settings: dict) -> None:
        """PreToolUse section has a Bash matcher entry."""
        matchers = [h.get("matcher") for h in settings["hooks"]["PreToolUse"]]
        assert "Bash" in matchers

    def test_has_webfetch_matcher(self, settings: dict) -> None:
        """PreToolUse section has a WebFetch matcher entry."""
        matchers = [h.get("matcher") for h in settings["hooks"]["PreToolUse"]]
        assert "WebFetch" in matchers

    def test_bash_hook_is_command(self, settings: dict) -> None:
        """Bash hooks are type=command (inline bash -c or Python script)."""
        for entry in settings["hooks"]["PreToolUse"]:
            if entry.get("matcher") == "Bash":
                hooks = entry["hooks"]
                commands = [
                    h for h in hooks if h.get("type") == "command" and h.get("command", "").strip()
                ]
                assert len(commands) >= 1, "No command hook found for Bash matcher"

    def test_webfetch_hook_is_inline_command(self, settings: dict) -> None:
        """WebFetch hook is type=command and starts with 'bash -c'."""
        for entry in settings["hooks"]["PreToolUse"]:
            if entry.get("matcher") == "WebFetch":
                hooks = entry["hooks"]
                inline = [
                    h
                    for h in hooks
                    if h.get("type") == "command" and h.get("command", "").startswith("bash -c")
                ]
                assert len(inline) >= 1, "No inline WebFetch hook found"

    def test_bash_hook_checks_all_expected_patterns(self, settings: dict) -> None:
        """Bash hooks collectively cover all expected danger patterns.

        The inline bash hook handles git/pip patterns. The rm-rf guard is
        a separate Python script referenced via destructive_command_guard.
        """
        import re
        from pathlib import Path

        repo_root = next(
            (a for a in Path(__file__).resolve().parents if (a / "scripts" / "hooks").is_dir()),
            None,
        )

        # Collect all Bash hook commands AND every referenced hooks/<name>.py
        # guard script — force-push/worktree checks moved OUT of the inline blob
        # into the dedicated guards (git_push_guard.py, worktree_cwd_guard.py),
        # so "collectively covered" must include their source.
        combined = ""
        for entry in settings["hooks"]["PreToolUse"]:
            if entry.get("matcher") != "Bash":
                continue
            for hook in entry.get("hooks", []):
                cmd = hook.get("command", "")
                combined += cmd + "\n"
                if repo_root:
                    for name in re.findall(r"hooks/(\w+)\.py", cmd):
                        script = repo_root / "scripts" / "hooks" / f"{name}.py"
                        if script.exists():
                            combined += script.read_text()

        assert "pip install" in combined
        assert "worktree" in combined
        assert "rm" in combined and "rf" in combined  # rm -rf in the destructive guard
        assert "git push" in combined  # git_push_guard.py
        assert "--force" in combined or "force" in combined
        assert "git reset --hard" in combined  # inline blob (kept)
        assert "git clean" in combined  # inline blob (kept)

    def test_webfetch_hook_checks_youtube(self, webfetch_hook_command: str) -> None:
        """WebFetch hook command contains YouTube pattern check."""
        assert "youtube" in webfetch_hook_command.lower()
        assert "youtu.be" in webfetch_hook_command.lower() or "youtu\\.be" in webfetch_hook_command
