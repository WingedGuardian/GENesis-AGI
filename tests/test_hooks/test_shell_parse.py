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
