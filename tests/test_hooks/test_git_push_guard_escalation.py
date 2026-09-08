"""Codex round-escalation gate on `gh pr comment … @codex review`.

Once a PR already carries ESCALATION_ROUND_CAP Codex reviews, requesting
another round must BLOCK with the step-back advisory (triage / mechanism /
state-space) until a trailing '# escalation-ack'. Companion to the commit
gate's Rule 3: that counter tracks LOCAL review rounds and stays asleep when
every local review is clean while the loop churns through CODEX rounds — the
2026-08-12 MW-3 whack-a-mole blind spot this gate closes.

Network-free via the existing _TEST_GH_CODEX_REVIEWS seam (JSONL, one
{login, commit_id} per line — same seam the reviewed-head gate uses).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "hooks"
_spec = importlib.util.spec_from_file_location(
    "git_push_guard_escalation_under_test", _HOOKS_DIR / "git_push_guard.py"
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

CODEX = "chatgpt-codex-connector[bot]"
CAP = _mod.ESCALATION_ROUND_CAP


def _reviews_jsonl(*commit_ids: str, login: str = CODEX) -> str:
    return "\n".join(json.dumps({"login": login, "commit_id": cid}) for cid in commit_ids)


def _reviews_jsonl_stated(*pairs: tuple[str, str], login: str = CODEX) -> str:
    """JSONL with explicit review state, e.g. ("aaa...", "DISMISSED")."""
    return "\n".join(
        json.dumps({"login": login, "commit_id": cid, "state": st}) for cid, st in pairs
    )


def _shas(n: int) -> list[str]:
    return [f"{i:x}" * 40 for i in range(1, n + 1)]


def _segs(cmd: str):
    return _mod.analyze(cmd)


def _check(cmd: str):
    return _mod._check_codex_round_escalation(_segs(cmd))


TRIGGER = 'gh pr comment 1372 --body "@codex review"'


class TestCountHelper:
    def test_counts_only_codex_reviews(self, monkeypatch):
        mixed = _reviews_jsonl(*_shas(2)) + "\n" + _reviews_jsonl("f" * 40, login="some-human")
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", mixed)
        ids = _mod._codex_review_commit_ids("1372")
        assert ids is not None and len(ids) == 2

    def test_empty_seam_is_zero_not_none(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "")
        assert _mod._codex_review_commit_ids("1372") == []

    def test_latest_sha_still_last_entry(self, monkeypatch):
        # Refactor regression: the #1366 reviewed-head gate consumes the SAME
        # fetch — the latest sha must remain the LAST codex entry.
        shas = _shas(3)
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*shas))
        assert _mod._latest_codex_reviewed_sha("1372") == shas[-1]

    def test_api_error_returns_none(self, monkeypatch):
        monkeypatch.delenv("_TEST_GH_CODEX_REVIEWS", raising=False)

        def boom(*a, **k):
            raise OSError("no network in tests")

        monkeypatch.setattr(_mod.subprocess, "run", boom)
        assert _mod._codex_review_commit_ids("1372") is None

    def test_dismissed_reviews_skipped_for_freshness(self, monkeypatch):
        # A dismissed review vouches for no commit — freshness ignores it.
        monkeypatch.setenv(
            "_TEST_GH_CODEX_REVIEWS",
            _reviews_jsonl_stated(("a" * 40, "DISMISSED"), ("b" * 40, "COMMENTED")),
        )
        assert _mod._codex_review_commit_ids("1372") == ["b" * 40]
        assert _mod._latest_codex_reviewed_sha("1372") == "b" * 40

    def test_freshness_falls_to_earlier_active_when_latest_dismissed(self, monkeypatch):
        # Realistic shape: an active review, then a LATER dismissed one (re-review
        # dismissed as stale). Freshness must fall back to the earlier active sha,
        # NOT the dismissed latest — the merge gate then blocks unless HEAD==that.
        monkeypatch.setenv(
            "_TEST_GH_CODEX_REVIEWS",
            _reviews_jsonl_stated(("a" * 40, "COMMENTED"), ("b" * 40, "DISMISSED")),
        )
        assert _mod._codex_review_commit_ids("1372") == ["a" * 40]
        assert _mod._latest_codex_reviewed_sha("1372") == "a" * 40

    def test_freshness_none_when_all_dismissed(self, monkeypatch):
        # All-dismissed → no vouched commit → None → the #1366 merge gate
        # fail-CLOSED blocks (a dismissed-only PR is never "reviewed at HEAD").
        monkeypatch.setenv(
            "_TEST_GH_CODEX_REVIEWS",
            _reviews_jsonl_stated(("a" * 40, "DISMISSED"), ("b" * 40, "DISMISSED")),
        )
        assert _mod._codex_review_commit_ids("1372") == []
        assert _mod._latest_codex_reviewed_sha("1372") is None


class TestDismissedRoundCounting:
    """Codex round 5 (#1385): dismissed reviews STILL RAN as rounds and consumed
    the review budget, so the escalation counter must count them — even though
    freshness skips them."""

    def test_dismissed_reviews_count_toward_escalation_cap(self, monkeypatch):
        # CAP dismissed Codex reviews = CAP rounds already ran → a new unacked
        # trigger is round N+1 and must BLOCK (was: allowed, count skipped them).
        monkeypatch.setenv(
            "_TEST_GH_CODEX_REVIEWS",
            _reviews_jsonl_stated(*[(f"{i:x}" * 40, "DISMISSED") for i in range(1, CAP + 1)]),
        )
        block, _ = _check(TRIGGER)
        assert block is True

    def test_dismissed_below_cap_still_allows(self, monkeypatch):
        monkeypatch.setenv(
            "_TEST_GH_CODEX_REVIEWS",
            _reviews_jsonl_stated(*[(f"{i:x}" * 40, "DISMISSED") for i in range(1, CAP)]),
        )
        assert _check(TRIGGER) == (False, "")


class TestEscalationSharedDeadline:
    """P1 (round 6): the escalation scan's gh (_codex_reviews) calls must be
    bounded by the shared hook deadline — otherwise a slow API + a compound
    command can exceed the ~60s hook wall-clock, CC SIGKILLs the hook, and EVERY
    gate (force-push/merge/sqlite) fails open. The gate must arm _merge_deadline
    before its first gh call."""

    def test_escalation_scan_arms_shared_deadline_before_gh_calls(self, monkeypatch):
        seen = {}

        def fake_reviews(pr, repo=None):
            seen["deadline_armed"] = _mod._merge_deadline is not None
            return []

        monkeypatch.setattr(_mod, "_codex_reviews", fake_reviews)
        monkeypatch.setattr(_mod, "_merge_deadline", None)
        _mod._check_codex_round_escalation(_segs(TRIGGER))
        assert seen.get("deadline_armed") is True

    def test_escalation_does_not_reset_an_already_armed_deadline(self, monkeypatch):
        import time as _time

        preset = _time.monotonic() + 5.0
        monkeypatch.setattr(_mod, "_merge_deadline", preset)
        monkeypatch.setattr(_mod, "_codex_reviews", lambda pr, repo=None: [])
        _mod._check_codex_round_escalation(_segs(TRIGGER))
        # Must not extend a deadline a prior gate already armed (aggregate budget).
        assert _mod._merge_deadline == preset


class TestEscalationGate:
    def test_at_cap_blocks_with_step_back_order(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        block, msg = _check(TRIGGER)
        assert block is True
        low = msg.lower()
        # The advisory must carry all three disciplines + the continue path.
        assert "triage" in low
        assert "mechanism" in low
        assert "state" in low and "enumerate" in low
        assert "revert" in low
        assert "escalation-ack" in msg

    def test_above_cap_blocks(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP + 2)))
        block, _ = _check(TRIGGER)
        assert block is True

    def test_below_cap_allows_silently(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP - 1)))
        assert _check(TRIGGER) == (False, "")

    def test_ack_sigil_allows(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP + 1)))
        block, _ = _check(TRIGGER + "  # escalation-ack")
        assert block is False

    def test_non_codex_comment_untouched(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        assert _check('gh pr comment 1372 --body "great work"') == (False, "")

    def test_non_comment_commands_untouched(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        for cmd in ("git status", "gh pr view 1372", "echo '@codex review'"):
            assert _check(cmd) == (False, "")

    def test_numberless_comment_fails_open(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        assert _check('gh pr comment --body "@codex review"') == (False, "")

    def test_variable_target_fails_closed(self, monkeypatch):
        """A `$n` in the PR position is unknowable to a PreToolUse hook, so it
        must REFUSE rather than skip. This is the 2026-09-08 bypass: the request
        written in a `for` loop posted rounds on two PRs already at/past the cap
        with no ack, because an unresolvable target silently fell through."""
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        block, msg = _check('gh pr comment $n --body "@codex review"')
        assert block is True
        assert "$n" in msg

    def test_variable_target_fails_closed_even_below_cap(self, monkeypatch):
        """Below the cap too: the refusal is about being unable to COUNT, not
        about the count. Allowing it under the cap would still be guessing."""
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(0)))
        assert _check('gh pr comment $n --body "@codex review"')[0] is True

    def test_every_parameter_form_in_the_target_fails_closed(self, monkeypatch):
        """The CLASS, enumerated — and the enumeration is not the mechanism.

        The gate ALLOWLISTS gh's documented target spellings (a number, #N, a
        PR URL, a branch in shell-inert characters); everything else is refused
        by construction. These forms are the ones a screen would have had to
        list — `$@`/`$*` (a wrapper forwarding its argument), the indirect and
        length forms, the special parameters, substitutions, legacy arithmetic —
        and the point of the list is that a spelling NOT on it is refused too,
        so a new one cannot become the next round's finding."""
        # Counter at ZERO on purpose: at the cap a target that DID resolve would
        # block on the step-back path and this would stay green for the wrong
        # reason. Below the cap, only the identity refusal can block — and the
        # message is asserted, so the two verdicts cannot be confused.
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(0)))
        for target in (
            "$n",
            '"$PR"',
            "${PR}",
            "${PR:-9}",
            "${!name}",  # indirect expansion
            "${#name}",  # length
            '"$@"',  # a wrapper forwarding its whole argument list
            '"$*"',
            "$$",  # pid
            "$!",  # last background pid
            "$?",  # last exit status
            "$#",  # argument count
            "$-",  # current option flags
            "$(cat pr.txt)",
            "`cat pr.txt`",
            "$[1+1]",  # legacy arithmetic
            "~/pr",  # tilde expansion
            "pr-*",  # glob
            "{1,2}",  # brace expansion
        ):
            cmd = f'gh pr comment {target} --body "@codex review"'
            block, msg = _check(cmd)
            assert block is True, cmd
            assert "does not name a pull request" in msg, cmd

    def test_an_unmodelled_flag_makes_the_target_unreadable(self, monkeypatch):
        """The allowlist on the VALUE is only as good as the walk that decides
        WHICH token the target is — and that walk must not be the soft half.

        gh's parser bundles short flags, so `-ewR o/r <target>` is `-e -w -R o/r`
        and the real target sits two tokens later. A walk that merely SKIPS the
        unrecognised `-ewR` hands back `o/r` — an innocuous literal that passes
        the allowlist — and never looks at the target. So an unmodelled dash
        token seen before any positional makes the identity unreadable.

        MEASURED 2026-09-08: no bundle containing `-R` can both parse AND post
        (every companion short flag either conflicts with the body source or
        swallows the rest of the bundle), so there is no reachable exploit with
        today's gh — and 0 of 530 real round requests hit this leg. It is closed
        because the next gh flag is what makes it reachable."""
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(0)))
        block, msg = _check('gh pr comment -ewR o/r $PR --body "@codex review"')
        assert block is True
        assert "-ewR" in msg

    def test_every_modelled_flag_spelling_still_resolves(self, monkeypatch):
        """The other half of the flag walk: every spelling gh documents must
        still reach the count. A closed set that refuses real flags would be a
        fail-closed bug rather than a fix."""
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        for form in (
            'gh pr comment 1372 -b "@codex review"',
            'gh pr comment 1372 -b"@codex review"',
            'gh pr comment 1372 --body="@codex review"',
            'gh pr comment --repo o/r 1372 --body "@codex review"',
            'gh pr comment -F body.md 1372 --body "@codex review"',
            'gh pr comment --edit-last 1372 --body "@codex review"',
            'gh pr comment -e 1372 --body "@codex review"',
            'gh pr comment -- 1372 --body "@codex review"',
            'gh pr comment 1372 --body "@codex review" >/dev/null 2>&1',
        ):
            block, msg = _check(form)
            assert block is True, form
            assert "does not name a pull request" not in msg, form

    def test_a_literal_url_with_a_path_or_fragment_is_not_refused(self, monkeypatch):
        """What a browser actually copies must not be read as an expansion.

        `/files`, `/commits/<sha>` and a `#issuecomment-…` deep link are all
        shell-inert — `#` opens a comment only at word start — so they resolve
        to their PR and reach the count. An expansion or a glob anywhere in the
        same URL still refuses (`?` is a glob metacharacter, hence excluded)."""
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        for form in (
            "https://github.com/o/r/pull/7",
            "https://github.com/o/r/pull/7/",
            "https://github.com/o/r/pull/7/files",
            "https://github.com/o/r/pull/7/commits/abc123",
            "https://github.com/o/r/pull/7#issuecomment-123",
        ):
            block, msg = _check(f'gh pr comment {form} --body "@codex review"')
            assert block is True, form  # the STEP-BACK block: it resolved
            assert "does not name a pull request" not in msg, form
        for form in (
            '"https://github.com/$O/r/pull/7/files"',
            '"https://github.com/o/r/pull/7?x=*"',
        ):
            block, msg = _check(f'gh pr comment {form} --body "@codex review"')
            assert block is True, form
            assert "does not name a pull request" in msg, form

    def test_unreadable_repo_fails_closed_with_a_literal_pr_number(self, monkeypatch):
        """The repo is identity-bearing too, and it is read on a path the target
        check cannot cover.

        With a literal number and `--repo "$REPO"` the number resolves, so a
        check gated on "no PR number" never runs — and the count then queries
        the literal path `repos/$REPO/…`, which errors, and the error fails
        OPEN. Same class, different door."""
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(0)))
        for form in (
            'gh pr comment 1372 --repo "$REPO" --body "@codex review"',
            'gh pr comment 1372 --repo=$REPO --body "@codex review"',
            'gh pr comment 1372 -R "$REPO" --body "@codex review"',
            'gh pr comment 1372 -R$REPO --body "@codex review"',
            # The host segment is identity-bearing as well: checked as WRITTEN,
            # before the HOST/OWNER/REPO reduction drops it.
            'gh pr comment 1372 --repo "$H/o/r" --body "@codex review"',
            # A URL target carries a repo — an expansion inside it is refused
            # for the same reason an expanded --repo is.
            'gh pr comment https://github.com/$O/r/pull/7 --body "@codex review"',
        ):
            block, msg = _check(form)
            assert block is True, form
            assert "does not name a pull request" in msg, form

    def test_literal_repo_forms_still_resolve(self, monkeypatch):
        """Guard against over-refusal: every literal repo spelling the gate has
        always accepted must still reach the COUNT, not the identity refusal."""
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        for form in (
            'gh pr comment 1372 --repo o/r --body "@codex review"',
            'gh pr comment 1372 --repo=o/r --body "@codex review"',
            'gh pr comment 1372 -Ro/r --body "@codex review"',
            'gh pr comment 1372 --repo ghe.example.com/o/r --body "@codex review"',
            'gh pr comment 1372 --repo o.r_x/re-po --body "@codex review"',
        ):
            block, msg = _check(form)
            assert block is True, form  # at cap — the STEP-BACK block, not identity
            assert "does not name a pull request" not in msg, form
            assert "escalation-ack" in msg, form

    def test_literal_branch_target_still_fails_open(self, monkeypatch):
        """The documented fail-open for a BRANCH target is deliberate and stays:
        it is resolvable in principle, unlike an expansion. The allowlist admits
        the characters real branch names are made of."""
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        for branch in ("my-branch", "fix/codex-round-target", "release_2.0", "user@host"):
            cmd = f'gh pr comment {branch} --body "@codex review"'
            assert _check(cmd) == (False, ""), cmd

    def test_a_branch_named_like_an_expansion_is_refused(self, monkeypatch):
        """The ACCEPTED cost of the allowlist, locked so it stays a decision.

        `shell_parse._argv` runs `shlex.split` first, so quoting is gone before
        argv exists: a branch literally named `$PR` (legal per
        `git check-ref-format`) is indistinguishable here from a live expansion,
        and is refused. Recovering it means deciding expandability from raw
        quoting — hand-written shell-quote analysis inside the guard, whose
        failure direction is a reopened bypass rather than a rewrite. The refusal
        names the remedy, and it costs one command."""
        # Counter at zero, message asserted — see the note in the enumeration
        # test above: at the cap this would pass even if the target resolved.
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(0)))
        for cmd in (
            "gh pr comment '$PR' --body \"@codex review\"",
            'gh pr comment \\$PR --body "@codex review"',
            "gh pr comment '`release`' --body \"@codex review\"",
        ):
            block, msg = _check(cmd)
            assert block is True, cmd
            assert "does not name a pull request" in msg, cmd

    def test_a_value_flag_value_is_never_read_as_the_target(self, monkeypatch):
        """What `_COMMENT_VALUE_FLAGS` exists for (#1385 round-5): a PR URL
        inside a `--body` must not redirect the gate to an unrelated repo.

        The flag-FIRST shapes are the ones that exercise the skip — with the
        positional written first the walk returns before it ever reaches the
        flag, so those assertions pass whether the set is right or empty."""
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        # The body's URL names another repo; the TARGET is the trailing 1372.
        assert _mod._comment_target(
            ["gh", "pr", "comment", "--body", "https://github.com/evil/repo/pull/9", "1372"]
        ) == ("1372", None)
        assert _mod._comment_target(
            ["gh", "pr", "comment", "-b", "https://github.com/evil/repo/pull/9", "1372"]
        ) == ("1372", None)
        # End to end: a body-first request still resolves to its own PR.
        block, msg = _check(
            'gh pr comment --body "see https://github.com/evil/repo/pull/9 @codex review" 1372'
        )
        assert block is True
        assert "does not name a pull request" not in msg

    def test_expansion_in_body_is_not_a_target(self, monkeypatch):
        """Only the FIRST positional is the target. A `$` inside the body must
        not be read as one, or ordinary review prose starts getting refused.

        Every body here CARRIES the trigger phrase on purpose: without it the
        gate skips the segment before it looks at anything, so the assertion
        would hold whatever the target logic did. (A mutation run caught exactly
        that — the earlier version of this test passed with `--body` deleted
        from the value-flag set, because its bodies were never round requests.)"""
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP - 1)))
        assert _check('gh pr comment 42 --body "@codex review — costs $5 per $unit"') == (
            False,
            "",
        )
        assert _check('gh pr comment 42 -b "@codex review, $5"') == (False, "")
        # Flag FIRST — this one needs the value-flag skip to reach the target.
        assert _check('gh pr comment --body "@codex review — costs $5" 42') == (False, "")

    def test_a_second_positional_no_longer_resolves(self, monkeypatch):
        """`_comment_target` reads the FIRST positional only, where it used to
        scan every one for something number-shaped.

        `gh pr comment my-branch 1372` exits with "accepts at most 1 arg(s),
        received 2", so the second word never named the PR that would be
        commented on — resolving it read an identity out of a command that
        cannot run. Recorded so the change is a decision, not a rediscovery."""
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        assert _mod._comment_target(["gh", "pr", "comment", "my-branch", "1372"]) == (None, None)
        assert _check('gh pr comment my-branch 1372 --body "@codex review"') == (False, "")

    def test_an_empty_repo_value_reads_as_absent_in_both_spellings(self, monkeypatch):
        """`--repo=` and `-R=` are both commands gh rejects, and they must be
        treated alike: one used to refuse with an empty token in the message
        while the other read as no flag at all."""
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        assert _mod._comment_repo_value(["gh", "pr", "comment", "1372", "--repo="]) is None
        assert _mod._comment_repo_value(["gh", "pr", "comment", "1372", "-R="]) is None
        for form in (
            'gh pr comment 1372 --repo= --body "@codex review"',
            'gh pr comment 1372 -R= --body "@codex review"',
        ):
            block, msg = _check(form)
            assert block is True, form
            assert "does not name a pull request" not in msg, form

    def test_variable_target_on_non_codex_comment_untouched(self, monkeypatch):
        """The refusal is scoped to a round REQUEST. An ordinary comment with a
        variable target is none of this gate's business."""
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        assert _check('gh pr comment $n --body "nice"') == (False, "")

    def test_ack_does_not_clear_an_unresolvable_target(self, monkeypatch):
        """The ack must NOT rescue this one, and that is the whole design.

        `acked` is computed command-wide, so a single sigil on
        `for n in …; do gh pr comment $n …; done` would license a round on every
        PR the loop touches — the unbounded version of the chaining the terminal
        tier already spends a license to stop. An acknowledgement names a
        conscious decision about ONE PR; when the PR is unknowable the sigil has
        nothing to attach to, so it cannot be honoured."""
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        cmd = 'gh pr comment $n --body "@codex review"  # escalation-ack'
        assert _check(cmd)[0] is True

    def test_api_error_fails_open(self, monkeypatch):
        monkeypatch.delenv("_TEST_GH_CODEX_REVIEWS", raising=False)

        def boom(*a, **k):
            raise OSError("no network in tests")

        monkeypatch.setattr(_mod.subprocess, "run", boom)
        assert _check(TRIGGER) == (False, "")

    def test_url_and_hash_pr_forms_resolve(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        for form in (
            'gh pr comment "#1372" --body "@codex review"',
            'gh pr comment https://github.com/o/r/pull/1372 --body "@codex review"',
        ):
            block, _ = _check(form)
            assert block is True, form

    def test_explicit_repo_flag_parsed(self):
        argv = ["gh", "pr", "comment", "7", "--repo", "o/r", "--body", "x"]
        assert _mod._comment_target(argv) == ("7", "o/r")

    def test_interposed_repo_flag_does_not_evade_subcommand_parse(self, monkeypatch):
        # Round-3 finding, UPGRADED in scope: `gh pr -R o/r <sub>` returned the
        # flag VALUE ('o/r') as the subcommand — bypassing not just this
        # advisory but EVERY downstream arm keyed on gh_pr_subcommand,
        # including the fail-closed merge gates ('gh pr -R o/r merge N --admin'
        # ran ungated on main). The parser must consume the flag's value.
        from shell_parse import analyze, gh_pr_subcommand

        for cmd, want in (
            ("gh pr -R o/r merge 7 --squash --admin", "merge"),
            ("gh pr --repo o/r comment 7 --body x", "comment"),
            ("gh pr -R o/r create --title t", "create"),
        ):
            segs = analyze(cmd)
            assert gh_pr_subcommand(segs[0].argv) == want, cmd
        # End-to-end: the interposed form now reaches the escalation gate.
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        block, _ = _check('gh pr -R WingedGuardian/GENesis-AGI comment 7 --body "@codex review"')
        assert block is True

    def test_interposed_repo_flag_merge_still_hits_admin_gate(self, monkeypatch):
        # The merge arm must fire on the interposed form: without --admin it
        # blocks on pure parsing (network-free) — previously exit 0 (ungated).
        monkeypatch.setattr(
            _mod,
            "read_payload",
            lambda: {"tool_input": {"command": "gh pr -R o/r merge 7 --squash"}},
        )
        assert _mod.main() == 2

    def test_host_qualified_repo_reduced_to_owner_repo(self):
        argv = ["gh", "pr", "comment", "7", "--repo", "ghe.example.com/o/r", "--body", "x"]
        assert _mod._comment_repo(argv) == "o/r"

    def test_url_inside_body_value_not_taken_as_pr_target(self):
        # Round-5 (#1385): a legit branch-target comment whose --body TEXT
        # contains a PR URL must NOT resolve to that URL's repo/number — the
        # gate would otherwise count/block against an unrelated repo. The
        # positional target is the branch ('feature') → non-numeric → fail-open.
        argv = [
            "gh",
            "pr",
            "comment",
            "feature",
            "--body",
            "https://github.com/other/project/pull/7 @codex review",
        ]
        assert _mod._comment_target(argv) == (None, None)

    def test_url_inside_short_body_flag_not_taken_as_target(self):
        argv = [
            "gh",
            "pr",
            "comment",
            "feature",
            "-b",
            "https://github.com/o/r/pull/9 @codex review",
        ]
        assert _mod._comment_target(argv) == (None, None)

    def test_glued_body_forms_not_taken_as_target(self):
        # Glued value forms (`--body=…`, `-b…`) are single `-`-prefixed tokens
        # already skipped by the generic dash-prefix branch — a URL inside them
        # must not resolve as the target either.
        assert _mod._comment_target(
            ["gh", "pr", "comment", "feature", "--body=https://github.com/o/r/pull/9"]
        ) == (None, None)
        assert _mod._comment_target(
            ["gh", "pr", "comment", "feature", "-bhttps://github.com/o/r/pull/9"]
        ) == (None, None)

    def test_real_url_target_still_resolves_with_body(self):
        # Guard against over-skipping: a genuine URL TARGET still resolves even
        # when a --body value is also present.
        argv = [
            "gh",
            "pr",
            "comment",
            "https://github.com/o/r/pull/12",
            "--body",
            "@codex review",
        ]
        assert _mod._comment_target(argv) == ("12", "o/r")


class TestCodexRound1Fixes:
    """Codex round-1 findings on the gate itself (compound-scan, URL repo,
    outer ack, remediation repo) — each witnessed RED before its fix."""

    def test_compound_scans_every_comment_segment(self, monkeypatch):
        # Finding 2: an allowed FIRST segment must not return for the whole
        # command — a later at-cap request in the same compound must still block.
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        cmd = (
            'gh pr comment --body "@codex review" && '  # numberless → allowed
            'gh pr comment 1372 --body "@codex review"'  # at cap → must block
        )
        block, _ = _check(cmd)
        assert block is True

    def test_url_target_carries_its_repo_into_the_count(self, monkeypatch):
        # Finding 1: a PR-URL target names its repo — the count must query THAT
        # repo, not the hook cwd's. Capture the api path via a fake subprocess.
        monkeypatch.delenv("_TEST_GH_CODEX_REVIEWS", raising=False)
        seen = {}

        def fake_run(argv, **kwargs):
            seen["path"] = next(a for a in argv if a.startswith("repos/"))

            class R:
                returncode = 0
                stdout = _reviews_jsonl(*_shas(CAP))

            return R()

        monkeypatch.setattr(_mod.subprocess, "run", fake_run)
        block, _ = _check(
            'gh pr comment https://github.com/other/project/pull/7 --body "@codex review"'
        )
        assert block is True
        assert seen["path"] == "repos/other/project/pulls/7/reviews"

    def test_glued_repo_flag_parsed(self):
        argv = ["gh", "pr", "comment", "7", "-Rother/project", "--body", "x"]
        assert _mod._comment_repo(argv) == "other/project"

    def test_outer_ack_on_nested_command_honored(self, monkeypatch):
        # Finding 4: `bash -c '…' # escalation-ack` — the inner gh segment's raw
        # text lacks the outer trailing comment; a whole-command trailing ack
        # must still count as the conscious continue (false-block prevention).
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        cmd = "bash -c 'gh pr comment 1372 --body \"@codex review\"'  # escalation-ack"
        block, _ = _check(cmd)
        assert block is False

    def test_double_trigger_in_one_command_counts_both(self, monkeypatch):
        # Round-2 finding: at CAP-1 existing reviews, `request && request` for
        # the SAME PR must block — each segment previously fetched the same
        # pre-execution count, so both passed and the second dispatched round
        # N+1 unacked. The scan must count earlier trigger segments in the
        # same command toward the round total.
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP - 1)))
        cmd = (
            'gh pr comment 1372 --body "@codex review" && gh pr comment 1372 --body "@codex review"'
        )
        block, _ = _check(cmd)
        assert block is True
        # A single trigger at CAP-1 still passes (it IS round CAP, allowed).
        block, _ = _check('gh pr comment 1372 --body "@codex review"')
        assert block is False

    def test_block_message_preserves_repo_flag(self, monkeypatch):
        # Finding 5: the printed remediation must keep --repo, or copying it
        # posts the trigger against the wrong repository.
        monkeypatch.delenv("_TEST_GH_CODEX_REVIEWS", raising=False)

        def fake_run(argv, **kwargs):
            class R:
                returncode = 0
                stdout = _reviews_jsonl(*_shas(CAP))

            return R()

        monkeypatch.setattr(_mod.subprocess, "run", fake_run)
        block, msg = _check('gh pr comment 7 --repo other/project --body "@codex review"')
        assert block is True
        assert "--repo other/project" in msg


class TestSoftDependency:
    def test_module_loads_without_review_state(self, monkeypatch):
        """CRITICAL (reviewer, round 1): an unguarded top-level import of
        review_state would crash the WHOLE guard at module load (exit 1 → CC
        treats as non-blocking → every fail-closed gate silently gone). The
        import must be soft: review_state unimportable → module still loads,
        cap falls back to its documented default."""
        import sys as _sys

        monkeypatch.setitem(_sys.modules, "review_state", None)  # forces ImportError
        spec = importlib.util.spec_from_file_location(
            "git_push_guard_no_review_state", _HOOKS_DIR / "git_push_guard.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # must NOT raise
        assert mod.ESCALATION_ROUND_CAP == 3  # documented fallback = the prose cap

    def test_module_loads_when_review_state_import_raises_non_importerror(self, monkeypatch):
        """P1 (round 6): a BROKEN review_state (SyntaxError / read error, not just
        absent) raised a NON-ImportError past the `except ImportError`, propagating
        out of module load → exit 1 (non-blocking) → EVERY fail-closed gate silently
        gone. Any import failure must degrade to the default cap, not crash."""
        import sys as _sys

        class _BoomModule:
            @property
            def ESCALATION_ROUND_CAP(self):  # simulates a broken review_state
                raise RuntimeError("broken review_state (SyntaxError/read error)")

        monkeypatch.setitem(_sys.modules, "review_state", _BoomModule())
        spec = importlib.util.spec_from_file_location(
            "git_push_guard_boom_review_state", _HOOKS_DIR / "git_push_guard.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # must NOT raise
        assert mod.ESCALATION_ROUND_CAP == 3


class TestMainWiring:
    def _run_main(self, monkeypatch, cmd: str) -> int:
        monkeypatch.setattr(_mod, "read_payload", lambda: {"tool_input": {"command": cmd}})
        return _mod.main()

    def test_main_blocks_at_cap(self, monkeypatch, capsys):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        rc = self._run_main(monkeypatch, TRIGGER)
        assert rc == 2
        assert "escalation-ack" in capsys.readouterr().err

    def test_main_allows_below_cap(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP - 1)))
        assert self._run_main(monkeypatch, TRIGGER) == 0

    def test_main_allows_with_ack(self, monkeypatch):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(CAP)))
        assert self._run_main(monkeypatch, TRIGGER + "  # escalation-ack") == 0


class TestCodexRoundTerminal:
    """The terminal has to live HERE too, not only in the commit gate.

    The commit-side terminal reads `review_state`'s local lifetime counter. This
    gate's own docstring records why that counter is not enough: it "stays asleep
    when every local review is clean while the loop churns through CODEX rounds on
    the PR — the exact blind spot of the 2026-08-12 MW-3 #1372 whack-a-mole (5
    Codex rounds, local counter at 0)". A terminal built on the sleeping counter
    inherits the blind spot, so the normal review/fix loop could run past seven
    rounds without ever reaching it. This tier counts the PR's REAL review history.
    """

    def _at(self, monkeypatch, n: int):
        monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", _reviews_jsonl(*_shas(n)))

    def test_escalation_ack_does_not_clear_the_terminal(self, monkeypatch):
        """The load-bearing case: the repeatable sigil must stop working here."""
        self._at(monkeypatch, _mod.FINAL_ROUND_CAP)
        blocked, msg = _check(f"{TRIGGER}  # escalation-ack")
        assert blocked, "an escalation-ack past the terminal must NOT dispatch"
        assert "terminal" in msg.lower()

    def test_escalation_ack_still_works_below_the_terminal(self, monkeypatch):
        """The control. Without this, a terminal that blocked everything would pass."""
        self._at(monkeypatch, _mod.ESCALATION_ROUND_CAP)
        assert _check(f"{TRIGGER}  # escalation-ack")[0] is False
        assert _check(TRIGGER)[0] is True, "control: unacked at the cap must block"

    def test_final_round_accept_clears_the_terminal(self, monkeypatch):
        self._at(monkeypatch, _mod.FINAL_ROUND_CAP)
        assert _check(TRIGGER)[0] is True, "control: the terminal must be live here"
        assert _check(f"{TRIGGER}  # final-round-accept")[0] is False

    def test_below_the_terminal_is_untouched(self, monkeypatch):
        self._at(monkeypatch, _mod.ESCALATION_ROUND_CAP - 1)
        assert _check(TRIGGER)[0] is False

    def test_terminal_message_names_the_sigil_that_works(self, monkeypatch):
        """A block printing only the sigil that does NOT work teaches a dead loop."""
        self._at(monkeypatch, _mod.FINAL_ROUND_CAP)
        msg = _check(TRIGGER)[1]
        assert "final-round-accept" in msg
        assert "user" in msg.lower()

    def test_one_acceptance_licenses_only_one_dispatch(self, monkeypatch):
        """`request && request  # final-round-accept` must not chain rounds.

        The sigil is matched command-wide on purpose — a nested
        `bash -c '…' # final-round-accept` carries it on the OUTER segment, so
        per-segment binding would break the documented nested form. That same
        command-wide truth let one decision authorise every trigger in a compound,
        while the advisory promises the user directed "ONE more round".
        """
        self._at(monkeypatch, _mod.FINAL_ROUND_CAP)
        one = f"{TRIGGER}  # final-round-accept"
        assert _check(one)[0] is False, "control: a single licensed dispatch allows"
        blocked, msg = _check(f"{TRIGGER} && {TRIGGER}  # final-round-accept")
        assert blocked, "a second terminal-stage dispatch rode the same acceptance"
        assert "one dispatch" in msg.lower() or "second terminal-stage" in msg.lower()

    def test_the_nested_form_still_works_at_the_terminal(self, monkeypatch):
        """Guard the guard: the fix must not break the sigil-on-the-outer-segment form."""
        self._at(monkeypatch, _mod.FINAL_ROUND_CAP)
        nested = f"bash -c '{TRIGGER}'  # final-round-accept"
        assert _check(nested)[0] is False, "nested single dispatch must still be licensed"

    def test_an_unreadable_count_fails_open(self, monkeypatch):
        """Advisory posture unchanged: an unreadable count never hard-blocks.

        Named for what it proves. It cannot be terminal-specific — with the count
        unreadable there is no count of any size, so `reviews is None` short-circuits
        before either tier. The terminal's own fail-open is covered by the pairing
        below, which establishes a real terminal count and THEN breaks the read.
        """
        monkeypatch.setattr(_mod, "_codex_reviews", lambda pr, repo=None: None)
        assert _check(TRIGGER)[0] is False

    def test_a_read_error_at_a_real_terminal_count_still_fails_open(self, monkeypatch):
        """Control: the terminal blocks on a readable count, and only on one."""
        self._at(monkeypatch, _mod.FINAL_ROUND_CAP)
        assert _check(TRIGGER)[0] is True, "control: a readable terminal count blocks"
        monkeypatch.setattr(_mod, "_codex_reviews", lambda pr, repo=None: None)
        assert _check(TRIGGER)[0] is False, "an unreadable count must not hard-block"
