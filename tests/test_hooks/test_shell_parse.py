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


# ── heredoc bodies are stdin DATA, not executed commands ────────────────────
#
# A QUOTED heredoc (``<<'EOF'``) undergoes NO shell expansion, so its body is
# inert text fed to stdin — it never executes. The parser must NOT split that
# body into command segments; doing so made a commit-message line like "cd into
# the module" look like a real ``cd`` (→ cwd UNKNOWN → false "commit to main"
# block) and made prose mentioning rm/push falsely trip the guards. Skipping a
# QUOTED body is provably safe (it cannot execute anything). UNQUOTED heredocs
# DO expand, so those are left conservatively parsed (fail toward over-matching).


class TestHeredoc:
    RF = "-r" + "f"  # keep the recursive-force flag literal out of this file
    DEEP = "/a/b/c/d"

    def _rm(self) -> str:
        return "rm " + self.RF + " " + self.DEEP

    def test_quoted_heredoc_body_excluded_from_segments(self):
        cmd = (
            "cat > /tmp/m.txt <<'EOF'\n"
            "fix: change how we cd into the module\n"
            "prose mentioning git push and other words\n"
            "EOF\n"
            "git commit -q -F /tmp/m.txt"
        )
        segs = sp.split_segments(cmd)
        assert not any("cd into the module" in s for s in segs), segs
        assert not any("prose mentioning" in s for s in segs), segs
        assert any(s.startswith("cat >") for s in segs), segs
        assert any("git commit" in s for s in segs), segs

    def test_cd_line_in_body_makes_no_cd_segment(self):
        cmd = "git commit -q -F - <<'EOF'\ncd /somewhere and do things\nEOF"
        assert not any(s.startswith("cd ") for s in sp.split_segments(cmd))

    def test_dangerous_command_outside_heredoc_still_seen(self):
        cmd = "cat > /tmp/n.txt <<'EOF'\nhello\nEOF\n" + self._rm()
        assert any(self.DEEP in s for s in sp.split_segments(cmd))

    def test_dangerous_command_inside_quoted_heredoc_is_inert_data(self):
        # SAFETY: a quoted-heredoc body never executes, so an rm/push mentioned in
        # it is data — it must NOT be seen as a real command (removes a false pos).
        cmd = f"cat > /tmp/n.txt <<'EOF'\nnote: {self._rm()}\nEOF\ngit commit -F /tmp/n.txt"
        assert not any(self.DEEP in s for s in sp.split_segments(cmd))

    def test_unterminated_heredoc_fails_open(self):
        # No closing delimiter → do NOT swallow the rest as data (that could hide a
        # real command). Fail open: the body is parsed, so a guard still sees it.
        cmd = f"cat <<'EOF'\nnote\n{self._rm()}"
        assert any(self.DEEP in s for s in sp.split_segments(cmd))

    def test_here_string_is_not_a_heredoc(self):
        cmd = "grep foo <<< 'some string'\ngit commit -q -F /tmp/m.txt"
        assert any("git commit" in s for s in sp.split_segments(cmd))

    def test_dash_heredoc_tab_indented_terminator(self):
        cmd = "\tcat <<-'EOF'\n\tcd into things\n\tEOF\n\tgit commit -q -F /tmp/m.txt"
        segs = sp.split_segments(cmd)
        assert not any("cd into things" in s for s in segs), segs
        assert any("git commit" in s for s in segs), segs

    def test_commented_out_heredoc_does_not_hide_following_commands(self):
        # SECURITY: `# <<'EOF'` is a COMMENT — bash does NOT start a heredoc, so the
        # lines after it are REAL commands. The parser must NOT skip them as a body
        # (that would hide a dangerous command from the guards).
        cmd = f"echo hi # <<'EOF'\n{self._rm()}\nEOF"
        assert any(self.DEEP in s for s in sp.split_segments(cmd))

    def test_heredoc_after_a_comment_line_still_skipped(self):
        # The in-comment suppression must reset at the newline: a REAL quoted heredoc
        # on a later line still has its body skipped.
        cmd = (
            "echo setup # a comment line\n"
            "git commit -q -F - <<'EOF'\n"
            "cd into things in the message\n"
            "EOF"
        )
        assert not any("cd into things" in s for s in sp.split_segments(cmd))

    def test_backtick_scoped_heredoc_does_not_hide_following_commands(self):
        # SECURITY: `<<'EOF'` inside a same-line-closing backtick is scoped by bash
        # to the substitution; bash RUNS the following lines, so a guard must still
        # see them (they must NOT be swallowed as a heredoc body).
        cmd = f"echo `true <<'EOF'`\n{self._rm()}\nEOF"
        assert any(self.DEEP in s for s in sp.split_segments(cmd))

    def test_dollar_paren_scoped_heredoc_stays_conservative(self):
        # `<<'EOF'` inside $(...) is not a top-level heredoc → no body-skip
        # (conservative). The following command stays visible to the guards.
        cmd = f"echo $(true <<'EOF')\n{self._rm()}\nEOF"
        assert any(self.DEEP in s for s in sp.split_segments(cmd))

    def test_unquoted_heredoc_kept_conservative(self):
        # Unquoted <<EOF bodies DO expand ($(...) executes), so they are left
        # parsed (conservative / fail-safe). Documents the intentional boundary.
        cmd = "cat <<EOF\ncd /somewhere\nEOF"
        assert any("cd /somewhere" in s for s in sp.split_segments(cmd))


# ── gh pr subcommand ────────────────────────────────────────────────────


class TestGhPr:
    def test_plain(self):
        assert sp.gh_pr_subcommand(["gh", "pr", "create"]) == "create"

    def test_global_flag_before_pr(self):
        assert sp.gh_pr_subcommand(["gh", "--repo", "o/r", "pr", "merge", "5"]) == "merge"

    def test_not_gh(self):
        assert sp.gh_pr_subcommand(["git", "pr", "create"]) is None
