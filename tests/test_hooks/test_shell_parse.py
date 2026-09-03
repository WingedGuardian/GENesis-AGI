"""Unit tests for scripts/hooks/shell_parse.py — the shared guard command parser.

The parser decides what a Bash command actually executes (real subcommands,
flags) and where an approval override binds. A miss here is a guard bypass, so
these lock in the wrapper/substitution/nesting cases two review passes found.
"""

from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS / "hooks"))
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


# ── unquoted backslash: the scanner's one remaining blind spot ──────────


class TestUnquotedBackslash:
    """An unquoted backslash escapes the next character; the scanner ignored it.

    Backslash was handled in exactly ONE place — inside a double-quoted string —
    so outside quotes it fell through to the default append and set no state. The
    character after it was then dispatched as if it were bare, which is wrong for
    every escapee and a GUARD BYPASS for the newline: bash REMOVES a
    backslash-newline and joins the two halves into one word, while the scanner
    split there and handed the protected-paths guard half a path.

    These lock the whole class rather than the newline alone, because one missing
    branch generates all of it — fixing only the measured member leaves the rest
    for the next round to find.
    """

    def test_line_continuation_joins_the_word_as_bash_joins_it(self):
        """THE defect: a continuation inside a path must not split the word."""
        assert sp.split_segments("rm -rf /var/da\\\nta") == ["rm -rf /var/data"]

    def test_continuation_at_a_word_boundary_still_joins(self):
        """bash removes the pair unconditionally — it does not insert a space."""
        assert sp.split_segments("echo one\\\ntwo") == ["echo onetwo"]

    @pytest.mark.parametrize(
        ("run_len", "expect_one_segment"),
        [(1, True), (2, False), (3, True), (4, False), (5, True), (6, False)],
    )
    def test_odd_runs_continue_and_even_runs_separate(self, run_len, expect_one_segment):
        """Parity, the lesson #1608 bought in the sibling guard.

        Only an ODD-length backslash run is a continuation: in an EVEN run the
        last backslash is itself escaped, so it is a literal and the newline
        after it is a real command separator. Consuming each escaped PAIR makes
        this fall out with no counting — the pair is eaten, so a newline that
        follows an even run is seen fresh by the separator branch.
        """
        cmd = "echo a" + ("\\" * run_len) + "\necho b"
        segs = sp.split_segments(cmd)
        assert (len(segs) == 1) is expect_one_segment, f"{run_len} backslash(es) -> {segs!r}"

    def test_escaped_separator_is_a_literal_not_a_split(self):
        r"""`\;` is a literal semicolon to bash, so it must not end the segment."""
        assert len(sp.split_segments("rm -rf /var/foo\\;bar")) == 1

    def test_escaped_quote_does_not_open_a_quoted_run(self):
        r"""`\"` is a literal quote; treating it as an opener swallowed the rest.

        Under the old scanner the quote opened and ran to end-of-string, so the
        `|` never split and a second executed command was invisible to every
        consumer. Under-reading in this direction hides a command from a guard.
        """
        assert len(sp.split_segments('printf %s \\"foo | cat')) == 2

    def test_ordinary_commands_are_untouched(self):
        """The equivalence side: no backslash, no behaviour change."""
        assert sp.split_segments("git status && git push") == ["git status", "git push"]
        assert sp.split_segments("echo hi 2>/dev/null") == ["echo hi"]

    def test_trailing_backslash_at_end_of_input_is_not_consumed(self):
        """A dangling backslash has nothing to escape — keep it, do not crash."""
        assert sp.split_segments("echo a\\") == ["echo a\\"]

    def test_a_comment_ending_in_a_backslash_does_not_swallow_the_next_command(self):
        """bash ends a comment at the newline and does NOT continue it.

        Folding there would delete a real command separator, and the following
        command would not merely move — it would VANISH from every consumer's
        segment list. Verified against bash itself: a script whose comment line
        ends in a backslash still executes the next line.
        """
        segs = sp.analyze("echo hi  # note a\\\nrm -rf /var/lib")
        assert [s.exe for s in segs] == ["echo", "rm"]

    # Every context a `#` can follow, and whether bash then treats it as a comment
    # start. DERIVED FROM BASH, not reasoned: each prefix was run as
    # `<prefix>#note \`⏎`touch <marker>` under `bash --norc --noprofile`, and the
    # flag records whether the marker FILE appeared — i.e. whether bash executed
    # line 2 as a command. Asserting on stdout instead would have been wrong: an
    # `echo` payload prints the marker as an argument even when correctly joined,
    # which is exactly how an earlier revision of this table reported two contexts
    # as broken when the parser was right.
    _COMMENT_START_CONTEXTS = [
        ("start", "", True),
        ("space", "echo a ", True),
        ("tab", "echo a\t", True),
        ("semicolon", "echo a;", True),
        ("pipe", "true|", True),
        ("amp", "true&", True),
        ("and_and", "true&&", True),
        ("open_paren", "(true)", True),
        ("close_paren", "(true) ", True),
        ("mid_word", "echo ab", False),
        ("after_quote", "echo 'x'", False),
        ("brace", "{ true;}", False),
    ]

    @pytest.mark.parametrize(
        ("label", "prefix", "bash_runs_next"),
        _COMMENT_START_CONTEXTS,
        ids=[c[0] for c in _COMMENT_START_CONTEXTS],
    )
    def test_comment_start_matches_bash_across_every_context(self, label, prefix, bash_runs_next):
        """Lock the comment-start RULE, not the one context that was found broken.

        Round one scoped the fold to whitespace-preceded comments; a second pass
        then found `)` — a metacharacter bash also opens a comment after, which
        reaches the buffer as itself. Fixing that one context would have left the
        next one for a third round, so this asserts the whole table: where bash
        executes the following line, the parser must still SEE it.
        """
        cmd = f"{prefix}#note \\\nrm -rf /var/lib/xyz"
        sees_second = any(s.exe == "rm" for s in sp.analyze(cmd))
        if bash_runs_next:
            assert sees_second, f"{label}: bash runs the next line but the parser folded it away"

    def test_a_comment_cannot_absorb_an_override_sigil_from_the_next_line(self):
        """The same fold would FORGE the human-in-the-loop acknowledgement.

        Pulling the next line's first word into the comment makes an unrelated
        token read as a deliberate override of the very gate being bypassed.
        """
        raws = sp.split_segments("git push --force #\\\nreview-override")
        assert len(raws) == 2
        assert not any(sp.has_trailing_override(r, "review-override") for r in raws)

    @pytest.mark.parametrize("sigil", sorted(sp._KNOWN_SIGILS))
    def test_no_sigil_can_be_forged_across_a_comment_continuation(self, sigil):
        """Whole-class lock: every declared sigil, not just the one found first.

        `_KNOWN_SIGILS` is the authoritative set and is itself kept exhaustive by
        the guardrail test above, so this cannot silently miss a new sigil.
        """
        raws = sp.split_segments(f"git push --force #\\\n{sigil}")
        assert not any(sp.has_trailing_override(r, sigil) for r in raws)

    # EVERY context a backslash-newline can sit in, and whether bash treats it as a
    # continuation. Same derivation as the comment table: each shape was run under
    # `bash --norc --noprofile` with the second command being a `touch`, and the flag
    # records whether the marker FILE appeared — execution, not output.
    #
    # This exists because three separate review findings turned out to be ONE
    # mechanism: folding is a JOIN, and a join under an incomplete context model makes
    # a real command vanish, where the old append-everything scanner could only ever
    # over-split. Splitting too much costs an over-read; joining too much costs a miss.
    # So the contexts are enumerated here rather than discovered one review round at a
    # time.
    _FOLD_CONTEXTS = [
        ("single_quotes", "echo 'a\\\n{cmd}'", False),
        ("double_quotes", 'echo "a\\\n{cmd}"', False),
        ("comment", "echo a # note \\\n{cmd}", True),
        ("comment_after_paren", "(true)#n \\\n{cmd}", True),
        ("heredoc_unquoted_delim", "cat <<EOF\nline \\\n{cmd}\nEOF", False),
        ("heredoc_quoted_delim", "cat <<'EOF'\nline \\\n{cmd}\nEOF", False),
        ("heredoc_terminator", "cat <<EOF\nbody\nEOF\n{cmd}", True),
        ("after_subshell", "(echo a) \\\n{cmd}", False),
        ("inside_command_sub", "x=$(echo a \\\n)\n{cmd}", True),
        ("backtick", "x=`echo a \\\n`\n{cmd}", True),
    ]

    @pytest.mark.parametrize(
        ("label", "template", "bash_executes"),
        _FOLD_CONTEXTS,
        ids=[c[0] for c in _FOLD_CONTEXTS],
    )
    def test_folding_never_hides_a_command_bash_executes(self, label, template, bash_executes):
        """The invariant that actually matters: a join must not lose a command.

        Asserted one-directionally on purpose. Where bash EXECUTES the second
        command the parser must still see it — that direction is a guard bypass.
        The reverse (the parser reporting a command bash does not run) is an
        over-read, which costs a rewrite and never a miss, so it is deliberately
        not constrained here.
        """
        cmd = template.format(cmd="rm -rf /var/lib/xyz")
        sees = any(s.exe == "rm" for s in sp.analyze(cmd))
        if bash_executes:
            assert sees, f"{label}: bash executes it, the parser folded it away"

    def test_the_override_reader_never_outruns_the_scanner_on_comment_start(self):
        """Lock the coupling that keeps sigil forgery closed.

        Two independent comment-start rules exist: `parse_segments` uses one to
        know where NOT to fold, and `_has_trailing_override` uses another to know
        where a sigil may live. Nothing links them, and the forge needs BOTH to
        line up — the scanner must fold a token in from the next line, and the
        reader must then honour it as an acknowledgement.

        So the invariant is an implication, not an equality: wherever the reader
        would HONOUR a sigil, the scanner must already have declined to fold.
        Scanner-stricter is safe and is the current state (it also recognises a
        comment after `(`/`)`, which the reader does not) — writing this as
        equality failed on exactly that pair while nothing was wrong. The
        dangerous direction is the reverse, and that is what is asserted.
        """
        for prefix in ("", "echo a ", "echo a\t", "echo a;", "true|", "(true)", "echo ab"):
            for sigil in sorted(sp._KNOWN_SIGILS):
                text = f"{prefix}#{sigil}"
                scanner_declines_to_fold = sp.split_segments(f"{text} \\\ntrue") == [
                    f"{text} \\",
                    "true",
                ]
                reader_honours = sp.has_trailing_override(text, sigil)
                assert not (reader_honours and not scanner_declines_to_fold), (
                    f"the override reader honours {sigil!r} after {prefix!r} where the "
                    "scanner would still fold — that pair is a forged acknowledgement"
                )

    def test_continued_git_C_keeps_its_target_in_one_segment(self):
        """The consequential case: a continued ``git -C`` must stay ONE command.

        ``_effective_cwd`` reads ``-C`` off a SEGMENT's argv to decide which
        worktree a git command runs in. Splitting a continued invocation left the
        ``-C`` on the first fragment and the operands on later ones, so the walk
        attributed the operation to the wrong directory — measured on real
        history: ``git -C <worktree> rm \\``⏎``<paths>`` resolved to the main tree.
        """
        cmd = "git -C /wt rm \\\n  src/a.py \\\n  src/b.py"
        segs = sp.analyze(cmd)
        top = [s for s in segs if getattr(s, "depth", 0) == 0]
        assert len(top) == 1, [s.raw for s in top]
        assert top[0].argv[:3] == ["git", "-C", "/wt"]
        assert "src/b.py" in top[0].argv
