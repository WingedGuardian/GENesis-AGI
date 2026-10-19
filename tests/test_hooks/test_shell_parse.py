"""Unit tests for scripts/hooks/shell_parse.py — the shared guard command parser.

The parser decides what a Bash command actually executes (real subcommands,
flags) and where an approval override binds. A miss here is a guard bypass, so
these lock in the wrapper/substitution/nesting cases two review passes found.
"""

from __future__ import annotations

import ast
import inspect
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS / "hooks"))
import pr_close_predicate as pcp  # noqa: E402
import shell_parse as sp  # noqa: E402

NV = "--no" + "-verify"  # keep the literal out of this file's own text


def _push_blocked(cmd: str) -> bool:
    segs = sp.executes(cmd, "git", "push")
    return bool(segs) and not all(s.override for s in segs)


def _commit_nv(cmd: str) -> bool:
    return any(sp.commit_skips_hooks(s.argv) for s in sp.analyze(cmd))


# ── pytest-invocation detection (shared by the test guards) ─────────────


class TestPytestDetection:
    def test_bare_pytest(self):
        assert sp.command_runs_pytest("pytest tests/")

    def test_python_m_pytest(self):
        assert sp.command_runs_pytest("python -m pytest tests/x.py")

    def test_env_prefixed(self):
        assert sp.command_runs_pytest("PYTHONPATH=src pytest tests/")

    def test_chained(self):
        assert sp.command_runs_pytest("ruff check . && pytest -q")

    def test_venv_path_entrypoint(self):
        seg = sp.analyze("/venv/bin/pytest -q")[0]
        assert sp.is_pytest_invocation(seg)

    def test_grep_word_not_matched(self):
        assert not sp.command_runs_pytest("grep pytest scripts/foo.py")

    def test_pytest_in_quoted_pipe_pattern_not_matched(self):
        # the quoted-| false positive: `|pytest` inside a grep pattern is not a run
        assert not sp.command_runs_pytest('grep -rniE "full_suite|pytest|x" f')
        assert not sp.command_runs_pytest("rg -n 'a|pytest|b' scripts/")

    def test_pytest_cov_module_not_matched(self):
        assert not sp.command_runs_pytest("python -m pytest_cov")

    def test_python_script_with_m_pytest_args_not_matched(self):
        # `-m pytest` here are the SCRIPT's own args, not python's — not a pytest run
        assert not sp.command_runs_pytest("python script.py -m pytest")

    def test_python_running_pytest_entrypoint_matched(self):
        # python executing the pytest console-script (a /path/.../pytest) IS a run
        seg = sp.analyze("python /venv/bin/pytest -q")[0]
        assert sp.is_pytest_invocation(seg)

    def test_python_flags_before_m_pytest_matched(self):
        assert sp.command_runs_pytest("python -X faulthandler -m pytest tests/x.py")


# ── git subcommand resolution through wrappers ──────────────────────────


class TestExecutableResolution:
    def test_plain(self):
        assert sp.git_subcommand(["git", "push"]) == "push"

    def test_path_prefix(self):
        assert _push_blocked("/usr/bin/git push origin main")

    def test_global_opt_with_value(self):
        assert sp.git_subcommand(["git", "-c", "user.name=x", "push"]) == "push"

    def test_sudo_user_flag(self):
        assert _push_blocked("sudo -u root git push")

    def test_timeout_positional(self):
        assert _push_blocked("timeout 5 git push")

    def test_nice_flag(self):
        assert _push_blocked("nice -n 10 git push")

    def test_chrt_priority_positional(self):
        assert _push_blocked("chrt -r 50 git push")

    def test_env_assignment(self):
        assert _push_blocked("env FOO=1 git push")

    def test_quoted_mention_not_executed(self):
        assert not _push_blocked('echo "git push"')

    def test_git_status_not_push(self):
        assert not _push_blocked("git status")


# ── nested scripts + command substitution ───────────────────────────────


class TestNesting:
    def test_bash_c(self):
        assert _commit_nv("bash -c 'git commit -n -m wip'")

    def test_bash_lc_bundle(self):
        assert _commit_nv("bash -lc 'git commit -n -m wip'")

    def test_bash_ce_bundle(self):
        assert any(s.exe == "echo" for s in sp.analyze("bash -ce 'echo hello'"))

    def test_zsh_Gc_bundle_recurse(self):
        assert any(
            s.exe == "git" and sp.git_subcommand(s.argv) == "push"
           for s in sp.analyze("zsh -Gc 'git push origin main'")
        )

    def test_bash_cl_bundle(self):
        assert any(s.exe == "echo" for s in sp.analyze("bash -cl 'echo hello'"))

    def test_bash_co_value_taking_option_bundle(self):
        assert any(s.exe == "echo" for s in sp.analyze("bash -co pipefail 'echo hello'"))

    def test_bash_Oc_value_taking_option_bundle(self):assert any(s.exe == "echo" for s in sp.analyze("bash -Oc extglob 'echo hello'"))

    def test_bash_ec_bundle_still_works(self):
        assert any(s.exe == "echo" for s in sp.analyze("bash -ec 'echo hello'"))

    @pytest.mark.parametrize("interpreter", ["dash", "sh"])
    @pytest.mark.parametrize("options", ["cC", "Cc", "cE", "Ec", "cI", "Ic", "cV", "Vc"])
    def test_dash_valid_uppercase_c_bundles_recurse(self, interpreter, options):
        assert any(s.exe == "echo" for s in sp.analyze(f"{interpreter} -{options} 'echo hello'"))

    @pytest.mark.parametrize(
        "command",
        [
            "bash -- -ce 'echo hidden'",
            "bash - -ce 'echo hidden'",
            "bash -cz 'echo hidden'",
        ],
    )
    def test_interpreter_non_options_and_invalid_bundles_do_not_recurse(self, command):
        assert not any(s.exe == "echo" for s in sp.analyze(command))

    def test_command_substitution(self):
        assert _push_blocked('echo "$(git push origin main)"')

    def test_assignment_substitution(self):
        assert _push_blocked("x=$(git push)")

    def test_backtick_substitution(self):
        assert _push_blocked("echo `git push`")


# ── override binding ────────────────────────────────────────────────────


class TestOverride:
    def test_trailing_comment(self):
        segs = sp.analyze("git push origin f # review-override")
        assert segs[0].override

    def test_binds_per_segment(self):
        segs = sp.analyze("git push origin f # review-override\ngh pr create --title x")
        push = [s for s in segs if sp.git_subcommand(s.argv) == "push"]
        create = [s for s in segs if sp.gh_pr_subcommand(s.argv) == "create"]
        assert push[0].override
        assert not create[0].override

    def test_in_quoted_string_does_not_count(self):
        segs = sp.analyze('git commit -m "wip # review-override"')
        assert not segs[0].override

    def test_exact_token_only(self):
        assert not sp.analyze("git push # review-override-x")[0].override
        assert not sp.analyze("git push # review-overridexyz")[0].override

    def test_trailing_comment_text_allowed(self):
        assert sp.analyze("git push # review-override: accepted P2s")[0].override
        assert sp.analyze("git push # review-override — notes")[0].override

    def test_independent_sigils_coexist(self):
        # Codex P2: two INDEPENDENT acks must be able to share one trailing comment —
        # at escalation round 2 a genuine-but-unrecognized audit needs BOTH audit-ack
        # (Rule 3) and depth-ack (Rule 2.5). Every whitespace token is scanned, so order
        # is irrelevant and each sigil matches its own token.
        for cmd in (
            "git commit -m x # audit-ack depth-ack",
            "git commit -m x # depth-ack audit-ack",
        ):
            seg = sp.analyze(cmd)[0].raw
            assert sp.has_trailing_override(seg, sigil="audit-ack"), cmd
            assert sp.has_trailing_override(seg, sigil="depth-ack"), cmd
        # Every merge-gate sigil must be in _KNOWN_SIGILS or it ENDS the leading run and
        # silently breaks combined waivers. scheduled-review-override (newest) combined
        # with stale-review-override: BOTH must still resolve regardless of order.
        for cmd in (
            "gh pr merge 1 --squash --admin # stale-review-override scheduled-review-override",
            "gh pr merge 1 --squash --admin # scheduled-review-override stale-review-override",
        ):
            seg = sp.analyze(cmd)[0].raw
            assert sp.has_trailing_override(seg, sigil="stale-review-override"), cmd
            assert sp.has_trailing_override(seg, sigil="scheduled-review-override"), cmd

    def test_multi_token_still_rejects_prefix_of_longer_token(self):
        # The multi-token scan must NOT loosen the exact-token guard: a sigil that is a
        # prefix of a longer token still does not match.
        seg = sp.analyze("git commit -m x # depth-ack-nope")[0].raw
        assert not sp.has_trailing_override(seg, sigil="depth-ack")

    def test_prose_mention_does_not_authorize(self):
        # Regression (self-audit): the multi-token scan must accept a sigil only in the
        # LEADING run of recognized ack/override tokens — a sigil appearing after prose,
        # negated or incidental, must NOT waive the gate.
        for cmd in (
            "git commit -m x # not a review-override, just cleanup",
            "git commit -m x # see review-override docs before merging",
        ):
            assert not sp.analyze(cmd)[0].override, cmd
        # depth-ack buried after prose is likewise not honored
        seg = sp.analyze("git commit -m x # TODO consider depth-ack later")[0].raw
        assert not sp.has_trailing_override(seg, sigil="depth-ack")
        # a non-sigil token AHEAD of a real sigil ends the run (the sigil is unreachable)
        seg2 = sp.analyze("git commit -m x # depth-ack-nope audit-ack")[0].raw
        assert not sp.has_trailing_override(seg2, sigil="audit-ack")


# ── commit hook-skip flag detection ─────────────────────────────────────


class TestCommitFlagParse:
    def test_quoted_flag(self):
        assert _commit_nv(f"git commit '{NV}' -m wip")

    def test_bundled_short(self):
        assert _commit_nv("git commit -nm wip")

    def test_n_glued_to_operator(self):
        assert _commit_nv("git commit -m wip -n&&echo done")

    def test_a_and_n_bundle(self):
        assert _commit_nv("git commit -an -m wip")

    def test_attached_message_not_flag(self):
        assert not _commit_nv("git commit -minitial")

    def test_message_starting_with_dash_n(self):
        assert not _commit_nv("git commit -m '-not ready yet'")

    def test_separate_message_dash_n(self):
        assert not _commit_nv("git commit -m -n")

    def test_pathspec_after_double_dash(self):
        assert not _commit_nv("git commit -- -n")

    def test_amend_alone(self):
        assert not _commit_nv("git commit --amend")


# ── gh pr subcommand ────────────────────────────────────────────────────


class TestGhPr:
    def test_plain(self):
        assert sp.gh_pr_subcommand(["gh", "pr", "create"]) == "create"

    def test_global_flag_before_pr(self):
        assert sp.gh_pr_subcommand(["gh", "--repo", "o/r", "pr", "merge", "5"]) == "merge"

    def test_not_gh(self):
        assert sp.gh_pr_subcommand(["git", "pr", "create"]) is None


# ── shell command-position resolution (reserved words + group openers) ───
# A destructive command wrapped in a control structure or group — `if …; then
# git clean -f; fi`, `(git clean -f)`, `{ git clean -f; }`, `! git clean -f` —
# must still resolve exe=git so the discard/push/rm gates see it. Without this,
# analyze() commits exe to the leading `then`/`(git`/`{` token and every gate
# that keys on `seg.exe == "git"/"gh"/"rm"` silently skips the segment.


class TestCommandPositionStrip:
    def _detects(self, cmd: str, exe: str, sub: str | None = None) -> bool:
        for s in sp.analyze(cmd):
            if s.exe != exe:
                continue
            if sub is None:
                return True
            if exe == "git" and sp.git_subcommand(s.argv) == sub:
                return True
            if exe == "gh" and sp.gh_pr_subcommand(s.argv) == sub:
                return True
        return False

    # reserved words that front a command (same segment as the keyword)
    def test_then_git_clean(self):
        assert self._detects("then git clean -f", "git", "clean")

    def test_do_git_push(self):
        assert self._detects("do git push --force", "git", "push")

    def test_else_git_reset(self):
        assert self._detects("else git reset --hard", "git", "reset")

    def test_elif_git(self):
        assert self._detects("elif git diff --quiet", "git", "diff")

    def test_bang_negation(self):
        assert self._detects("! git clean -fdx", "git", "clean")

    def test_if_condition_command(self):
        # `if git diff --quiet; then …` → the condition IS a git command
        assert self._detects("if git diff --quiet", "git", "diff")

    def test_while_until_condition(self):
        assert self._detects("while git fetch --all", "git", "fetch")
        assert self._detects("until git pull", "git", "pull")

    # group openers — spaced and GLUED
    def test_subshell_spaced(self):
        assert self._detects("( git clean -f )", "git", "clean")

    def test_case_selector_ending_in_paren(self):
        assert self._detects(
            'case "$(printf b)" in b) git push origin main ;; esac',
            "git",
            "push",
        )

    def test_subshell_glued(self):
        assert self._detects("(git clean -f)", "git", "clean")

    def test_brace_group(self):
        assert self._detects("{ git clean -f; }", "git", "clean")

    def test_glued_gh_pr_merge(self):
        assert self._detects("(gh pr merge 5 --admin)", "gh", "merge")

    # realistic compound control structures
    def test_if_then_fi(self):
        assert self._detects("if true; then git clean -f; fi", "git", "clean")

    def test_while_do_done(self):
        assert self._detects("while :; do git push --force origin main; done", "git", "push")

    def test_case_pattern_command(self):
        assert self._detects("case x in y) echo hello ;; esac", "echo")

    def test_case_multiple_pattern_commands(self):
        command = "case x in a) echo first ;; b) echo second ;; esac"
        segments = sp.analyze(command)

        assert sum(s.exe == "echo" for s in segments) == 2

    def test_case_parenthesized_later_pattern_command(self):
        assert self._detects(
            "case b in a) : ;; (b) git push origin main ;; esac",
            "git",
            "push",
        )

    def test_function_body_command(self):
        assert self._detects("f() { echo hello; }", "echo")

    def test_function_body_command_with_spaced_parens(self):
        assert self._detects("f () { echo hello; }", "echo")

    def test_function_body_git_push_with_spaced_parens(self):
        assert self._detects("f () { git push origin main; }", "git", "push")

    def test_function_keyword_body_command(self):
        assert self._detects("function f { echo hello; }", "echo")

    def test_function_keyword_with_optional_parens_body_command(self):
        assert self._detects(
            "function f () { git push origin main; }",
            "git",
            "push",
        )

    def test_function_keyword_if_body_command(self):
        assert self._detects(
            "function f if git push origin main; then :; fi",
            "git",
            "push",
        )

    def test_function_keyword_while_body_command(self):
        assert self._detects(
            "function f while git push origin main; do :; done",
            "git",
            "push",
        )

    def test_function_keyword_case_body_command(self):
        assert self._detects(
           "function f case x in x) git push origin main ;; esac",
           "git",
           "push",
        )

    def test_coproc_command(self):
        assert self._detects("coproc echo hello", "echo")

    def test_named_coproc_compound_body_command(self):
        assert self._detects("coproc worker { rm -rf /tmp/scratch; }", "rm")

    @pytest.mark.parametrize(
       "opener, closer",
       [("(", ")"), ("if true; then", "fi"), ("while false; do", "done")],
    )

    def test_named_coproc_other_compound_body_command(self, opener, closer):
       assert self._detects(
          f"coproc worker {opener} rm -rf /tmp/scratch; {closer}",
          "rm",
       )

    def test_coproc_named_subshell_command(self):
       assert self._detects(
          "coproc worker (git push origin main)",
          "git",
          "push",
       )
    def test_glued_rm(self):
        assert self._detects("(rm -rf ~)", "rm")

    # trailing group-closer must be stripped off the last operand so a path
    # consumer (protected_paths) sees the real path, not `…/x)`
    def test_trailing_closer_stripped_glued(self):
        seg = sp.analyze("(rm -rf /tmp/x)")[0]
        assert seg.exe == "rm"
        assert seg.argv[-1] == "/tmp/x"  # not "/tmp/x)"

    def test_trailing_closer_stripped_spaced(self):
        seg = sp.analyze("( rm -rf /tmp/x )")[0]
        assert seg.exe == "rm"
        assert ")" not in seg.argv

    def test_trailing_closer_with_redirection_after(self):
        # a redirection can follow the subshell close: `(rm -rf /x) 2>/dev/null`
        # → the `)` is glued to `/x`, NOT to the last token → must still be stripped
        seg = sp.analyze("(rm -rf /etc) 2>/dev/null")[0]
        assert seg.exe == "rm"
        assert "/etc" in seg.argv and "/etc)" not in seg.argv

    def test_trailing_closer_redirection_git(self):
        seg = sp.analyze("(git push origin main) >log 2>&1")[0]
        assert seg.exe == "git" and sp.git_subcommand(seg.argv) == "push"
        assert "main" in seg.argv and "main)" not in seg.argv

    # ── SAFETY / MONOTONICITY: normal commands and non-command positions ──
    def test_plain_unchanged(self):
        seg = sp.analyze("git push origin main")[0]
        assert seg.exe == "git" and seg.argv == ["git", "push", "origin", "main"]

    def test_reserved_word_inside_message_not_stripped(self):
        # `then`/`do` inside a commit MESSAGE are operands, never stripped
        seg = sp.analyze('git commit -m "then do the push"')[0]
        assert seg.exe == "git" and sp.git_subcommand(seg.argv) == "commit"
        assert "then do the push" in seg.argv  # message token intact

    def test_for_loop_header_value_is_not_a_command(self):
        # `for x in git` — the `git` is a loop VALUE, must NOT resolve to exe git
        assert not self._detects("for x in git checkout", "git")

    def test_branch_named_like_keyword_still_git(self):
        # a real git command whose ARG looks like a keyword is unaffected
        seg = sp.analyze("git checkout then")[0]
        assert seg.exe == "git" and sp.git_subcommand(seg.argv) == "checkout"


class TestCleanArgvHardening:
    """Round-2/3: the revealed argv is CLEAN — arithmetic `((…))` runs nothing and
    count-matched subshell closers are peeled. Control words strip UNCONDITIONALLY
    (round-1 behavior), so a control wrapper can only ever OVER-gate (safe), never
    hide a real git/gh/rm. (The round-2 `wrapper_consumed` refinement was reverted
    in round-3 — it turned the safe over-gate into a real false-negative on
    `time ! git push`; see test_control_word_after_wrapper_over_gates_safe and the
    F6 regression lock below.) Redirection handling lives in the shared tokenizer
    (split_segments, #1455) and is quote-aware, so a quoted flag value that merely
    resembles a redirect is preserved and the gates are not desynced; see
    TestGateDesyncProtection (and TestRedirects for real redirects)."""

    def _detects(self, cmd: str, exe: str, sub: str | None = None) -> bool:
        for s in sp.analyze(cmd):
            if s.exe != exe:
                continue
            if sub is None:
                return True
            if exe == "git" and sp.git_subcommand(s.argv) == sub:
                return True
            if exe == "gh" and sp.gh_pr_subcommand(s.argv) == sub:
                return True
        return False

    # ── arithmetic `((…))` executes NO external command ──────────────────
    def test_arith_double_paren_runs_nothing(self):
        # `((rm -rf / 1))` is arithmetic evaluation, not a subshell rm
        assert not self._detects("((rm -rf / 1))", "rm")

    def test_arith_increment_no_exe(self):
        segs = sp.analyze("((i++))")
        assert segs and segs[0].exe == ""

    def test_arith_glued_git_not_gated(self):
        # `((git push))` evaluates `git`,`push` as arith variables — no push runs
        assert not self._detects("((git push))", "git", "push")

    def test_arith_with_command_sub_STILL_surfaces_inner(self):
        # MONOTONICITY: a real command hidden in $(…) inside arithmetic must NOT
        # be hidden by the arithmetic early-return — it surfaces via analyze()'s
        # separate substitution path (at depth>0).
        assert self._detects("(( $(git push --force) ))", "git", "push")
        assert self._detects("(( $(rm -rf /) ))", "rm")

    # NOTE: redirection tokenization lives in the shared tokenizer (split_segments,
    # #1455) — quote-aware, so a quoted flag value resembling a redirect is kept (see
    # TestGateDesyncProtection); real redirects + process substitution are covered by
    # TestRedirects below. This class covers only the control-position/arith/closer
    # resolution in _strip_wrappers, which composes on top of that tokenization.

    # ── count-matched trailing closer (nested groups) ────────────────────
    def test_nested_group_closer_fully_stripped(self):
        seg = sp.analyze("( (pytest tests/x.py) )")[0]
        assert seg.exe == "pytest" and seg.argv == ["pytest", "tests/x.py"]

    # ── control words strip UNCONDITIONALLY → safe OVER-gate, never a MISS ──
    def test_control_word_after_wrapper_over_gates_safe(self):
        # `command if git push` runs a program literally named `if` (NOT git push),
        # but we strip `if` and gate the push anyway. That is an OVER-gate on an
        # exotic form — the SAFE direction. Modelling wrapper-vs-keyword precisely
        # (round-2 `wrapper_consumed`) was reverted because it MISSED `time ! git
        # push`; over-gating this near-never-typed form is the acceptable cost.
        assert self._detects("command if git push", "git", "push")

    def test_control_word_at_position_still_strips(self):
        # a bare `if <cmd>` — the condition IS the command — still resolves through
        assert self._detects("if git diff --quiet", "git", "diff")

    def test_var_assignment_then_keyword_over_gates_safe(self):
        # `X=1 then git push` — assignment then a word that MAY be a command name;
        # we strip `then` and gate the push (safe over-gate, same rationale).
        assert self._detects("X=1 then git push", "git", "push")

    # ── F6 REGRESSION LOCK: control wrappers must DETECT the real git push ──
    # These are the exact round-2 false-negatives (`wrapper_consumed` resolved exe
    # to `!`/`if` → push gate MISS). Bash actually runs `git push` in every one
    # (`time`/`command` are wrappers; `!` negates; `if`/`then` front the command),
    # so a MISS here is a real security regression. Pins the revert; forbids any
    # future re-introduction of a wrapper-position flag from regressing it.
    def test_f6_control_wrappers_detect_git_push(self):
        for cmd in (
            "time ! git push",
            "time if git push",
            "command if git push",
            "X=1 then git push",
            "! git push",
            "if git push",
        ):
            assert self._detects(cmd, "git", "push"), f"MISSED git push in: {cmd!r}"

    # ── MONOTONICITY: real danger is never hidden by any strip ───────────
    def test_monotonic_protected_target_survives(self):
        seg = sp.analyze("(rm -rf ~/protected)")[0]
        assert seg.exe == "rm" and "~/protected" in seg.argv

    def test_monotonic_redir_does_not_hide_target(self):
        # `rm -rf / > log` — the shared tokenizer strips `> log` (a real redirect),
        # but the `/` danger token precedes it and stays visible to the guard, so a
        # trailing redirect never hides an rm target.
        seg = sp.analyze("rm -rf / > log")[0]
        assert seg.exe == "rm" and "/" in seg.argv

    def test_monotonic_clean_through_control(self):
        assert self._detects("(git clean -f)", "git", "clean")
        assert self._detects("then git clean -f", "git", "clean")


class TestGateDesyncProtection:
    """A QUOTED flag VALUE that merely resembles a redirect (`git commit -m '<x>'`)
    must NOT be treated as a redirect and must NOT desync the git/gh/commit
    flag/value gates. The shared tokenizer (split_segments, #1455) is quote-aware,
    so the `<`/`>` inside the quotes is preserved as the flag's value; the accessor
    must be IDENTICAL for a quoted `<`-leading value and a plain quoted value.
    (An UNQUOTED `git -C <dir> push` genuinely IS a bash redirect and is correctly
    stripped by the tokenizer — that case is covered by TestRedirects, not here.)"""

    def test_commit_no_verify_gate_not_desynced(self):
        # a quoted message beginning with `<` must not let `-m` swallow --no-verify
        nv = "--no" + "-verify"
        plain = sp.analyze(f"git commit -m 'msg' {nv}")[0].argv
        angle = sp.analyze(f"git commit -m '<x>' {nv}")[0].argv
        assert sp.commit_skips_hooks(plain) is True
        assert sp.commit_skips_hooks(angle) is True  # NOT desynced to False

    def test_git_subcommand_not_desynced(self):
        # `git -C '<dir>' push` — the quoted `-C` value must not swallow `push`
        assert sp.git_subcommand(sp.analyze("git -C 'dir' push")[0].argv) == "push"
        assert sp.git_subcommand(sp.analyze("git -C '<dir>' push")[0].argv) == "push"

    def test_gh_pr_subcommand_not_desynced(self):
        # `gh pr -R '<o/r>' merge 5` — the quoted `-R` value must not swallow `merge`
        assert sp.gh_pr_subcommand(sp.analyze("gh pr -R 'o/r' merge 5")[0].argv) == "merge"
        assert sp.gh_pr_subcommand(sp.analyze("gh pr -R '<o/r>' merge 5")[0].argv) == "merge"


# ── shell redirects: fd leaks + &/| mis-splits ──────────────────────────
#
# split_segments had no redirect grammar, so (a) simple redirects leaked into
# argv as phantom positionals and (b) `2>&1`/`&>`/`>|` mis-split on their
# embedded `&`/`|`. Both are guard bypasses: a LEADING redirect made
# git_subcommand return the redirect token, so `git 2>/dev/null push` slipped the
# push gate. These lock the closed redirect grammar.


class TestRedirects:
    # --- SECURITY: a leading redirect must not hide the git subcommand ---
    def test_leading_stderr_redirect_push_recognized(self):
        argv = sp.analyze("git 2>/dev/null push origin main --force")[0].argv
        assert sp.git_subcommand(argv) == "push"

    def test_leading_redirect_push_blocked(self):
        assert _push_blocked("git 2>/dev/null push origin main")

    def test_leading_dup_redirect_push_blocked(self):
        # `2>&1` splits on '&' under the old tokenizer, hiding push in a bogus seg
        assert _push_blocked("git 2>&1 push origin main")

    def test_leading_redirect_escaped_space_target_push_blocked(self):
        # bash: `error\ log` is ONE target word (escaped space) → the real command
        # is `git push`. The target scan must consume the whole word, not stop at
        # the escaped space (else the subcommand mis-reads as 'log' → fail-open).
        cmd = r"git 2>error\ log push origin main"
        assert sp.git_subcommand(sp.analyze(cmd)[0].argv) == "push"
        assert _push_blocked(cmd)

    def test_leading_redirect_concatenated_quote_target_push_blocked(self):
        # bash: `pre"error log"post` is ONE word (concatenated quoting) → `git push`.
        cmd = 'git 2>pre"error log"post push origin main'
        assert sp.git_subcommand(sp.analyze(cmd)[0].argv) == "push"
        assert _push_blocked(cmd)

    def test_leading_redirect_escaped_space_target_commit_no_verify(self):
        # same fail-open on the commit gate: `err\ log` is one target word.
        assert _commit_nv(rf"git 2>err\ log commit '{NV}' -m x")

    def test_leading_redirect_commit_no_verify(self):
        assert _commit_nv(f"git 2>/dev/null commit '{NV}' -m x")

    def test_leading_redirect_commit_recognized(self):
        segs = sp.analyze("git 1>/dev/null commit -m x")
        assert any(sp.git_subcommand(s.argv) == "commit" for s in segs)

    # --- redirects stripped from argv ---
    def test_glued_stderr_redirect_stripped(self):
        assert sp.analyze("pytest tests/x.py 2>/dev/null")[0].argv == ["pytest", "tests/x.py"]

    def test_separated_stdout_redirect_stripped(self):
        assert sp.analyze("pytest tests/x.py > out.log")[0].argv == ["pytest", "tests/x.py"]

    def test_append_redirect_stripped(self):
        assert sp.analyze("pytest tests/x.py >> out.log")[0].argv == ["pytest", "tests/x.py"]

    def test_combined_out_and_dup_redirect(self):
        segs = sp.analyze("pytest tests/x.py > out.log 2>&1")
        assert segs[0].argv == ["pytest", "tests/x.py"]
        assert len(segs) == 1  # no bogus trailing '1' segment

    def test_ampersand_redirect_stripped(self):
        segs = sp.analyze("pytest tests/x.py &>log")
        assert segs[0].argv == ["pytest", "tests/x.py"]
        assert len(segs) == 1  # not split on the leading '&'

    def test_ampersand_append_redirect_stripped(self):
        assert sp.analyze("pytest tests/x.py &>> log")[0].argv == ["pytest", "tests/x.py"]

    def test_noclobber_redirect_stripped(self):
        segs = sp.analyze("pytest tests/x.py >| out")
        assert segs[0].argv == ["pytest", "tests/x.py"]
        assert len(segs) == 1  # not split on the embedded '|'

    def test_input_redirect_stripped(self):
        assert sp.analyze("pytest tests/x.py < in.txt")[0].argv == ["pytest", "tests/x.py"]

    def test_herestring_redirect_stripped(self):
        # a plain here-string target strips (operator + word). A $-bearing target is
        # deliberately LEFT (see test_dollar_var_redirect_target_left_but_harmless).
        assert sp.analyze("grep foo <<<plainword")[0].argv == ["grep", "foo"]

    def test_fd_dup_target_stripped(self):
        assert sp.analyze("pytest tests/x.py >&2")[0].argv == ["pytest", "tests/x.py"]

    def test_escaped_space_redirect_target_stripped(self):
        # an escaped space keeps the target as ONE word → fully stripped from argv
        assert sp.analyze(r"pytest tests/x.py 2>err\ log")[0].argv == ["pytest", "tests/x.py"]

    def test_concatenated_quote_redirect_target_stripped(self):
        # `pre"a b"post` is one concatenated word target → stripped whole
        assert sp.analyze('pytest tests/x.py 2>pre"a b"post')[0].argv == ["pytest", "tests/x.py"]

    # --- pytest detection survives redirects ---
    def test_pytest_detected_with_redirect(self):
        assert sp.command_runs_pytest("python -m pytest tests/x.py -v 2>&1")
        assert sp.command_runs_pytest("pytest tests/x.py 2>/dev/null")

    # --- REGRESSION: real operators & quotes unaffected ---
    def test_background_ampersand_still_splits(self):
        exes = [s.exe for s in sp.analyze("sleep 1 & echo done")]
        assert "sleep" in exes and "echo" in exes

    def test_pipe_still_splits(self):
        assert [s.exe for s in sp.analyze("cat x | grep y")] == ["cat", "grep"]

    def test_redirect_char_in_quotes_preserved(self):
        seg = sp.analyze('git commit -m "a > b | c"')[0]
        assert sp.git_subcommand(seg.argv) == "commit"
        assert "a > b | c" in seg.argv  # quoted metachars are one token, not stripped

    def test_and_or_operators_unaffected(self):
        assert sp.command_runs_pytest("ruff check . && pytest -q")

    def test_redirect_then_pipe(self):
        segs = sp.analyze("grep x f 2>/dev/null | wc -l")
        assert [s.exe for s in segs] == ["grep", "wc"]
        assert segs[0].argv == ["grep", "x", "f"]

    def test_override_survives_redirect(self):
        seg = sp.analyze("git push origin main > out.log # review-override")[0]
        assert seg.override
        assert sp.git_subcommand(seg.argv) == "push"

    def test_word_ending_in_digit_not_treated_as_fd(self):
        # `push2>x` — the digit is part of the word, not a standalone fd, so the
        # command word 'push2' is preserved (no fail-open onto a bare 'push').
        assert sp.git_subcommand(sp.analyze("git push2>x")[0].argv) == "push2"

    # --- a substitution AS the redirect target still executes → stay visible ---
    def test_cmd_substitution_redirect_target_keeps_rm_visible(self):
        # `2>$(rm -rf x)` runs the rm to produce the filename. The nested rm MUST
        # remain a visible segment for the destructive-command guard (regression:
        # the target-consumer once ate the '$', hiding the substitution).
        segs = sp.analyze("echo hi 2>$(rm -rf /tmp/genesis-x)")
        assert any(s.exe == "rm" and "-rf" in s.argv for s in segs)

    def test_backtick_substitution_redirect_target_keeps_command_visible(self):
        segs = sp.analyze("echo hi 2>`rm -rf /tmp/genesis-x`")
        assert any(s.exe == "rm" for s in segs)

    def test_substitution_target_does_not_break_adjacent_plain_redirect(self):
        # a plain redirect elsewhere still strips normally
        assert sp.analyze("pytest tests/x.py 2>/dev/null")[0].argv == ["pytest", "tests/x.py"]

    def test_double_quoted_substitution_redirect_target_keeps_rm_visible(self):
        # bash expands $(…) inside DOUBLE quotes → the rm runs → must stay visible
        segs = sp.analyze('echo hi 2>"$(rm -rf /tmp/genesis-x)"')
        assert any(s.exe == "rm" for s in segs)

    def test_herestring_double_quoted_substitution_keeps_rm_visible(self):
        segs = sp.analyze('grep foo <<<"$(rm -rf /tmp/genesis-x)"')
        assert any(s.exe == "rm" for s in segs)

    def test_double_quoted_backtick_redirect_target_keeps_rm_visible(self):
        segs = sp.analyze('echo x 2>"`rm -rf /tmp/genesis-x`"')
        assert any(s.exe == "rm" for s in segs)

    def test_single_quoted_substitution_redirect_target_correctly_hidden(self):
        # bash does NOT expand inside SINGLE quotes → nothing runs → correct to hide
        segs = sp.analyze("echo x 2>'$(rm -rf /tmp/genesis-x)'")
        assert not any(s.exe == "rm" for s in segs)

    def test_plain_quoted_filename_redirect_target_stripped(self):
        # a quoted filename target with NO substitution strips normally
        assert sp.analyze('pytest tests/x.py 2>"my log.txt"')[0].argv == ["pytest", "tests/x.py"]

    def test_embedded_substitution_in_redirect_target_keeps_rm_visible(self):
        # a substitution EMBEDDED in an otherwise-plain unquoted target (not at the
        # first char) still expands and runs — must stay visible (round-3 variant).
        segs = sp.analyze("echo hi 2>pre$(rm -rf /tmp/genesis-x)post")
        assert any(s.exe == "rm" for s in segs)

    def test_dollar_var_redirect_target_left_but_harmless(self):
        # a bare $VAR target has no command substitution — left in text (no nested
        # command to hide); documents the accepted argv-residual, not a regression.
        segs = sp.analyze("echo hi 2>$LOGDIR/out")
        assert not any(s.exe in ("rm", "rmdir") for s in segs)


# ── has_top_level_pipe (run_in_background pipe guard, quote/redirect-aware) ──


@pytest.mark.parametrize(
    "cmd",
    [
        "a | b",
        "cat x|grep y",
        "make |& tee log",  # |& = 2>&1 | — a real pipe
        "a || b | c",  # a genuine pipe survives past the ||
        "a > out.log | b",  # pipe after a redirect
        "ls; foo | bar",
    ],
)
def test_has_top_level_pipe_true(cmd):
    assert sp.has_top_level_pipe(cmd) is True


@pytest.mark.parametrize(
    "cmd",
    [
        "a || b",  # control operator, not a pipe
        "a && b",
        "echo hello world",
        "grep -F '|' file",  # | inside single quotes (the classic false-positive)
        "jq '.a | .b' data.json",  # | inside a quoted jq program
        'jq ".x | .y" data.json',  # | inside double quotes
        "git 2>| out push",  # >| is a redirect operator, not a pipe
        "git 2>/dev/null push",
        "printf '%s' payload",
    ],
)
def test_has_top_level_pipe_false(cmd):
    assert sp.has_top_level_pipe(cmd) is False


def test_has_top_level_pipe_case_pattern_is_known_residual():
    # DOCUMENTED residual: a `|` in a case pattern (or heredoc body) is not tracked
    # by the shared scanner, so it reads as a pipe. Accepted for a convenience guard
    # (worst case: an over-block the user reworks, never a security bypass).
    assert sp.has_top_level_pipe("case $x in a|b) echo hi;; esac") is True


@pytest.mark.parametrize(
    "cmd",
    [
        "x=$(cat a | b)",  # $() output is captured, not streamed to bg stdout
        "echo $(seq 1 3 | tail -1)",
        "x=`cat a | b`",  # backtick substitution
        "RESULT=$(gh api foo | jq .name)",  # the common idiom
    ],
)
def test_has_top_level_pipe_substitution_is_not_a_bg_pipe(cmd):
    assert sp.has_top_level_pipe(cmd) is False


@pytest.mark.parametrize(
    "cmd",
    [
        "$(cmd) | filter",  # a REAL pipe after a substitution closes
        "(cat a | b)",  # a bare subshell DOES stream to the bg stdout
    ],
)
def test_has_top_level_pipe_real_pipe_around_substitution(cmd):
    assert sp.has_top_level_pipe(cmd) is True


class TestSigilRunRegression:
    """A sigil passed to has_trailing_override but absent from _KNOWN_SIGILS is
    read as PROSE, so writing it FIRST silently ends the leading run and disables
    every sigil after it. The sigil queried for ITSELF still matches, which is why
    two such tokens shipped undetected."""

    def test_an_unlisted_leading_sigil_no_longer_ends_the_run(self):
        h = sp.has_trailing_override

        assert h("git merge f  # merge-to-main-override audit-ack", "audit-ack")
        assert h("git merge f  # audit-ack merge-to-main-override", "audit-ack")
        assert h("git merge f  # merge-to-main-override", "merge-to-main-override")

    def test_prose_still_ends_the_run(self):
        h = sp.has_trailing_override

        assert not h("git merge f  # see merge-to-main-override docs", "merge-to-main-override")

    def test_every_sigil_any_guard_queries_is_declared(self):
        """``_KNOWN_SIGILS`` claims to be kept in sync with the sigils actually
        passed to has_trailing_override across the guards. Twice it was not, and
        both times the consequence was silent.

        Derived with ``ast`` over every CONSUMER, not a regex over one file. Two
        guards pass their sigil as a module constant (``_OVERRIDE_SIGIL``,
        ``_OVERRIDE``), so a literal-only scan cannot see them — which is how
        ``full-suite-ok`` stayed undeclared while a regex test passed green.

        The population is ``scripts/**.py``, NOT ``scripts/hooks/*.py``: a
        hooks-only glob misses ``review_enforcement_commit.py``, where
        ``audit-ack`` and ``depth-ack`` are queried — 2 of the declared sigils,
        invisible to a scan that claimed to cover "every hook".
        """
        _DEFAULT_SIGIL = inspect.signature(sp._has_trailing_override).parameters["sigil"].default

        queried: dict[str, str] = {}
        for path in sorted(_SCRIPTS.rglob("*.py")):
            tree = ast.parse(path.read_text())
            consts = {}
            for n in ast.walk(tree):
                # Assign (`X = "s"`) AND AnnAssign (`X: str = "s"`) — a scan that
                # handled only the first missed an annotated constant silently.
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant):
                    targets = [t for t in n.targets if isinstance(t, ast.Name)]
                elif (
                    isinstance(n, ast.AnnAssign)
                    and isinstance(n.value, ast.Constant)
                    and isinstance(n.target, ast.Name)
                ):
                    targets = [n.target]
                else:
                    continue
                if isinstance(n.value.value, str):
                    for t in targets:
                        consts[t.id] = n.value.value
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                if name not in ("has_trailing_override", "_has_trailing_override"):
                    continue
                arg = node.args[1] if len(node.args) > 1 else None
                for kw in node.keywords:
                    if kw.arg == "sigil":
                        arg = kw.value
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    queried[arg.value] = path.name
                elif isinstance(arg, ast.Name) and arg.id in consts:
                    queried[consts[arg.id]] = path.name
                elif arg is None:
                    # A call with no sigil relies on the DEFAULT, which the walk
                    # would otherwise never see — so a changed default could point
                    # at an undeclared token from a call site invisible to this
                    # test. Resolved from the live signature rather than restated
                    # here, so the two cannot drift.
                    queried[_DEFAULT_SIGIL] = path.name

        # Guard the guard, against the CONSUMERS not a magic number: a floor of 5
        # was satisfied by three files even when a fourth was silently dropped
        # from the population, so it could not detect a lost consumer.
        files = set(queried.values())
        assert files >= {
            "git_push_guard.py",
            "git_discard_guard.py",
            "full_suite_guard.py",
            "review_enforcement_commit.py",
        }, f"a known consumer produced no sigils — the walk went blind: {sorted(files)}"
        undeclared = {s: f for s, f in queried.items() if s not in sp._KNOWN_SIGILS}
        assert not undeclared, f"queried but not declared in _KNOWN_SIGILS: {undeclared}"
        # The REVERSE direction, and it is the same defect class rather than an
        # extra: an over-declared sigil no guard enforces widens acceptance for
        # every OTHER sigil exactly as the two missing ones narrowed it, and a
        # one-directional assertion ships it green. MEASURED: injecting an unqueried
        # token into _KNOWN_SIGILS left all 146 tests in this file passing.
        unqueried = sorted(set(sp._KNOWN_SIGILS) - set(queried))
        assert not unqueried, f"declared in _KNOWN_SIGILS but no guard queries it: {unqueried}"


class TestUvCarrierResolution:
    """The RESOLVER's own contract, pinned independently of any one guard.

    full_suite_guard now has a fail-closed leg for an unresolved carrier, so it
    blocks these shapes whether or not the parse is right — which means a guard
    test cannot distinguish a correct parse from a caught one. Measured: with the
    leg in place, reverting the --isolated and tool-run fixes left the guard tests
    GREEN. analyze() feeds every other consumer too (the destructive and routing
    guards read the resolved exe), so the resolution is asserted here on its own
    terms rather than through a caller that has its own safety net.
    """

    def _exe(self, cmd: str) -> str:
        return [s for s in sp.analyze(cmd) if s.depth == 0][0].exe

    def test_isolated_is_boolean_and_does_not_eat_the_command(self):
        """--isolated is valueless in uv run. Listing it as value-taking made the
        parser consume the command word as its value and resolve the path after."""
        assert self._exe("uv run --isolated pytest tests/") == "pytest"

    def test_uv_tool_run_resolves_like_uvx(self):
        """uv documents them as identical: uvx is an alias for uv tool run."""
        assert self._exe("uv tool run pytest tests/") == "pytest"
        assert self._exe("uvx pytest tests/") == "pytest"

    def test_a_versioned_tool_name_normalises(self):
        """uv permits package@version; unnormalised it matches no gate."""
        assert self._exe("uvx pytest@8.3.5 tests/") == "pytest"
        assert self._exe("uv tool run ruff@0.3.0 check .") == "ruff"

    def test_a_front_end_flag_before_tool_run_still_normalises_the_version(self):
        """The `tool run` fact comes from the RESOLVER, never from a fixed offset.

        The resolver consumes the front-end's own value flags before matching the
        subcommand, so `tool run` does not sit at argv[1:3] once any of them is
        present. Reading it off that offset left `uv --directory v tool run
        pytest@8.3.5` resolving to the exe `pytest@8.3.5` — a name that matches no
        gate, so full_suite_guard ALLOWED it where the unflagged spelling blocks.
        Every flag asserted here is already in _RUN_CARRIER_VALUE_FLAGS, so these
        are reachable spellings, not hypotheticals.
        """
        for flag in ("--directory", "--project", "--python", "-C"):
            cmd = f"uv {flag} /x tool run pytest@8.3.5 tests/"
            assert self._exe(cmd) == "pytest", cmd

    def test_an_at_inside_a_directory_is_not_a_version_suffix(self):
        """Split the NAME, not the whole token.

        `python@3.12` is Homebrew's real keg layout, so a path carrying an `@` is
        an ordinary shape. Splitting the whole token resolved
        `uvx /opt/homebrew/opt/python@3.12/bin/pytest` to `python` and
        `uvx /nix/store/abc@1/bin/rm` to `abc` — the HIDE direction, in the one
        place this resolver exists to reveal.
        """
        assert self._exe("uvx /opt/homebrew/opt/python@3.12/bin/pytest tests/") == "pytest"
        assert self._exe("uv tool run /nix/store/abc@1/bin/rm -rf /x") == "rm"

    def test_a_version_suffix_is_only_stripped_for_uv_tool_runners(self):
        """Scoped on purpose — @ is uv's spelling, not a universal one, and
        stripping it everywhere would invent syntax for tools that lack it."""
        assert self._exe("some-cmd@1.2.3 arg") == "some-cmd@1.2.3"

    def test_a_non_run_uv_subcommand_still_resolves_to_uv(self):
        """The run literal is what keeps uv rm -rf / from hiding rm behind the
        front-end. Adding tool run must not weaken that."""
        assert self._exe("uv rm -rf /") == "uv"
        assert self._exe("uv pip install requests") == "uv"
def _shell_oracle_env() -> dict[str, str]:
    """Environment for a subprocess whose OUTPUT is the ground truth.

    A non-interactive `bash -c` SOURCES $BASH_ENV before the command, and
    SHELLOPTS/BASHOPTS turn options on in the child and change what it DOES, not
    merely what it prints. An operator who exports any of them would have their
    startup file run by this test and could see its output ahead of the marker,
    so the verdict would describe their machine rather than the command shape.
    Same channels, same reasoning, as scripts/replay_guard_corpus.py.
    """
    env = dict(os.environ)
    for startup in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "BASH_XTRACEFD"):
        env.pop(startup, None)
    return env




# ── `-c` operand resolution ─────────────────────────────────────────────────
# `_nested_script` used to return the token immediately after the `-c` bundle.
# That is the script only when nothing sits between, and something frequently
# does: an end-of-options `--`, a further short option, or the VALUE of an
# option elsewhere in the bundle. The parser then handed back `--`/`-e`/
# `pipefail` as the "script", the real command was never parsed, and the guards
# that decide from these segments had nothing to object to.
#
# MEASURED before the fix, end to end through the real hooks: `git_push_guard`
# returned exit 0 (ALLOW) for `bash -c -- 'git push --force origin main'` while
# returning 2 (BLOCK) for the same command without the `--`. A real shell runs
# both.

#: Each entry is a shape a REAL SHELL EXECUTES. That is the part that makes it a
#: bypass rather than a curiosity, and it is asserted below rather than assumed —
#: a shape bash refuses is not a hole, and treating one as such would be a test
#: that fails for the wrong reason the day bash changes.
_EXECUTING_C_SHAPES = [
    "bash -c 'PAYLOAD'",
    "bash -c -- 'PAYLOAD'",
    "bash -c -e 'PAYLOAD'",
    "bash -c -x 'PAYLOAD'",
    "bash -c -e -x 'PAYLOAD'",
    "bash -co pipefail 'PAYLOAD'",
    "bash -cxo pipefail 'PAYLOAD'",
    "bash -oxc pipefail 'PAYLOAD'",
    "bash -c -o pipefail 'PAYLOAD'",
    # Every OCCURRENCE of a value-taking letter consumes a token, not every
    # distinct letter: a set() count read `errexit` as the script here.
    "bash -coo pipefail errexit 'PAYLOAD'",
    # `+` bundles are option-DISABLES the scan must also step past, and `+o`
    # consumes a value exactly like `-o`.
    "bash -c +e 'PAYLOAD'",
    "bash -c +o pipefail 'PAYLOAD'",
    # A lone `-` (and a lone `+`) ends option processing like `--`.
    "bash -c - 'PAYLOAD'",
    "bash -c + 'PAYLOAD'",
    "sh -c -- 'PAYLOAD'",
    "sh -c - 'PAYLOAD'",
    "sh -c +e 'PAYLOAD'",
]


@pytest.mark.parametrize("template", _EXECUTING_C_SHAPES)
def test_a_command_the_shell_really_runs_is_not_invisible(template):
    """The bypass, closed. For every shape a real shell executes, the embedded
    command must be VISIBLE to the parser — otherwise every guard downstream
    decides from a segment list that does not contain it."""
    # Guard-the-guard, and the reason this suite is trustworthy: prove the shell
    # ACTUALLY runs the payload. An output-distinct marker, not an echoed word —
    # bash's `invalid option name` diagnostic quotes the script text back, so a
    # substring check on a word reports a false positive. This exact mistake
    # turned a real finding into a non-finding once already.
    probe = subprocess.run(
        ["bash", "-c", template.replace("PAYLOAD", "echo $((6*7))")],
        capture_output=True,
        text=True,
        timeout=30,
        env=_shell_oracle_env(),
    )
    assert probe.stdout.strip().splitlines()[:1] == ["42"], (
        f"the shell does not execute {template!r}, so this fixture no longer "
        f"describes a bypass: {probe.stdout!r} {probe.stderr[:200]!r}"
    )

    command = template.replace("PAYLOAD", "git push origin main")
    segments, _blind = sp.analyze_checked(command)
    assert "git" in [seg.exe for seg in segments], (
        f"{command!r} hides its command from the parser — a real shell runs it, "
        "so every guard that searches these segments is blind to it"
    )


def test_an_option_shaped_token_is_not_mistaken_for_the_script():
    """The unit beneath the parametrised cases above, so a regression localises.

    `--` ends option processing; the script is the OPERAND after it, never the
    marker itself."""
    assert sp._nested_script(
        ["bash", "-c", "--", "git push origin main"], "bash"
    ) == "git push origin main"
    assert sp._nested_script(
        ["bash", "-c", "-e", "git push origin main"], "bash"
    ) == "git push origin main"
    # An `o`/`O` ANYWHERE in the bundle consumes the next token as its value,
    # not only when adjacent to `c` — which is what let `-cxo pipefail` read
    # `pipefail` as the script.
    assert sp._nested_script(
        ["bash", "-cxo", "pipefail", "git push origin main"], "bash"
    ) == "git push origin main"
    assert sp._nested_script(
        ["bash", "-oxc", "pipefail", "git push origin main"], "bash"
    ) == "git push origin main"


def test_a_command_with_no_script_still_yields_nothing():
    """The control that moves. Without it, a helper that returned the last token
    unconditionally would satisfy every assertion above while inventing scripts
    out of ordinary arguments."""
    assert sp._nested_script(["bash", "-c"], "bash") == ""
    assert sp._nested_script(["bash", "-c", "--"], "bash") == ""
    assert sp._nested_script(["bash", "file.sh"], "bash") == ""
    # No `-c` at all: an interpreter running a FILE takes no inline script.
    assert sp._nested_script(["bash", "-x", "file.sh"], "bash") == ""


# ── shapes the shell REFUSES or neuters, which must not be reported as commands
# Over-parsing is not free. Reporting a nested command for an invocation the
# shell rejects makes a guard block something that was never going to run, and
# each of these was a real false block in the first version of the operand scan
# (Codex P2s, PR #2112).

def test_the_token_after_a_terminator_is_the_command_even_if_it_looks_like_one():
    """`--` ENDS option processing. `bash -c -- '-x' CMD` runs `-x` as the
    command and CMD is merely `$0`, so resuming the option scan past `--`
    reported CMD as the script and blocked a command that never ran."""
    # start=2, which is where `_nested_script` begins for `bash -c …`: the token
    # after the `-c` bundle. That token IS the `--`, and the scan must stop
    # there and take the next one whole.
    found, script, _noexec, _sticky = sp._first_operand(
        ["bash", "-c", "--", "-x", "git push origin main"],
        2,
        sp._C_BUNDLE_OPTIONS["bash"],
        sp._C_VALUE_TAKING["bash"],
    )
    assert (found, script) == (True, "-x"), (found, script)
    segments, _ = sp.analyze_checked("bash -c -- '-x' 'git push origin main'")
    assert "git" not in [seg.exe for seg in segments], (
        "reported a nested git command for an invocation where the git text is "
        "only $0 — a false block"
    )


def test_an_option_the_interpreter_rejects_yields_no_script():
    """`bash -c -z CMD` exits 2 without running CMD. Skipping `-z` as though it
    were valid made the parser report the command anyway."""
    segments, _ = sp.analyze_checked("bash -c -z 'git push origin main'")
    assert "git" not in [seg.exe for seg in segments], "parsed a command bash refuses"
    # Same refusal through a `+` bundle: `bash -c +z CMD` exits 2.
    segments, _ = sp.analyze_checked("bash -c +z 'git push origin main'")
    assert "git" not in [seg.exe for seg in segments], "parsed a command bash refuses"


def test_every_value_taking_letter_consumes_its_own_token():
    """`bash -coo pipefail errexit CMD` runs CMD: EACH `o` eats a value, so a
    set() membership count consumed one token too few and read `errexit` as
    the script (Codex P2, PR #2112)."""
    assert sp._nested_script(
        ["bash", "-coo", "pipefail", "errexit", "git push origin main"], "bash"
    ) == "git push origin main"
    # In the post-`-c` operand scan as well.
    found, script, _noexec, _sticky = sp._first_operand(
        ["bash", "-c", "-oo", "pipefail", "errexit", "git push origin main"],
        2,
        sp._C_BUNDLE_OPTIONS["bash"],
        sp._C_VALUE_TAKING["bash"],
    )
    assert (found, script) == (True, "git push origin main")


def test_plus_bundles_are_options_the_scan_steps_past():
    """`bash -c +e CMD` runs CMD: `+` bundles disable options rather than
    enabling them, and `+o` consumes a value token just like `-o`. Reading
    `+e` as the script hid the real command (Codex P2, PR #2112)."""
    assert sp._nested_script(
        ["bash", "-c", "+e", "git push origin main"], "bash"
    ) == "git push origin main"
    assert sp._nested_script(
        ["bash", "-c", "+o", "pipefail", "git push origin main"], "bash"
    ) == "git push origin main"


def test_a_lone_minus_or_plus_ends_options_for_the_script():
    """`bash -c - CMD` and `bash -c + CMD` both run CMD — a lone sign ends
    option processing like `--`. Falling through to the operand case read
    `-` itself as the script (Codex P2, PR #2112)."""
    assert sp._nested_script(
        ["bash", "-c", "-", "git push origin main"], "bash"
    ) == "git push origin main"
    assert sp._nested_script(
        ["bash", "-c", "+", "git push origin main"], "bash"
    ) == "git push origin main"


def test_value_taking_letters_stay_interpreter_specific():
    """`-O` is Bash-only. dash rejects `dash -cxO extglob CMD` with
    'Illegal option -O' and runs nothing, so treating `O` as value-taking
    everywhere made Bash-only syntax look valid for every nested shell."""
    assert sp._nested_script(
        ["bash", "-cxO", "extglob", "git push origin main"], "bash"
    ) == "git push origin main"
    assert sp._nested_script(
        ["dash", "-cxO", "extglob", "git push origin main"], "dash"
    ) == ""


def test_an_empty_script_is_an_operand_not_a_miss():
    """An empty string is a VALID `-c` command: `bash -c '' -c CMD` runs nothing,
    exits 0, and CMD becomes a positional argument. A truthiness check treated
    that as 'no operand found' and kept scanning, reporting CMD as a script."""
    found, script, _noexec, _sticky = sp._first_operand(
        ["bash", "-c", "", "-c", "git push origin main"],
        2,
        sp._C_BUNDLE_OPTIONS["bash"],
        sp._C_VALUE_TAKING["bash"],
    )
    assert (found, script) == (True, ""), (found, script)
    segments, _ = sp.analyze_checked("bash -c '' -c 'git push origin main'")
    assert "git" not in [seg.exe for seg in segments], (
        "an empty -c command was treated as a miss and the scan ran on"
    )


#: Shapes a real shell REFUSES or PARSES-WITHOUT-EXECUTING. Reporting a command
#: for any of these is a false block on something that provably does nothing.
#: Every entry is proven inert below before it is used, so the corpus cannot
#: quietly stop describing reality when a shell changes.
_INERT_C_SHAPES = [
    "bash -c -n 'PAYLOAD'",          # -n: read but do not execute
    "bash -cn 'PAYLOAD'",            # same, inside the -c bundle
    "bash -c -D 'PAYLOAD'",          # -D: dump strings, does not execute
    "bash -c -o noexec 'PAYLOAD'",   # the -o spelling of -n
    "bash -c -z -c 'PAYLOAD'",       # -z rejected; a later -c is not a retry
    "dash -c -t 'PAYLOAD'",          # dash: Illegal option
    "dash -c -h 'PAYLOAD'",
    "dash -c -r 'PAYLOAD'",
]


@pytest.mark.parametrize("template", _INERT_C_SHAPES)
def test_a_command_the_shell_never_runs_is_not_reported(template):
    """The other direction from the bypass tests, and the one three review
    rounds were spent on.

    Over-parsing is not free: reporting a nested command for an invocation the
    shell refuses or neuters makes every downstream guard block something that
    was never going to run. Each of these was a real false block.
    """
    # The guard-the-guard below reads the INNER shell's stdout, so it needs that
    # shell to exist. When it does not, the outer bash reports the failure on
    # stderr and leaves stdout EMPTY — which satisfies "did not print 42" for
    # the wrong reason, and the test then proves nothing about whether the shape
    # is inert. Keyed on the template's own interpreter rather than on `dash` by
    # name, so an `ash`/`ksh`/`zsh` row added later cannot reintroduce this
    # (CodeRabbit, PR #2112).
    interpreter = template.split(None, 1)[0]
    if not shutil.which(interpreter):
        pytest.skip(
            f"{interpreter} is not installed; the inertness probe needs the real shell"
        )
    probe = subprocess.run(
        ["bash", "-c", template.replace("PAYLOAD", "echo $((6*7))")],
        capture_output=True,
        text=True,
        timeout=30,
        env=_shell_oracle_env(),
    )
    # Guard-the-guard, and the load-bearing half: prove the shell really does
    # NOT run it. A shape that started executing would make the assertion below
    # a demand for a genuine bypass.
    assert probe.stdout.strip().splitlines()[:1] != ["42"], (
        f"{template!r} now EXECUTES its payload, so refusing to report it would "
        f"be a bypass rather than a fix: {probe.stdout!r}"
    )

    command = template.replace("PAYLOAD", "git push origin main")
    segments, _blind = sp.analyze_checked(command)
    assert "git" not in [seg.exe for seg in segments], (
        f"{command!r} reports a nested git command, but the shell never runs it "
        "— every guard downstream blocks a command that does nothing"
    )


def test_the_dash_allowlist_matches_what_dash_accepts():
    """The table was asserting things about dash that dash disagrees with.

    MEASURED against the installed dash: `-h`, `-r` and `-t` are rejected with
    "Illegal option" and nothing runs, yet all three were in the sh/dash
    allowlists, so the operand scan walked past them. Pinned by asking the shell
    rather than by re-typing a list, so the test cannot drift from reality the
    way the table did.
    """
    if not shutil.which("dash"):
        pytest.skip("dash is not installed; this assertion needs the real shell")
    for letter in sorted(sp._C_BUNDLE_OPTIONS["dash"]):
        if letter == "c":
            continue
        probe = subprocess.run(
            ["dash", f"-{letter}", "-c", "echo $((6*7))"],
            capture_output=True, text=True, timeout=30, env=_shell_oracle_env(),
        )
        combined = probe.stdout + probe.stderr
        assert "llegal option" not in combined and "nvalid option" not in combined, (
            f"the dash allowlist contains {letter!r}, which the installed dash "
            f"rejects: {combined.strip()[:120]!r}"
        )


def test_the_first_c_bundle_owns_the_decision():
    """A rejected option after the first `-c` terminates resolution.

    Resuming the outer scan let a LATER `-c` be read as a fresh selector, so an
    invocation the shell rejects outright still produced a command."""
    assert sp._nested_script(
        ["bash", "-c", "-z", "-c", "git push origin main"], "bash"
    ) == ""
    # CONTROL: a single valid -c still resolves, so this is not "always refuse".
    assert sp._nested_script(
        ["bash", "-c", "git push origin main"], "bash"
    ) == "git push origin main"


#: Every no-exec spelling, in every position, with both signs — and the cases
#: where execution is RESTORED. Each row is (argv-after-bash, runs?), and the
#: `runs?` column is re-derived from the real shell below rather than trusted,
#: so the corpus cannot quietly stop describing bash the way the option table
#: did. MEASURED on bash 5.2 (PR #2112).
#:
#: The two letters do NOT behave alike, which is the defect these pin:
#:   `n` is shell STATE      — `+n` clears it and the LAST occurrence wins
#:   `D` is an invocation ACTION — either sign selects it, nothing clears it
#: and the state is ONE scan across the whole option list, so an option BEFORE
#: the `-c` bundle counts exactly as much as one after it.
_NO_EXEC_MATRIX = [
    (["-c", "PAYLOAD"], True),                                   # baseline
    (["-n", "-c", "PAYLOAD"], False),                            # pre-selector -n
    (["-o", "noexec", "-c", "PAYLOAD"], False),                  # the -o spelling
    (["-D", "-c", "PAYLOAD"], False),                            # pre-selector -D
    (["+D", "-c", "PAYLOAD"], False),                            # + sign, still inert
    (["-c", "+D", "PAYLOAD"], False),                            # post-selector +D
    (["-D", "+D", "-c", "PAYLOAD"], False),                      # D is STICKY
    (["-D", "-c", "+D", "PAYLOAD"], False),                      # sticky across -c
    (["+n", "-c", "PAYLOAD"], True),                             # +n enables
    (["-n", "+n", "-c", "PAYLOAD"], True),                       # last wins -> runs
    (["+n", "-n", "-c", "PAYLOAD"], False),                      # last wins -> inert
    (["-n", "-c", "+n", "PAYLOAD"], True),                       # cleared ACROSS -c
    (["-n", "-c", "-n", "PAYLOAD"], False),
    (["-o", "noexec", "+o", "noexec", "-c", "PAYLOAD"], True),   # -o form, last wins
    (["+o", "noexec", "-o", "noexec", "-c", "PAYLOAD"], False),
    (["-o", "noexec", "-c", "+o", "noexec", "PAYLOAD"], True),   # cleared across -c
    (["-o", "errexit", "-c", "PAYLOAD"], True),                  # unrelated -o runs
    (["-en", "-c", "PAYLOAD"], False),                           # n inside a bundle
    (["-ec", "PAYLOAD"], True),                                  # bundle without n
    (["-e", "-z", "-c", "PAYLOAD"], False),                      # invalid opt: refused
    (["-c", "PAYLOAD", "-z"], True),                             # invalid AFTER operand
    # THE THREE TERMINATOR SPELLINGS, which do not behave alike. `-` and `--`
    # really do end option processing, so the token after them is the command
    # and no `-c` script exists to resolve. A lone `+` does NOT: the shell
    # reads an empty option bundle, skips it, and keeps parsing to `-c`.
    # Treating `+` like the other two made the pre-selector scan abandon an
    # argv the shell runs, and every segment-based guard went blind to the
    # payload — a BYPASS, and the reason these rows exist.
    (["+", "-c", "PAYLOAD"], True),                              # + is NOT a terminator
    (["+", "+n", "-c", "PAYLOAD"], True),                        # + skipped, +n enables
    (["-", "-c", "PAYLOAD"], False),                             # - DOES terminate
    (["--", "-c", "PAYLOAD"], False),                            # -- DOES terminate
]


@pytest.mark.parametrize("argv_tail,should_run", _NO_EXEC_MATRIX)
def test_the_parser_reports_a_nested_command_exactly_when_bash_runs_it(
    argv_tail, should_run
):
    """Both error directions at once, against the shell itself.

    Reporting a command bash never runs is a FALSE BLOCK; failing to report one
    it does run is a BYPASS. A single `-n`-anywhere rule would have produced the
    second: `bash -n +n -c CMD` really does execute CMD.
    """
    # ORACLE: ask the real shell, so the expectation cannot drift from reality.
    probe = subprocess.run(
        ["bash", *[t.replace("PAYLOAD", "echo $((6*7))") for t in argv_tail]],
        capture_output=True, text=True, timeout=30, env=_shell_oracle_env(),
    )
    really_runs = probe.stdout.strip().splitlines()[:1] == ["42"]
    assert really_runs is should_run, (
        f"the corpus says runs={should_run} for {argv_tail!r}, but the installed "
        f"bash disagrees ({probe.stdout!r} / {probe.stderr.strip()[:80]!r}) — fix "
        "the row, never the assertion below"
    )

    command = "bash " + " ".join(
        f"'{t}'" if " " in t else t
        for t in [x.replace("PAYLOAD", "git push origin main") for x in argv_tail]
    )
    reported = "git" in [seg.exe for seg in sp.analyze(command)]
    assert reported is should_run, (
        f"{command!r}: bash runs={should_run} but the parser reports={reported} — "
        + ("a BYPASS" if should_run else "a FALSE BLOCK")
    )


def test_sh_carries_the_union_because_the_allowlist_is_keyed_on_a_basename():
    """`sh` is not a shell, it is a NAME, and it does not name the same binary
    everywhere.

    The table is selected by the executable's basename. Here /bin/sh is dash, so
    dash's strict set looked correct — but on a host where /bin/sh is Bash,
    `sh -ch CMD` RUNS while the strict set rejected the bundle, `_nested_script`
    returned "", and the nested command was invisible to every segment-based
    guard. The two directions are not symmetric: too strict HIDES a live command,
    too permissive costs a false block on an invocation that fails anyway.
    """
    sh_set = sp._C_BUNDLE_OPTIONS["sh"]
    assert sp._C_BUNDLE_OPTIONS["bash"] <= sh_set, (
        "sh must accept every bash bundle letter: where /bin/sh IS bash, a "
        "letter missing here hides a command the shell really runs"
    )
    assert sp._C_BUNDLE_OPTIONS["dash"] <= sh_set, (
        "sh must also accept every dash letter — the first attempt at this fix "
        "used bash's set alone and dropped 'I'/'V', hiding `sh -cI CMD` on a "
        "dash host: the same bypass in the opposite direction"
    )
    # Behaviour, not just the table: a bash-only bundle under the `sh` name.
    assert "git" in [seg.exe for seg in sp.analyze("sh -ch 'git push origin main'")]
    # CONTROL: `dash` under its OWN name keeps the strict set, where the
    # basename really does identify the binary.
    assert "h" not in sp._C_BUNDLE_OPTIONS["dash"]
    assert "git" not in [seg.exe for seg in sp.analyze("dash -ch 'git push origin main'")]


_UNPROVEN_DATA_HEREDOC_CASES = [
    ("parameter-expansion", "cat ${x:-<<'EOF'}", "EOF", (), (), True, True),
    ("file-descriptor", "cat <<'EOF' >&file", "EOF", (), (), True, True),
    ("fd-number", "cat <<'EOF' 01>file", "EOF", (), (), True, True),
    ("writer-substitution", "x=$(cat <<'EOF' > f", "EOF", (), (")",), True, True),
    ("path-cat", "./cat <<'EOF'", "EOF", (), (), True, True),
    ("process-substitution", "cat <<'EOF' > >(bash)", "EOF", (), (), True, True),
    ("function-definition", "cat() { bash; }; cat <<'EOF'", "EOF", (), (), True, True),
    ("path-assignment", "PATH=/tmp cat <<'EOF'", "EOF", (), (), True, True),
    ("sudo", "sudo cat <<'EOF'", "EOF", (), (), True, True),
    ("env", "env cat <<'EOF'", "EOF", (), (), True, True),
    ("second-line-open-quote", "echo \"x\ncat <<'EOF'", "EOF", (), (), False, False),
    ("body-delimiter-prefix", "git commit -m \"$(cat <<'EOF'", "EOF", ("EOF)",), (')"',), False, True),
    ("body-delimiter-space", "cat <<'EOF'", "EOF", ("EOF ",), (), True, True),
    ("body-delimiter-cr", "cat <<'EOF'", "EOF", ("EOF\r",), (), True, True),
    ("body-delimiter-tab", "cat <<'EOF'", "EOF", ("\tEOF",), (), True, True),
    ("strip-tabs", "cat <<-'EOF'", "EOF", (), (), True, True),
    ("unquoted", "cat <<EOF", "EOF", (), (), True, True),
    ("double-quoted", 'cat <<"EOF"', "EOF", (), (), True, True),
    ("two-openers", "cat <<'EOF' <<'BAR'", "EOF", (), (), True, True),
    ("pipe", "cat <<'EOF' | bash", "EOF", (), (), True, True),
    ("writer", "cat > f <<'EOF'", "EOF", (), (), True, True),
    ("trailing-backslash", "cat <<'EOF'\\", "EOF", (), (), False, True),
    ("rebase-exec", "git rebase --exec \"$(cat <<'EOF'", "EOF", (), (')"',), True, True),
    ("git-global-option", "git -c x=y commit -F - <<'EOF'", "EOF", (), (), True, True),
    ("python", "python3 - <<'PY'", "PY", (), (), True, True),
    ("tail-glued-digit-2", "cat <<'EOF' >/dev/null2>/dev/null", "EOF", (), (), True, True),
    ("tail-glued-digit-1", "cat <<'EOF' >/dev/null1>/dev/null", "EOF", (), (), True, True),
    ("tail-glued-chain", "cat <<'EOF' >/dev/null1>/dev/null2>/dev/null", "EOF", (), (), True, True),
    (
        "second-data-heredoc",
        "git commit -F - <<'ONE'",
        "ONE",
        (),
        ("git tag -F - t <<'TWO'", "message", "TWO"),
        True,
        True,
    ),
]


@pytest.mark.parametrize(
    ("name", "head", "delimiter", "body_prefix", "after_terminator", "body_visible", "after_visible"),
    _UNPROVEN_DATA_HEREDOC_CASES,
    ids=[case[0] for case in _UNPROVEN_DATA_HEREDOC_CASES],
)
@pytest.mark.parametrize("push_location", ["body", "after"])
def test_unproven_data_heredocs_stay_visible(
    name,
    head,
    delimiter,
    body_prefix,
    after_terminator,
    body_visible,
    after_visible,
    push_location,
):
    body_lines = list(body_prefix)
    body_lines.append(
        "git push --force origin main" if push_location == "body" else "ordinary prose"
    )
    lines = [head, *body_lines, delimiter, *after_terminator]
    if push_location == "after":
        lines.append("git push --force origin main")
    command = "\n".join(lines)

    assert sp.excise_data_heredoc(command) is None, name
    visible = any(sp.git_subcommand(seg.argv) == "push" for seg in sp.analyze(command))
    assert visible is (body_visible if push_location == "body" else after_visible), command


def test_unterminated_data_heredoc_is_not_excised_and_keeps_main_visibility():
    command = "cat <<'EOF'\ngit push --force origin main"
    assert sp.excise_data_heredoc(command) is None
    assert any(sp.git_subcommand(seg.argv) == "push" for seg in sp.analyze(command))


def test_the_exact_review_corpus_keeps_its_post_heredoc_push_visible():
    command = (
        "python3 - <<'PY'\n"
        "# It feeds the reviewer's answer into this gate. `cmd`\n"
        "'''\n"
        "PY\n"
        "git push --force origin main"
    )
    assert sp.excise_data_heredoc(command) is None
    assert any(sp.git_subcommand(seg.argv) == "push" for seg in sp.analyze(command))


_DATA_HEREDOC_RELIEF_CASES = [
    ("cat", "cat <<'EOF'", ""),
    ("cat-dash", "cat - <<'EOF'", ""),
    ("cat-redirects", "cat <<'EOF' >/dev/null 2>/dev/null", ""),
    ("git-commit-short-file", "git commit -F - <<'EOF'", ""),
    ("git-commit-file", "git commit --file - <<'EOF'", ""),
    ("git-commit-file-equals", "git commit --file=- <<'EOF'", ""),
    ("git-tag", "git tag -a v1 -F - <<'EOF'", ""),
    ("git-notes", "git notes add -F - <<'EOF'", ""),
    ("git-notes-append", "git notes append -F - <<'EOF'", ""),
    ("gh-pr-create", 'gh pr create --title "x y" --body-file - <<\'EOF\'', ""),
    ("gh-issue-comment", "gh issue comment 5 -F - <<'EOF'", ""),
    ("gh-issue-create", "gh issue create --title x --body-file - <<'EOF'", ""),
    ("gh-issue-edit", "gh issue edit 5 --body-file=- <<'EOF'", ""),
    ("gh-pr-edit", "gh pr edit 5 --body-file=- <<'EOF'", ""),
    ("gh-pr-comment", "gh pr comment 5 --body-file - <<'EOF'", ""),
    ("git-commit-message", 'git commit -m "$(cat <<\'EOF\'', ')"'),
    ("git-commit-message-equals", 'git commit --message="$(cat <<\'EOF\'', ')"'),
    ("gh-pr-body", 'gh pr create --title t -b "$(cat <<\'EOF\'', ')"'),
    ("gh-pr-comment-body", 'gh pr comment 5 --body "$(cat <<\'EOF\'', ')"'),
]

_DATA_HEREDOC_BODY = (
    "it's prose with an apostrophe\n"
    "`a backtick`\n"
    "$(git push --force origin main)\n"
    "# don't treat this as a command\n"
    "line ending in \\\n"
    "git push --force origin main"
)


@pytest.mark.parametrize(
    ("name", "head", "closing"),
    _DATA_HEREDOC_RELIEF_CASES,
    ids=[case[0] for case in _DATA_HEREDOC_RELIEF_CASES],
)
def test_proven_data_heredocs_are_excised_and_idempotent(name, head, closing):
    command = f"{head}\n{_DATA_HEREDOC_BODY}\nEOF\n{closing}"
    excised = sp.excise_data_heredoc(command)
    assert excised is not None, name
    excised_text, body = excised
    assert "git push --force origin main" in body
    assert sp._excised_text(command) == excised_text
    assert sp._excised_text(excised_text) == excised_text
    assert not any(
        sp.git_subcommand(seg.argv) == "push" for seg in sp.analyze(command)
    )
    assert not any(
        "git push --force origin main" in segment
        for segment in sp.split_segments(command)
    )
    assert not sp.untokenizable(command)
    assert sp.analyze_checked(command)[1] is None


def test_plain_data_heredoc_preserves_a_later_push():
    command = "cat <<'EOF'\nprose\nEOF\ngit push --force origin main"
    excised = sp.excise_data_heredoc(command)
    assert excised is not None
    assert excised[0].endswith("git push --force origin main")
    assert any(sp.git_subcommand(seg.argv) == "push" for seg in sp.analyze(command))


def test_data_heredoc_excision_rejects_unbounded_or_single_line_commands():
    assert sp.excise_data_heredoc("cat <<'EOF'") is None
    assert (
        sp.excise_data_heredoc(
            "cat <<'EOF'\n" + "x" * (sp.MAX_COMMAND_CHARS + 1) + "\nEOF"
        )
        is None
    )


def test_data_heredoc_excision_is_linear_on_long_first_lines():
    # Guards against super-linear backtracking, which at these lengths costs
    # seconds; the bound is loose so shared CI runners don't flake on it.
    commands = [
        "cat " + "\\" * 16_000 + " <<'EOF'\nbody\nEOF",
        "cat " + "x" * 48_000 + " <<'EOF'\nbody\nEOF",
    ]
    for command in commands:
        best = float("inf")
        for _ in range(5):
            started = time.perf_counter()
            assert sp.excise_data_heredoc(command) is None
            best = min(best, time.perf_counter() - started)
        assert best < 0.5, (len(command), best)


@pytest.mark.parametrize(
    ("argv", "method", "endpoint"),
    [
        (["gh", "api", "repos/o/r/pulls"], "GET", "repos/o/r/pulls"),
        (["gh", "api", "repos/o/r/issues", "-f", "title=x"], "POST", "repos/o/r/issues"),
        (["gh", "api", "repos/o/r/issues", "--input", "b.json"], "POST", "repos/o/r/issues"),
        (["gh", "api", "-X", "GET", "search/issues", "-f", "q=x"], "GET", "search/issues"),
        (["gh", "api", "-XPATCH", "repos/o/r/pulls/5"], "PATCH", "repos/o/r/pulls/5"),
        (["gh", "api", "-X=delete", "repos/o/r/git/refs/x"], "DELETE", "repos/o/r/git/refs/x"),
        (["gh", "-X", "PUT", "api", "repos/o/r/pulls/5/merge"], "PUT", "repos/o/r/pulls/5/merge"),
        (["gh", "api", "-i", "-X", "PATCH", "repos/o/r/pulls/5"], "PATCH", "repos/o/r/pulls/5"),
        (["gh", "api", "-X", "GET", "-X", "POST", "x"], "POST", "x"),
        (["gh", "api", "--jq", "-X", "repos/o/r"], "GET", "repos/o/r"),
    ],
)
def test_gh_api_request_reads_the_effective_method(argv, method, endpoint) -> None:
    """gh's own defaulting (gh api --help, gh 2.101.0): GET, or POST once a
    parameter or --input is given; an explicit -X/--method wins, the last one
    kept; a flag's VALUE (`--jq -X`) is never read as the flag."""
    req = sp.gh_api_request(argv)
    assert req is not None
    assert (req.method, req.endpoint) == (method, endpoint)


def test_gh_api_request_is_none_off_the_api_group() -> None:
    assert sp.gh_api_request(["gh", "workflow", "run", "api"]) is None
    assert sp.gh_api_request(["gh", "pr", "view", "5"]) is None
    assert sp.gh_api_request(["git", "push"]) is None


# ── a here-doc body that carries the parse across its terminator (#3433) ──────
#
# parse_segments has no here-doc state, so a body that leaves the segmenter open (an
# unmatched backtick in what reads as a redirect target, an apostrophe) merges the
# terminator and the commands after it into one segment. The fix REPORTS that as a
# bounds-type blind spot; it never changes the segmentation. Literals are assembled
# so this file's own text does not carry a gated command.

_HD_GIT = "g" + "it"
_HD_PUSH = " ".join([_HD_GIT, "pu" + "sh", "--" + "force", "origin", "main"])
_HD_STATUS = _HD_GIT + " st" + "atus"
_HD_COMMIT = " ".join([_HD_GIT, "com" + "mit", "-m", "x"])

# (id, command, must the check fire, the statement bash runs after the here-doc,
#  the text bash is asked about when it differs from the command)
_HEREDOC_MATRIX = [
    ("issue-tee", "tee out.txt <<'EOF'\nx<`\nEOF\n" + _HD_PUSH, True, _HD_PUSH, None),
    ("issue-cat-redirect", "cat > notes.md <<'EOF'\nUse a<`b for markup\nEOF\n" + _HD_PUSH,
     True, _HD_PUSH, None),
    ("issue-python-stdin", "python3 - <<'PY'\nx<`y`z`\nPY\n" + _HD_PUSH, True, _HD_PUSH, None),
    ("issue-tab-strip", "cat <<-'EOF'\nx<`\n\tEOF\n" + _HD_PUSH, True, _HD_PUSH, None),
    ("quote-class", "tee f <<'EOF'\nit's\nEOF\n" + _HD_PUSH + "\necho '", True, _HD_PUSH,
     "tee f <<'EOF'\nit's\nEOF\n" + _HD_PUSH),
    ("two-openers", "cat <<'A' >/dev/null; tee b <<'B'\nB\nA\nx<`\nB\n" + _HD_PUSH,
     True, _HD_PUSH, None),
    ("decoy-copy", "cd ~/proj && tee f <<'EOF'\ncd /x\n" + _HD_PUSH + "\nx<`\nEOF\n" + _HD_PUSH,
     True, _HD_PUSH, None),
    ("crlf", "tee f <<'EOF'\r\nx<`\r\nEOF\r\n" + _HD_PUSH, True, _HD_PUSH, None),
    ("unquoted-delimiter", "tee f <<EOF\nx<`\nEOF\n" + _HD_PUSH, True, _HD_PUSH, None),
    ("terminator-paren", "x=$(tee f <<'EOF'\nx<`\nEOF)\n" + _HD_PUSH, True, _HD_PUSH, None),
    ("excised-then-second", "cat <<'A'\nhi\nA\ntee b <<'B'\nx<`\nB\n" + _HD_PUSH,
     True, _HD_PUSH, None),
    ("depth1-bash-c", "bash -c 'tee f <<EOF\nx<`\nEOF\n" + _HD_PUSH + "'", True, _HD_PUSH,
     "tee f <<EOF\nx<`\nEOF\n" + _HD_PUSH),
    ("comment-opener-hole", "echo hi # see <<Z\ntee f <<'X'\nx<`\nX\n" + _HD_PUSH + "\nZ",
     True, _HD_PUSH, None),
    ("ctl-plain-cat-excised", "cat <<'EOF'\nx<`\nEOF\n" + _HD_PUSH, False, _HD_PUSH, None),
    ("ctl-clean-body", "tee f <<'EOF'\nhello\nEOF\n" + _HD_PUSH, False, _HD_PUSH, None),
    # fail-closed: no terminator line while something follows (bash runs nothing here)
    ("unterminated", "tee f <<'EOF'\nx<`\n" + _HD_PUSH, True, _HD_PUSH, None),
    # fail-closed: spaces are not stripped, so there is no terminator line
    ("tab-strip-spaces-unterminated", "cat <<-'EOF'\nx<`\n  EOF\n" + _HD_PUSH, True,
     _HD_PUSH, None),
    ("ctl-herestring", "cat <<< 'x<`'\n" + _HD_PUSH, False, _HD_PUSH, None),
    ("ctl-commit-idiom", _HD_GIT + " commit -am \"$(cat <<'EOF'\nmsg\nEOF\n)\" && " + _HD_STATUS,
     False, _HD_STATUS, None),
    ("ctl-bash-c-clean", "bash -c 'cat <<EOF\nx\nEOF\necho done'", False, "echo done",
     "cat <<EOF\nx\nEOF\necho done"),
    ("ctl-echo-mention", 'echo "use <<EOF\nEOF\nthen" && ls', False, "ls", None),
    ("ctl-subshell", "(cat > f <<'EOF'\nx\nEOF\ncd sub && " + _HD_STATUS + ")", False,
     _HD_STATUS, None),
    ("ctl-markdown-code", "cat > n.md <<'EOF'\nRun `make` then `x`\nEOF\n" + _HD_GIT + " add n.md",
     False, _HD_GIT + " add n.md", None),
    # The depth-1 shape that MEASURED 93 false fires before the suffix test: the
    # substitution hands the nested parse a script ending in the terminator and `)`.
    ("ctl-substitution-apostrophe",
     "cd sub && " + _HD_GIT + " commit -m \"$(cat <<'EOF'\nit's done\nEOF\n)\"",
     False, "cd sub", None),
]


def _hd_argv_present(command: str, statement: str) -> bool:
    """Whether some parsed segment, at any depth, IS ``statement``."""
    return any(seg.argv == statement.split() for seg in sp.analyze(command))


@pytest.mark.parametrize(
    ("command", "fires", "statement", "oracle_text"),
    [pytest.param(*row[1:], id=row[0]) for row in _HEREDOC_MATRIX],
)
def test_heredoc_hidden_suffix_matrix(command, fires, statement, oracle_text):
    """The check fires exactly on the rows whose body carries the parse across the
    terminator, and a fire withholds every segment (bounds-type, like a bound)."""
    segments, blind = sp.analyze_checked(command)
    if fires:
        assert blind is sp._BLIND_HEREDOC_BODY
        assert segments == []
    else:
        assert blind is not sp._BLIND_HEREDOC_BODY
        assert sp._analyze_bounded(command)[1] != "heredoc"


_HD_MARK = "__SUFFIX_MARK__"


def _bash_runs_statement_at(text: str, statement: str, sandbox: Path) -> bool:
    """Whether bash parses the LAST ``statement`` in ``text`` as a statement.

    Never executes anything: bash only parses a function definition and prints it
    (``declare -f``), in a restricted shell, with a fake ``git`` first on PATH, in an
    empty directory that is no repository. The statement is replaced by a marker, and
    function-body statements are printed indented while here-doc body text is not.
    """
    cut = text.rindex(statement)
    probe = text[:cut] + _HD_MARK + text[cut + len(statement):]
    result = subprocess.run(
        [shutil.which("bash"), "-r", "-c", "f() {\n" + probe + "\n}\ndeclare -f f"],
        env={
            "PATH": str(sandbox / "bin"),
            "HOME": str(sandbox / "work"),
            "GIT_CEILING_DIRECTORIES": str(sandbox),
        },
        cwd=str(sandbox / "work"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0 or "f ()" not in result.stdout:
        return False  # bash could not end the body there: no statement after it
    return re.search(r"^ {4,}.*\b" + _HD_MARK + r"\b", result.stdout, re.M) is not None


@pytest.fixture
def bash_oracle_sandbox(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("the oracle arm needs bash")
    (tmp_path / "bin").mkdir()
    (tmp_path / "work").mkdir()
    fake = tmp_path / "bin" / "git"
    fake.write_text("#!/bin/sh\necho FAKE-git >&2\nexit 97\n")
    fake.chmod(0o755)
    return tmp_path


@pytest.mark.parametrize(
    ("command", "fires", "statement", "oracle_text"),
    [pytest.param(*row[1:], id=row[0]) for row in _HEREDOC_MATRIX],
)
def test_heredoc_hidden_suffix_matrix_against_bash(
    command, fires, statement, oracle_text, bash_oracle_sandbox
):
    """The oracle arm: every fire is a place bash really runs the statement after the
    here-doc, and every non-fire where bash runs it leaves it visible in the parse."""
    runs = _bash_runs_statement_at(oracle_text or command, statement, bash_oracle_sandbox)
    if fires:
        if _hd_carried_only(oracle_text or command):
            assert runs, "a carried-state fire must be a body bash ends before the statement"
    elif runs:
        assert _hd_argv_present(command, statement), (
            "bash runs this statement, the check is silent, so the parse must show it"
        )


def _hd_carried_only(text: str) -> bool:
    """Whether every report on ``text`` is the carried-state test, the one that claims
    bash ended the body. The fail-closed reports claim only that it is unproven."""
    splits: list[int] = []
    openers: list[int] = []
    sp.parse_segments(text, _splits=splits, _openers=openers)
    found = sp._heredoc_hidden_suffixes(text, splits, openers)
    return bool(found) and all(why == "carried" for _, why, _ in found)


def test_heredoc_check_never_changes_the_segmentation():
    """Public parse output is byte-identical with and without the split recording,
    and every recorded split is a newline the parse really split on."""
    corpus = [row[1] for row in _HEREDOC_MATRIX] + [
        "a; b && c || d | e & f\ng",
        "echo 'x\ny' > \"$(z\n)\" ; w",
        "echo $((1<<2)) # c <<A\n(( x<<=1 )); cat <<B\nB",
    ]
    for command in corpus:
        splits: list[int] = []
        openers: list[int] = []
        starts: list[int] = []
        recorded = sp.parse_segments(command, _splits=splits, _openers=openers, _starts=starts)
        assert recorded == sp.parse_segments(command)
        assert len(starts) == len(recorded)
        assert all(command[i] == "\n" for i in splits)
        assert all(command[i : i + 2] == "<<" and command[i + 2 : i + 3] != "<" for i in openers)
        assert sp.split_segments(command) == [
            p.raw for p in sp.parse_segments(sp._excised_text(command))
        ]


def test_heredoc_check_keeps_a_plain_cat_body_excised():
    command = "cat <<'EOF'\nx<`\nEOF\n" + _HD_PUSH
    assert sp.excise_data_heredoc(command) is not None
    segments, blind = sp.analyze_checked(command)
    assert blind is None
    assert _hd_argv_present(command, _HD_PUSH)


def test_heredoc_blind_spot_is_registered_and_bounds_type():
    assert sp._BLIND_HEREDOC_BODY in sp._ALL_BLIND_SPOTS
    assert sp._BLIND_HEREDOC_BODY.bounds_induced is True
    assert sp.rewrites_before_running("tee out.txt <<'EOF'\nx<`\nEOF\n" + _HD_PUSH)
    assert not sp.rewrites_before_running("tee out.txt <<'EOF'\nhello\nEOF\n" + _HD_PUSH)


_HD_HIDES = "tee out.txt <<'EOF'\nx<`\nEOF\n"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # untokenizable keeps precedence: the discard guard's measured exemption
        pytest.param(_HD_HIDES + _HD_PUSH + " # don't", "_BLIND_UNTOKENIZABLE",
                     id="untokenizable-outranks"),
        pytest.param(_HD_HIDES + _HD_PUSH + " \\\n --tags", "_BLIND_CONTINUATION",
                     id="continuation-outranks"),
        pytest.param(_HD_HIDES + _HD_PUSH + " $'\\x41'", "_BLIND_BUILT_ESCAPE",
                     id="built-escape-outranks"),
        pytest.param("x" * (sp.MAX_COMMAND_CHARS) + "\n" + _HD_HIDES + _HD_PUSH,
                     "_BLIND_OVER_LONG", id="length-outranks"),
        pytest.param(_HD_HIDES + _HD_GIT + " $V origin", "_BLIND_HEREDOC_BODY",
                     id="outranks-unresolved-verb"),
    ],
)
def test_heredoc_blind_spot_precedence(command, expected):
    assert sp.analyze_checked(command)[1] is getattr(sp, expected)


def test_heredoc_blind_spot_under_a_depth_bound_reports_the_bound():
    inner = _HD_HIDES + _HD_PUSH
    for _ in range(sp.MAX_SUBSTITUTION_DEPTH + 2):
        inner = "echo \"$(" + inner + ")\""
    nested = "bash -c 'tee f <<EOF\nx<`\nEOF\nls' ; " + inner
    assert sp.analyze_checked(nested)[1] is sp._BLIND_OVER_NESTED


def test_unresolved_verb_still_recovered_when_a_heredoc_withholds_the_parse():
    """The push guard's one recovered fact must survive the new reason: a here-doc
    parse is complete, so it still contributes (it can only add a refusal)."""
    command = _HD_GIT + " $V origin\n" + _HD_HIDES + "ls"
    assert sp.analyze_checked(command)[1] is sp._BLIND_HEREDOC_BODY
    assert sp.unresolved_verb_programs(command) == frozenset({"git"})


def _hd_line_events(text: str) -> int:
    """Python line events executed by the recording parse and the check together:
    deterministic, unlike wall-clock."""
    count = 0

    def tracer(frame, event, arg):
        nonlocal count
        if event == "line":
            count += 1
        return tracer

    previous = sys.gettrace()
    sys.settrace(tracer)
    try:
        splits: list[int] = []
        openers: list[int] = []
        sp.parse_segments(text, _splits=splits, _openers=openers)
        sp._heredoc_hidden_suffixes(text, splits, openers)
    finally:
        sys.settrace(previous)
    return count


_HD_ADVERSARIAL = {
    "many-openers-many-matching-lines": lambda n: ("tee f <<'A'\n" + "A\n" * n)[:n],
    "many-openers-on-one-line": lambda n: ("cat " + "<<A " * n)[:n],
    "alternating-opener-and-tab-lines": lambda n: ("x <<-A\n\tA\n" * n)[:n],
    "exact-opener-then-tab-lines": lambda n: ("x <<A\n\tA\n" * n)[:n],
    "opener-per-line-unclosed-quote": lambda n: ("x <<'A\n" * n)[:n],
    "opener-per-line-no-terminator": lambda n: ("x <<A\n" * n)[:n],
    "one-huge-token-then-opener": lambda n: "x" * n + "\ntee <<'E'\nx<`\nE\nls",
    "unquoted-unterminated-openers-and-expansions": lambda n: ("x <<A\n$(\n" * n)[:n],
    "unresolvable-delimiters-on-one-line": lambda n: ("cat " + "<<$A " * n)[:n],
    "quoted-decoys": lambda n: ("echo '<<A' \"<<B\" <<C\n" * n)[:n],
    "arithmetic-shifts": lambda n: ("echo $((1<<2)) <<A\n" * n)[:n],
}


@pytest.mark.parametrize("shape", sorted(_HD_ADVERSARIAL))
def test_heredoc_scan_is_linear_on_adversarial_shapes(shape):
    """Doubling the input at most doubles the scan's work (Python line events, so
    deterministic). A lookup that filters after its bisect (one mixed index for
    ``<<`` and ``<<-``) goes quadratic on the exact-opener-then-tab-lines shape:
    MEASURED, a ratio of 3.97 there against 2.00 for the split indexes."""
    build = _HD_ADVERSARIAL[shape]
    small, large = _hd_line_events(build(6_000)), _hd_line_events(build(12_000))
    assert large <= 2.2 * small + 50, (shape, small, large)


def test_heredoc_scan_does_one_bisect_per_candidate(monkeypatch):
    calls = 0

    def counting(real):
        def bisect(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real(*args, **kwargs)

        return bisect

    monkeypatch.setattr(sp, "_bisect_right", counting(sp._bisect_right))
    monkeypatch.setattr(sp, "_bisect_left", counting(sp._bisect_left))
    text = ("x <<-A\n\tA)\n$(\n" * sp.MAX_COMMAND_CHARS)[: sp.MAX_COMMAND_CHARS]
    splits: list[int] = []
    openers: list[int] = []
    sp.parse_segments(text, _splits=splits, _openers=openers)
    sp._heredoc_hidden_suffixes(text, splits, openers)
    # per opener: the body start, its row, the substitution-ending test (up to four),
    # the terminator and the expansion test
    assert 0 < calls <= 8 * text.count("<<")


# ── #3433 review: where an opener is, what its delimiter is, what bash expands ──
#
# A pre-PR review found three ways the first version missed a command bash runs:
# a quoted `<<` taken for an opener (its delimiter then swallowed the real one), a
# delimiter spelled with quoting bash removes differently, and a body bash expands
# because its delimiter is unquoted. Each row here was confirmed against bash before
# the fix: no blind spot, and no segment for the statement.

_HD_LOCK = "tee o <<"
_HD_X = "x<`\n"

# (id, command, the statement bash runs after the body)
_HD_REVIEW_HIDDEN = [
    ("decoy-single-quoted", "grep '<<' f; " + _HD_LOCK + "'EOF'\n" + _HD_X + "EOF\n" + _HD_PUSH),
    ("decoy-double-quoted", 'echo "<<" <<"EOF"\n' + _HD_X + "EOF\n" + _HD_PUSH),
    ("decoy-glued", "echo '<<'<<'EOF'\n" + _HD_X + "EOF\n" + _HD_PUSH),
    ("delimiter-locale-quoted", _HD_LOCK + '$"EOF"\n' + _HD_X + "EOF\n" + _HD_PUSH),
    ("delimiter-ansi-c", _HD_LOCK + "$'EOF'\n" + _HD_X + "EOF\n" + _HD_PUSH),
    ("delimiter-empty-ansi-c-inside", _HD_LOCK + "E$''OF\n" + _HD_X + "EOF\n" + _HD_PUSH),
    ("delimiter-backslash-in-single-quotes",
     "tee o <<-'E\\nF'\n" + _HD_X + "\tE\\nF\n" + _HD_PUSH),
    ("delimiter-unresolvable-expansion", _HD_LOCK + "$X\n" + _HD_X + "$X\n" + _HD_PUSH),
]


@pytest.mark.parametrize(
    "command", [pytest.param(row[1], id=row[0]) for row in _HD_REVIEW_HIDDEN]
)
def test_heredoc_review_reproducers_are_reported(command):
    segments, blind = sp.analyze_checked(command)
    assert blind is sp._BLIND_HEREDOC_BODY
    assert segments == []


@pytest.mark.parametrize(
    "command",
    # the unresolvable row is reported because the parse cannot know, not because
    # bash ends the body there, so the oracle does not apply to it
    [pytest.param(row[1], id=row[0]) for row in _HD_REVIEW_HIDDEN[:-1]],
)
def test_heredoc_review_reproducers_against_bash(command, bash_oracle_sandbox):
    """The oracle arm: bash really ends the body before the hidden statement, and
    the parse (before the report) does not show it."""
    assert _bash_runs_statement_at(command, _HD_PUSH, bash_oracle_sandbox)
    assert not _hd_argv_present(command, _HD_PUSH)


# An UNQUOTED delimiter: bash expands the body, so a substitution in it runs. Quoted
# in the text the parser reads, it is inert to the parser.
_HD_EXPANDED = [
    ("dollar-paren", "cat <<EOF\n'$(" + _HD_PUSH + ")'\nEOF", "$(" + _HD_PUSH + ")"),
    ("backtick", "cat <<EOF\n'`" + _HD_PUSH + "`'\nEOF", "`" + _HD_PUSH + "`"),
    ("tab-strip", "cat <<-EOF\n\t'$(" + _HD_PUSH + ")'\n\tEOF\nls", "$(" + _HD_PUSH + ")"),
    ("unterminated", "cat <<EOF\n'$(" + _HD_PUSH + ")'", "$(" + _HD_PUSH + ")"),
]


@pytest.mark.parametrize(
    "command", [pytest.param(row[1], id=row[0]) for row in _HD_EXPANDED]
)
def test_unquoted_heredoc_body_with_a_substitution_is_reported(command):
    segments, blind = sp.analyze_checked(command)
    assert blind is sp._BLIND_HEREDOC_BODY
    assert segments == []


def _bash_expands_in_body(text: str, substitution: str, sandbox: Path) -> bool:
    """Whether bash keeps ``substitution`` in an UNQUOTED here-doc body, where it
    expands it. Parses and prints only (see :func:`_bash_runs_statement_at`)."""
    result = subprocess.run(
        [shutil.which("bash"), "-r", "-c", "f() {\n" + text + "\n}\ndeclare -f f"],
        env={"PATH": str(sandbox / "bin"), "HOME": str(sandbox / "work"),
             "GIT_CEILING_DIRECTORIES": str(sandbox)},
        cwd=str(sandbox / "work"), capture_output=True, text=True, timeout=30,
    )
    out = result.stdout
    return "f ()" in out and re.search(r"<<-?EOF\n", out) is not None and substitution in out


@pytest.mark.parametrize(
    ("command", "substitution"),
    # The unterminated row runs to the end of the function text, so the wrapper
    # cannot frame it. MEASURED instead (harmless): ``cat <<EOF`` newline
    # ``'$(echo RAN)'`` under ``bash -r -c`` prints 'RAN' after the warning that the
    # here-doc was delimited by end-of-file.
    [pytest.param(row[1], row[2], id=row[0]) for row in _HD_EXPANDED if row[0] != "unterminated"],
)
def test_unquoted_heredoc_body_reproducers_against_bash(command, substitution,
                                                        bash_oracle_sandbox):
    assert _bash_expands_in_body(command, substitution, bash_oracle_sandbox)
    assert not _hd_argv_present(command, _HD_PUSH)


# (id, command): never reported, and the trailing status command stays visible.
_HD_REVIEW_QUIET = [
    ("here-string", "cat <<< \"$(date)\"\necho `date`\n" + _HD_STATUS),
    ("quoted-text", "echo '<<EOF'\necho $(date)\n" + _HD_STATUS),
    ("unquoted-plain-variable", "cat > f <<EOF\nhello $HOME\nEOF\n" + _HD_STATUS),
    ("quoted-delimiter-substitution", "python3 - <<'PY'\nprint('$(x)')\nPY\n" + _HD_STATUS),
    ("crlf", "tee f <<'EOF'\r\nhi\r\nEOF\r\n" + _HD_STATUS),
    ("tab-strip", "cat <<-EOF\n\thi\n\tEOF\n" + _HD_STATUS),
    ("ansi-c-delimiter-clean", "tee f <<$'EOF'\nhi\nEOF\n" + _HD_STATUS),
    # a line starting with the delimiter and holding a `)` counts only AFTER the
    # opener: here it is the opener's own line
    ("substitution-ending-line-before-the-body",
     "PYTHONPATH=$(pwd) python3 - <<'PY'\nprint(1)\nPY\n" + _HD_STATUS),
]

# FAIL-CLOSED by owner ruling: a `<<` bash would not call an opener (a comment,
# arithmetic, text inside another body) is still recorded, because telling them apart
# means emulating bash. These are reported although nothing is hidden. The ruling
# accepts that cost; the corpus measurement beside the blind spot prices it.
_HD_REPORTED_BY_DESIGN = [
    ("arithmetic-shift", "echo $((1<<2))\necho $(date)\n" + _HD_STATUS),
    ("arithmetic-command", "(( x <<= 1 ))\necho ${x}\n" + _HD_STATUS),
    ("comment", "echo hi # see <<EOF\necho $(date)\n" + _HD_STATUS),
    ("opener-written-inside-a-quoted-body",
     "cat > s.sh <<'EOF'\ncat <<X\n$(date)\nX\nEOF\n" + _HD_STATUS),
    ("unresolvable-opener-inside-a-quoted-body",
     "cat > s.md <<'EOF'\nuse <<$X here\nEOF\n" + _HD_STATUS),
    ("unterminated-quoted-body", "tee f <<'EOF'\nhi\n" + _HD_STATUS),
    ("opener-line-quote-spans-lines", "cat <<'EOF' \"a\nb\"\nhi\nEOF\n" + _HD_STATUS),
]


@pytest.mark.parametrize(
    "command", [pytest.param(row[1], id=row[0]) for row in _HD_REPORTED_BY_DESIGN]
)
def test_heredoc_fail_closed_reports_what_it_cannot_prove(command):
    assert sp.analyze_checked(command)[1] is sp._BLIND_HEREDOC_BODY


# The second review's five classes (#3433): bash runs the push, and the first rework
# showed no segment and no bound-type blind spot. (id, command, how bash runs it)
_HD_G = " ".join([_HD_GIT, "pu" + "sh", "origin", "ORACLE"])
_HD_SECOND_REVIEW = [
    ("hash-not-a-comment-escaped", "echo a \\ # ; cat <<'EOF'\na<`b\nEOF\n" + _HD_G, "runs"),
    ("hash-not-a-comment-regex", "[[ a =~ (#) ]] && cat <<'EOF'\na<`b\nEOF\n" + _HD_G, "runs"),
    ("arithmetic-opened-in-a-body",
     "cat <<'EOF'\n((\nEOF\ncat <<'EOF2'\na<`b\nEOF2\n" + _HD_G, "runs"),
    ("arithmetic-opened-in-backticks", "echo `echo ((`; cat <<'EOF'\na<`b\nEOF\n" + _HD_G, "runs"),
    ("decoy-paren-line-between-queued-openers",
     "cat <<'A' <<'B'\nA)\nB\nA\na<`b\nB\n" + _HD_G, "runs"),
    ("unterminated-in-backticks", "x=`cat <<'EOF'\na<$(b\n`\n" + _HD_G, "runs"),
    ("unterminated-in-process-substitution", "<(cat <<''\na<`b\nQ\n); " + _HD_G, "runs"),
    ("expansion-after-a-nested-opener",
     "x=`cat <<'EOF'\nq\n`\ncat <<E2\n'$(" + _HD_G + ")'\nE2\nEOF\n", "expands"),
]


@pytest.mark.parametrize(
    "command", [pytest.param(row[1], id=row[0]) for row in _HD_SECOND_REVIEW]
)
def test_heredoc_second_review_reproducers_are_reported(command):
    segments, blind = sp.analyze_checked(command)
    assert blind is sp._BLIND_HEREDOC_BODY
    assert segments == []


@pytest.mark.parametrize(
    ("command", "how"), [pytest.param(*row[1:], id=row[0]) for row in _HD_SECOND_REVIEW]
)
def test_heredoc_second_review_reproducers_against_bash(command, how, bash_oracle_sandbox):
    """The oracle arm: bash really runs the push (as a statement, or as a substitution
    in a body it expands, printed under the unquoted ``<<E2``), and the parse does not
    show it. Parses and prints only (see :func:`_bash_runs_statement_at`)."""
    if how == "runs":
        assert _bash_runs_statement_at(command, _HD_G, bash_oracle_sandbox)
    else:
        sandbox = bash_oracle_sandbox
        out = subprocess.run(
            [shutil.which("bash"), "-r", "-c", "f() {\n" + command + "\n}\ndeclare -f f"],
            env={"PATH": str(sandbox / "bin"), "HOME": str(sandbox / "work"),
                 "GIT_CEILING_DIRECTORIES": str(sandbox)},
            cwd=str(sandbox / "work"), capture_output=True, text=True, timeout=30,
        ).stdout
        assert "<<E2\n'$(" + _HD_G + ")'\nE2" in out
    assert not _hd_argv_present(command, _HD_G)


# The third review's class (#3433): the delimiter word runs straight into the backtick
# that closes an enclosing backtick substitution. The word scanner reads that backtick
# as a NEW substitution and swallows the rest, so no word is delimited and the
# fail-closed check used to skip the opener silently. bash ends the word at the
# backtick and runs the push.
_HD_BACKTICK_WORD = [
    ("echo-semicolon", "echo `cat <<EOF`; " + _HD_G),
    ("assignment", "x=`cat <<EOF`; " + _HD_G),
    ("newline", "echo `cat <<EOF`\n" + _HD_G),
    ("quoted-word", "echo `cat <<'EOF'`; " + _HD_G),
    ("tab-strip-newline", "echo `cat <<-EOF`\n" + _HD_G),
    ("backslash-word", "echo `cat <<E\\OF`; " + _HD_G),
    ("empty-quotes-in-word", "echo `cat <<EO''F`; " + _HD_G),
    ("fd-prefix-then-assignment", "echo `cat 2<<EOF` y=1; " + _HD_G),
    ("second-backtick-pair", "echo `cat <<EOF`; echo `x`; " + _HD_G),
    ("inside-parameter-default", "echo ${x:-`cat <<EOF`}; " + _HD_G),
    ("inside-command-substitution", "echo $(echo `cat <<EOF`); " + _HD_G),
]


@pytest.mark.parametrize("command", [pytest.param(c, id=i) for i, c in _HD_BACKTICK_WORD])
def test_heredoc_word_closed_by_a_backtick_is_reported(command):
    segments, blind = sp.analyze_checked(command)
    assert blind is not None and blind.bounds_induced, blind
    assert segments == []


@pytest.mark.parametrize("command", [pytest.param(c, id=i) for i, c in _HD_BACKTICK_WORD])
def test_heredoc_word_closed_by_a_backtick_against_bash(command, bash_oracle_sandbox):
    """The oracle arm: bash runs the push as its own statement, and the parse does
    not show it."""
    assert _bash_runs_statement_at(command, _HD_G, bash_oracle_sandbox)
    assert not _hd_argv_present(command, _HD_G)


def test_heredoc_word_closed_by_a_backtick_with_nothing_after_is_quiet():
    assert sp.analyze_checked("echo `cat <<EOF`")[1] is None


# The fourth review's classes (#3433). C1: inside `$(…)` or `<(…)` bash ends a body
# on a line that STARTS with the delimiter and holds a later `)`, and runs what follows
# the delimiter on it. C2: the substitution extractor ends a `$(` at a `)` inside a
# body, so the nested script it hands the parse stops before the terminator and never
# shows the command after it.
_HD_COMMIT = _HD_GIT + " com" + "mit"
_HD_SUBSTITUTION_END = [
    ("c1-space", "echo $(cat <<EOF\nx\nEOF " + _HD_G + ")\nEOF"),
    ("c1-tab", "echo $(cat <<EOF\nx\nEOF\t" + _HD_G + ")\nEOF"),
    ("c1-glued", "echo $(cat <<EOF\nx\nEOF" + _HD_G + ")\nEOF"),
    ("c1-tab-strip", "echo $(cat <<-EOF\nx\n\tEOF " + _HD_G + ")\nEOF"),
    ("c1-quoted", "echo $(cat <<'EOF'\nx\nEOF " + _HD_G + ")\nEOF"),
    ("c1-process-substitution", "cat <(cat <<'EOF'\nx\nEOF" + _HD_G + ")\nEOF"),
    ("c2-unquoted", 'echo "$(cat <<EOF\n)\nEOF\n' + _HD_G + '\n)"'),
    ("c2-commit-message", _HD_COMMIT + " -m \"$(cat <<'EOF'\n)\nEOF\n" + _HD_G + "\n)\""),
    # Found by the round's fuzz: quotes in the body flip the OUTER segmenter, so the
    # segment the substitution is cut from ends inside the body too. Bodies are read
    # in the outermost text, not in that segment.
    ("c2-quotes-in-body", "cd s && " + _HD_COMMIT + " -q -m \"$(cat <<'EOF'\n\"\n\"\nEOF\n"
     + _HD_G + "\n)\""),
    ("c2-quotes-in-body-tab-strip", "gh pr create --body \"$(cat <<-EOF\n\"\n\"\n\tEOF\n"
     + _HD_G + "\n)\""),
]


@pytest.mark.parametrize("command", [pytest.param(c, id=i) for i, c in _HD_SUBSTITUTION_END])
def test_heredoc_ended_inside_a_substitution_is_reported(command):
    segments, blind = sp.analyze_checked(command)
    assert blind is not None and blind.bounds_induced, blind
    assert segments == []


@pytest.mark.parametrize("command", [pytest.param(c, id=i) for i, c in _HD_SUBSTITUTION_END])
def test_heredoc_ended_inside_a_substitution_against_bash(command, bash_oracle_sandbox):
    """The oracle arm: bash prints the body ended by its ``EOF`` line and the push
    after it inside the substitution, and the parse does not show it."""
    sandbox = bash_oracle_sandbox
    out = subprocess.run(
        [shutil.which("bash"), "-r", "-c", "f() {\n" + command + "\n}\ndeclare -f f"],
        env={"PATH": str(sandbox / "bin"), "HOME": str(sandbox / "work"),
             "GIT_CEILING_DIRECTORIES": str(sandbox)},
        cwd=str(sandbox / "work"), capture_output=True, text=True, timeout=30,
    ).stdout
    assert "\nEOF\n\n" + _HD_G in out
    assert not _hd_argv_present(command, _HD_G)


# The message idiom stays smooth where it is provably inert: a quoted delimiter, its
# terminator line exactly, then only blanks and the `)` closing the substitution. The
# `cd … &&` prefix keeps the excision (a narrower, older relief) out of the way, so
# these exercise the exemption itself. A `)` or an apostrophe in the body is what
# made the extractor cut the script short.
_HD_IDIOM_BODIES = ["1) first\n2) second", "it's (done", "plain)", "a)\nb)\nc)"]


@pytest.mark.parametrize("body", _HD_IDIOM_BODIES)
@pytest.mark.parametrize("receiver", [
    "cd sub && " + _HD_COMMIT + " -q -m",
    "cd sub && gh pr create --title t --body",
    _HD_GIT + " -C sub com" + "mit -m",
])
def test_quoted_message_idiom_cut_by_the_extractor_is_exempt(receiver, body):
    command = f"{receiver} \"$(cat <<'EOF'\n{body}\nEOF\n)\""
    assert sp.analyze_checked(command)[1] is None


@pytest.mark.parametrize(
    ("command", "why"),
    [
        pytest.param("cd sub && " + _HD_COMMIT + " -m \"$(cat <<'EOF'\n1) a\nEOF\nls\n)\"",
                     "a command between the terminator and the closing paren", id="c2-shape"),
        pytest.param("cd sub && " + _HD_COMMIT + " -m \"$(cat <<EOF\n1) a\nEOF\n)\"",
                     "an unquoted delimiter: the body expands", id="unquoted"),
        pytest.param("cd sub && " + _HD_COMMIT + " -m \"$(cat <<'EOF'\n1) a\nEOF ok\n)\"",
                     "no exact terminator line", id="no-exact-terminator"),
        pytest.param("cd sub && " + _HD_COMMIT + " -m \"$(cat <<'EOF'\n1) a\nEOF\n; )\"",
                     "something other than blanks before the paren", id="not-blank-before-paren"),
    ],
)
def test_quoted_message_idiom_exemption_is_narrow(command, why):
    assert sp.analyze_checked(command)[1] is sp._BLIND_HEREDOC_BODY, why


def test_quoted_message_idiom_exemption_holds_when_the_extractor_overruns():
    """An apostrophe in the body sends the extractor PAST the closing paren, so the
    script it hands the parse holds the terminator and then outer text. The body's
    real end and the paren after it are read in the outer text all the same."""
    command = ("cd sub && " + _HD_COMMIT
               + " -q -m \"$(cat <<'EOF'\nthe container's key\nEOF\n)\" f.py")
    assert sp.analyze_checked(command)[1] is None


@pytest.mark.parametrize(
    "second",
    ["<<'X'", "3<<'X'", "2>&1 <<'X'", "| cat <<'X'", "<<-'X'"],
)
@pytest.mark.parametrize("tail", ["", " > /dev/null", "; true"])
def test_a_second_opener_in_the_message_idiom_is_never_exempt(second, tail):
    """A second opener takes its body from the lines after the first terminator,
    so the `)` there is data to bash and the substitution keeps running: the
    exemption must not apply (it requires the substitution's only opener)."""
    body = "\ta\n\tEOF" if "-" in second else "a\nEOF"
    command = (
        f"echo \"$(cat <<'EOF' {second}\n{body}\n)\nX\n" + _HD_G + f"\n){tail}\""
    ).replace("cat <<'EOF' <<-'X'", "cat <<-'EOF' <<-'X'")
    segments, blind = sp.analyze_checked(command)
    assert blind is not None and blind.bounds_induced, (command, blind)


def test_a_command_on_the_idiom_opener_line_stays_visible():
    """The exemption only covers the body: anything on the opener line is parsed."""
    command = "x=\"$(cat <<'EOF'; " + _HD_G + "\nmsg\nEOF\n)\""
    segments, blind = sp.analyze_checked(command)
    assert blind is not None or any(s.argv[:2] == _HD_G.split()[:2] for s in segments)


def test_quoted_message_idiom_exemption_needs_the_opener_at_the_top_of_it(
    bash_oracle_sandbox,
):
    """Inside a subshell in the substitution, the paren after the body closes the
    subshell, not the substitution, and what follows it still runs."""
    command = "x=\"$( (cat <<'EOF'\nit's\nEOF\n) ; " + _HD_G + " )\""
    assert sp.analyze_checked(command)[1] is sp._BLIND_HEREDOC_BODY
    sandbox = bash_oracle_sandbox
    out = subprocess.run(
        [shutil.which("bash"), "-r", "-c", "f() {\n" + command + "\n}\ndeclare -f f"],
        env={"PATH": str(sandbox / "bin"), "HOME": str(sandbox / "work"),
             "GIT_CEILING_DIRECTORIES": str(sandbox)},
        cwd=str(sandbox / "work"), capture_output=True, text=True, timeout=30,
    ).stdout
    assert "\nEOF\n ) " + _HD_G in out  # bash runs it after the subshell


@pytest.mark.parametrize(
    ("command", "statement"),
    [pytest.param(row[1], _HD_STATUS, id=row[0]) for row in _HD_REVIEW_QUIET],
)
def test_heredoc_review_controls_stay_quiet(command, statement):
    segments, blind = sp.analyze_checked(command)
    assert blind is None
    assert _hd_argv_present(command, statement)


def test_unquoted_heredoc_body_with_a_parameter_expansion_is_reported():
    """``${`` is reported by owner ruling: bash expands it, and its operators can
    nest a command the parser does not read."""
    command = "cat > f <<EOF\nhome ${HOME}\nEOF\n" + _HD_STATUS
    assert sp.analyze_checked(command)[1] is sp._BLIND_HEREDOC_BODY


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("grep '<<' f; tee o <<'EOF'", ["<<'EOF'"], id="single-quoted-decoy"),
        pytest.param('echo "<<" <<"EOF"', ['<<"EOF"'], id="double-quoted-decoy"),
        pytest.param("echo $'<<' <<E", ["<<E"], id="ansi-c-decoy"),
        # every `<<` outside quotes, by owner ruling: no comment or arithmetic model
        pytest.param("echo hi # <<A\ncat <<B", ["<<A", "<<B"], id="comment"),
        pytest.param("echo $((1<<2)) <<C", ["<<2", "<<C"], id="arithmetic"),
        pytest.param("(( a<<=1 )); cat <<D", ["<<=", "<<D"], id="arithmetic-command"),
        pytest.param("( (cat <<F) )", ["<<F"], id="nested-subshells-are-not-arithmetic"),
        pytest.param("cat <<<x <<-G", ["<<-G"], id="here-string"),
    ],
)
def test_heredoc_openers_are_every_unquoted_operator(command, expected):
    openers: list[int] = []
    assert sp.parse_segments(command, _openers=openers) == sp.parse_segments(command)
    assert len(openers) == len(expected)
    assert [command[i : i + len(e)] for i, e in zip(openers, expected, strict=True)] == expected


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("EOF", ("EOF", False)),
        ("'EOF'", ("EOF", True)),
        ('"EOF"', ("EOF", True)),
        ("\\EOF", ("EOF", True)),
        ("E\\OF", ("EOF", True)),
        ('$"EOF"', ("EOF", True)),
        ("$'EOF'", ("EOF", True)),
        ("E$''OF", ("EOF", True)),
        ("'E\\nF'", ("E\\nF", True)),
        ('"E\\OF"', ("E\\OF", True)),
        ('"E\\$F"', ("E$F", True)),
        ('"E\\"F"', ('E"F', True)),
        ("EOF\r", ("EOF\r", False)),
        ("a{b,c}", ("a{b,c}", False)),
        ("$X", None),
        ("E$", None),
        ("`x`", None),
        ('"E$F"', None),
        ("$'E\\x4fF'", None),
        ("'E\nF'", None),
        ("'EOF", None),
    ],
)
def test_heredoc_delimiter_follows_bash_quote_removal(word, expected):
    """MEASURED against bash 5.2 (``declare -f`` prints the delimiter it chose): the
    spellings the review named, and the ones the implementation refuses to guess."""
    assert sp._heredoc_delimiter(word) == expected


class TestPrCloseReason:
    @pytest.mark.parametrize(
        ("argv", "reason"),
        [
            pytest.param(
                ["gh", "api", "-X", "PATCH", "repos/o/r/pulls/5", "-f", "state=closed"],
                "a REST `state=closed` PATCH to a pull-request endpoint",
                id="rest-separated-field",
            ),
            pytest.param(
                [
                    "gh",
                    "api",
                    "--method",
                    "PATCH",
                    "repos/o/r/pulls/5",
                    "--raw-field",
                    "state=closed",
                ],
                "a REST `state=closed` PATCH to a pull-request endpoint",
                id="rest-long-separated-values",
            ),
            pytest.param(
                ["gh", "api", "-XPATCH", "repos/o/r/pulls/5", "-fstate=closed"],
                "a REST `state=closed` PATCH to a pull-request endpoint",
                id="rest-glued-values",
            ),
            pytest.param(
                ["gh", "api", "--method=PATCH", "repos/o/r/pulls/5", "--field=state=closed"],
                "a REST `state=closed` PATCH to a pull-request endpoint",
                id="rest-long-glued-values",
            ),
            pytest.param(
                ["gh", "api", "-f", "state=closed", "-X", "PATCH", "repos/o/r/pulls/5"],
                "a REST `state=closed` PATCH to a pull-request endpoint",
                id="rest-fields-before-method",
            ),
            pytest.param(
                ["gh", "api", "-X", "patch", "repos/o/r/pulls/5", "-f", "state=closed"],
                "a REST `state=closed` PATCH to a pull-request endpoint",
                id="rest-method-case-insensitive",
            ),
            pytest.param(
                ["/usr/bin/gh", "api", "-X", "PATCH", "repos/o/r/pulls/5", "-f", "state=closed"],
                "a REST `state=closed` PATCH to a pull-request endpoint",
                id="rest-absolute-gh",
            ),
            pytest.param(
                ["gh", "api", "-X", "PATCH", "repos/o/r/pulls/5", "-F", "state=closed"],
                "a REST `state=closed` PATCH to a pull-request endpoint",
                id="rest-typed-field",
            ),
            pytest.param(["gh", "pr", "close", "5"], "`gh pr close`", id="gh-pr-close"),
            pytest.param(
                # Deliberate safe-side over-match; this placement is not relied on.
                ["gh", "-R", "o/r", "pr", "close", "5"],
                "`gh pr close`",
                id="gh-pr-close-separated-repo",
            ),
            pytest.param(
                [
                    "gh",
                    "api",
                    "graphql",
                    "-f",
                    'query=mutation { closePullRequest(input:{pullRequestId:"x"}) { clientMutationId } }',
                ],
                "a `closePullRequest` GraphQL mutation",
                id="graphql-close-mutation",
            ),
            pytest.param(
                ["gh", "api", "-X", "PATCH", "repos/o/r/issues/5", "-f", "state=closed"],
                "a REST `state=closed` PATCH to an issue-or-pull-request endpoint",
                id="rest-issues-endpoint",
            ),
            pytest.param(
                ["gh", "--repo", "api", "pr", "close", "5"],
                "`gh pr close`",
                id="gh-repo-value-before-pr-close",
            ),
        ],
    )
    def test_text_visible_closes_have_reasons(self, argv, reason):
        assert pcp.pr_close_reason(argv) == reason

    @pytest.mark.parametrize(
        "argv",
        [
            pytest.param(
                ["gh", "api", "repos/o/r/pulls/5", "-f", "state=closed"],
                id="rest-default-post",
            ),
            pytest.param(
                ["gh", "api", "-X", "POST", "repos/o/r/pulls/5", "-f", "state=closed"],
                id="rest-post",
            ),
            pytest.param(
                ["gh", "api", "-X", "DELETE", "repos/o/r/pulls/5", "-f", "state=closed"],
                id="rest-delete",
            ),
            pytest.param(
                ["gh", "api", "-X", "PATCH", "repos/o/r/pulls/5", "-f", "state=open"],
                id="rest-open",
            ),
            pytest.param(
                ["gh", "api", "-X", "PATCH", "repos/o/r/pulls/5", "-f", "state=draft"],
                id="rest-draft",
            ),
            pytest.param(
                ["gh", "api", "-X", "PATCH", "pulls", "-f", "state=closed"],
                id="rest-incomplete-path",
            ),
            pytest.param(["gh", "repo", "view", "o/r"], id="not-api"),
            pytest.param(
                ["git", "api", "-X", "PATCH", "repos/o/r/pulls/5", "-f", "state=closed"],
                id="not-gh",
            ),
            pytest.param(["gh", "pr", "close", "5", "--help"], id="terminal-help"),
            pytest.param(
                [
                    "gh",
                    "api",
                    "graphql",
                    "-f",
                    'query=query { closePullRequest(input:{pullRequestId:"x"}) { clientMutationId } }',
                ],
                id="graphql-name-without-mutation",
            ),
        ],
    )
    def test_non_closes_have_no_reason(self, argv):
        assert pcp.pr_close_reason(argv) is None
