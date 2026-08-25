"""Unit tests for scripts/hooks/shell_parse.py — the shared guard command parser.

The parser decides what a Bash command actually executes (real subcommands,
flags) and where an approval override binds. A miss here is a guard bypass, so
these lock in the wrapper/substitution/nesting cases two review passes found.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "hooks"))
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
    F6 regression lock below.) Redirection cleanup is NOT done in analyze — it would
    desync the git/gh/commit flag/value gates; see TestGateDesyncProtection."""

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

    # NOTE: analyze() does NOT strip redirections — that would desync the
    # git/gh/commit flag/value gates (see TestGateDesyncProtection). Redirection is
    # left in the argv; a consumer that reads POSITIONALS (protected_paths,
    # destructive) skips it LOCALLY, after its own flag/value handling.

    # ── process substitution is left intact by analyze ───────────────────
    def test_process_substitution_not_stripped(self):
        seg = sp.analyze("diff <(cat a) b")[0]
        assert seg.exe == "diff"
        assert any(t.startswith("<(") for t in seg.argv)  # procsub token preserved

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
        # `rm -rf / > log` — analyze leaves the redirection in place, so the `/`
        # danger token stays visible to the guard (redirs never hide a target)
        seg = sp.analyze("rm -rf / > log")[0]
        assert seg.exe == "rm" and "/" in seg.argv

    def test_monotonic_clean_through_control(self):
        assert self._detects("(git clean -f)", "git", "clean")
        assert self._detects("then git clean -f", "git", "clean")


class TestGateDesyncProtection:
    """The reason redirection stripping is NOT in the shared resolver: shlex drops
    quotes, so a quoted flag VALUE beginning with `<`/`>` must NOT desync the
    git/gh/commit flag/value gates. Each gate accessor must be IDENTICAL for a
    `<`-leading value and a plain value (found + fixed on #1457 round-2)."""

    def test_commit_no_verify_gate_not_desynced(self):
        # a message beginning with `<` must not let `-m` swallow --no-verify
        nv = "--no" + "-verify"
        plain = sp.analyze(f"git commit -m msg {nv}")[0].argv
        angle = sp.analyze(f"git commit -m <x> {nv}")[0].argv
        assert sp.commit_skips_hooks(plain) is True
        assert sp.commit_skips_hooks(angle) is True  # NOT desynced to False

    def test_git_subcommand_not_desynced(self):
        # `git -C <dir> push` — the `-C` value must not swallow `push`
        assert sp.git_subcommand(sp.analyze("git -C dir push")[0].argv) == "push"
        assert sp.git_subcommand(sp.analyze("git -C <dir> push")[0].argv) == "push"

    def test_gh_pr_subcommand_not_desynced(self):
        # `gh pr -R <o/r> merge 5` — the `-R` value must not swallow `merge`
        assert sp.gh_pr_subcommand(sp.analyze("gh pr -R o/r merge 5")[0].argv) == "merge"
        assert sp.gh_pr_subcommand(sp.analyze("gh pr -R <o/r> merge 5")[0].argv) == "merge"
