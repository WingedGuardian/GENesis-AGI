"""The review ROUND rule: a round is a head that drew findings.

Owner rulings, 2026-10-01: any GitHub App reviewer's finding-bearing review opens
a round; only an explicit informational class (Codex P3, Devin 🔍, CodeRabbit
trivial/info and its nitpick section) does not; clean signals confirm a head and
never add one; evidence from before ``ROUND_RULE_CUTOVER_ISO`` keeps the old
Codex-only count.

Every test passes ``cutover`` explicitly, so none depends on the shipped date.
"""

from __future__ import annotations

import json
import random
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

import review_budget as rb  # noqa: E402
import review_findings as rf  # noqa: E402

H1, H2, H3, H4, H5 = ("1" * 40, "2" * 40, "3" * 40, "4" * 40, "5" * 40)
COMMITS = (H1, H2, H3, H4, H5)
CUT = "2026-06-01T00:00:00+00:00"
BEFORE = "2026-01-01T00:00:00Z"
AFTER = "2026-07-01T00:00:00Z"
LATER = "2026-07-02T00:00:00Z"

CODEX = rf.CODEX_LOGIN
DEVIN = "devin-ai-integration[bot]"
RABBIT = "coderabbitai[bot]"
STRANGER = "acme-reviewer[bot]"  # an App with no known format
GATE_FILE = "scripts/hooks/git_push_guard.py"

P1 = "**<sub><sub>![P1 Badge](https://img.shields.io/badge/P1-orange)</sub></sub>  Broken**"
P2 = "**<sub><sub>![P2 Badge](https://img.shields.io/badge/P2-yellow)</sub></sub>  Off by one**"
P3 = "**<sub><sub>![P3 Badge](https://img.shields.io/badge/P3-grey)</sub></sub>  Style**"
CR_MAJOR = "_🎯 Functional Correctness_ | _🟠 Major_ | _⚡ Quick win_\n\n**Wrong.**"
CR_TRIVIAL = "_🧹 Nitpick_ | _🔵 Trivial_\n\n**Rename.**"


def _devin(marker: str, fid: str = "BUG_0001") -> str:
    meta = json.dumps({"id": fid, "kind": "bug"})
    return f"<!-- devin-review-comment {meta} -->\n\n{marker} **Title**\n\nWhy.\n"


def _rv(login, head, *tops, when=AFTER, body="", state="COMMENTED"):
    return {
        "login": login,
        "commit_id": head,
        "state": state,
        "submitted_at": when,
        "body": body,
        "top_level": list(tops),
    }


def _cm(body, *, login=CODEX, kind="Bot", when=AFTER):
    return {"login": login, "type": kind, "body": body, "created_at": when}


def _ev(
    *,
    head=H5,
    reviews=(),
    comments=(),
    files=("src/x.py",),
    commits=COMMITS,
    cutover=CUT,
    templates=(),
):
    return rb.evaluate_evidence(
        current_head=head,
        commit_heads=commits,
        reviews=reviews,
        issue_comments=comments,
        changed_files=files,
        cutover=cutover,
        external_identity_templates=templates,
    )


def _count(**kw):
    got = _ev(**kw)
    assert got["status"] == "ok", got
    return got["count"]


# -- what opens a round ------------------------------------------------------


@pytest.mark.parametrize(
    "review, rounds",
    [
        (_rv(CODEX, H1), 0),  # a clean Codex review confirms, never advances
        (_rv(CODEX, H1, P3), 0),
        (_rv(CODEX, H1, P2), 1),
        (_rv(CODEX, H1, P1), 1),
        (_rv(CODEX, H1, "an unbadged Codex comment"), 1),  # drift counts
        (_rv(DEVIN, H1, _devin("🟡")), 1),
        (_rv(DEVIN, H1, _devin("🔴")), 1),
        (_rv(DEVIN, H1, _devin("🔍")), 0),
        (_rv(DEVIN, H1, "no metadata at all"), 1),  # drift counts
        (_rv(RABBIT, H1, CR_MAJOR), 1),
        (_rv(RABBIT, H1, CR_TRIVIAL), 0),
        (_rv(RABBIT, H1, "a comment with no header"), 1),  # drift counts
        (_rv(STRANGER, H1, "anything at all"), 1),  # unknown format: any comment
        (_rv(STRANGER, H1), 0),  # reply wrappers / body-only: no top-level comment
        (_rv("github-actions[bot]", H1, P1), 0),
        (_rv("github-advanced-security[bot]", H1, "## CodeQL / x"), 0),
        (_rv("a-maintainer", H1, P1), 0),  # humans never open a round
        (_rv(CODEX, H1, P2, state="DISMISSED"), 1),  # a dismissed round was still spent
    ],
)
def test_what_opens_a_round(review, rounds):
    assert _count(reviews=(review,)) == rounds


@pytest.mark.parametrize(
    "summary, rounds",
    [
        ("🧹 Nitpick comments (2)", 0),
        ("⚠️ Outside diff range comments (1)", 1),
        ("♻️ Duplicate comments (1)", 1),
        ("⚠️ Outside diff range comments (0)", 0),
    ],
)
def test_coderabbit_body_sections(summary, rounds):
    body = f"**Actionable comments posted: 0**\n<details>\n<summary>{summary}</summary>\n</details>"
    assert _count(reviews=(_rv(RABBIT, H1, body=body),)) == rounds


def test_two_reviewers_on_one_head_are_one_round():
    got = _ev(reviews=(_rv(CODEX, H1, P2, P2), _rv(DEVIN, H1, _devin("🟡"))))
    assert got["count"] == 1
    (only,) = got["rounds"]
    assert only["findings"] == 3 and set(only["reviewers"]) == {CODEX, DEVIN}


def test_a_late_review_stays_on_its_own_head():
    """Late attachment was dropped (owner, 2026-10-01): a review that lands after
    a newer head was reviewed still counts on the head it names."""
    got = _ev(reviews=(_rv(DEVIN, H2, _devin("🟡"), when=AFTER), _rv(CODEX, H1, P2, when=LATER)))
    assert got["count"] == 2


def test_a_force_pushed_away_finding_head_counts_and_sorts_first():
    gone = "f" * 40
    got = _ev(reviews=(_rv(CODEX, gone, P1), _rv(DEVIN, H2, _devin("🔴"))))
    assert got["count"] == 2
    assert [r["head"] for r in got["rounds"]] == [gone, H2]


# -- the Codex findings issue comment ----------------------------------------


def _bulb(*shas):
    links = "\n".join(f"https://github.com/o/r/blob/{s}/x.py#L1" for s in shas)
    return f"### 💡 Codex Review\n\n{links}\n{P1}\n"


def test_a_codex_findings_comment_is_a_round_on_its_permalink_head():
    got = _ev(comments=(_cm(_bulb(H3, H3)),))
    assert got["count"] == 1 and got["reviewed_heads"] == [H3]


@pytest.mark.parametrize("shas", [(), (H2, H3)])
def test_a_codex_findings_comment_without_one_head_is_unknown(shas):
    got = _ev(comments=(_cm(_bulb(*shas)),))
    assert got["status"] == "unknown"
    assert "codex_findings_comment_unbound" in got["errors"]


def test_a_codex_findings_comment_before_cutover_keeps_the_old_rule():
    assert _count(comments=(_cm(_bulb(H3), when=BEFORE),)) == 0


# -- inputs that must never read as fewer rounds -----------------------------


def test_a_pending_primary_review_keeps_its_old_rule_count():
    """The old rule counted every primary review, pending included."""
    assert _count(reviews=(_rv(CODEX, H1, when=None, state="PENDING"),)) == 1
    assert _count(reviews=(_rv(DEVIN, H1, _devin("🔴"), when=None, state="PENDING"),)) == 0


def test_a_review_without_a_commit_is_unknown():
    got = _ev(reviews=(_rv(DEVIN, None, _devin("🟡")),))
    assert got["status"] == "unknown" and "malformed_review_head" in got["errors"]


@pytest.mark.parametrize(
    "login, body",
    [
        (CODEX, "### 💡 Codex Review\n\nHere are some automated review suggestions."),
        (RABBIT, "**Actionable comments posted: 2**"),
        (DEVIN, "**Devin Review** found 1 potential issue."),
    ],
)
def test_findings_declared_but_deleted_are_unknown(login, body):
    got = _ev(reviews=(_rv(login, H1, body=body),))
    assert got["status"] == "unknown" and "review_findings_deleted" in got["errors"]


@pytest.mark.parametrize("when", ["not a time", "2026-07-01T00:00:00", 17])
def test_a_malformed_or_zoneless_review_time_is_unknown(when):
    got = _ev(reviews=(_rv(DEVIN, H1, _devin("🟡"), when=when),))
    assert got["status"] == "unknown" and "malformed_review_time" in got["errors"]


@pytest.mark.parametrize("tops", [None, "one string", [1]])
def test_unreadable_comments_on_a_counted_review_are_unknown(tops):
    review = _rv(DEVIN, H1)
    review["top_level"] = tops
    got = _ev(reviews=(review,))
    assert got["status"] == "unknown" and "review_comments_unreadable" in got["errors"]


def test_a_malformed_cutover_is_unknown():
    got = _ev(cutover="yesterday")
    assert got["status"] == "unknown" and "malformed_round_cutover" in got["errors"]


# -- clean signals and the cutover --------------------------------------------


def _clean(sha_prefix, when=AFTER):
    return _cm(
        f"Codex Review: Didn't find any major issues.\nReviewed commit: `{sha_prefix}`", when=when
    )


def test_a_clean_comment_after_cutover_confirms_without_counting():
    got = _ev(head=H3, comments=(_clean(H3[:10]),))
    assert got["count"] == 0 and got["current_head_reviewed"] is True


def test_an_unresolvable_clean_comment_after_cutover_is_ignored_before_it_is_unknown():
    gone = "abcdef1234"
    assert _ev(comments=(_clean(gone),))["status"] == "ok"
    before = _ev(comments=(_clean(gone, when=BEFORE),))
    assert before["status"] == "unknown" and "unresolved_review_head" in before["errors"]


def test_evidence_before_cutover_keeps_the_old_codex_only_count():
    got = _ev(
        reviews=(
            _rv(CODEX, H1, when=BEFORE),  # clean, but the old rule counted it
            _rv(DEVIN, H2, _devin("🔴"), when=BEFORE),  # not the primary: never counted
        ),
        comments=(_clean(H3[:10], when=BEFORE),),
    )
    assert got["reviewed_heads"] == [H1, H3]
    assert got["legacy_heads"] == 2
    assert all(r["legacy"] for r in got["rounds"])


def test_a_head_in_both_rules_counts_once():
    got = _ev(reviews=(_rv(CODEX, H1, when=BEFORE), _rv(DEVIN, H1, _devin("🟡"))))
    assert got["count"] == 1 and got["rounds"][0]["legacy"] is False


def _random_corpus(rng: random.Random, when_choices):
    reviews, comments = [], []
    for _ in range(rng.randint(0, 8)):
        login = rng.choice([CODEX, DEVIN, RABBIT, STRANGER, "a-maintainer"])
        tops = rng.choice([[], [P2], [P3], [_devin("🟡")], [_devin("🔍")], [CR_MAJOR], ["x"]])
        reviews.append(_rv(login, rng.choice(COMMITS), *tops, when=rng.choice(when_choices)))
    for _ in range(rng.randint(0, 3)):
        comments.append(_clean(rng.choice(COMMITS)[:10], when=rng.choice(when_choices)))
    return reviews, comments


def test_at_the_cutover_instant_every_count_is_the_old_count():
    """The grandfathering contract, as a property over random evidence: when all of
    it predates the cutover, the count is exactly the old rule's (every head the
    primary reviewed, by review object or clean comment)."""
    rng = random.Random(20261001)
    for _ in range(200):
        reviews, comments = _random_corpus(rng, [BEFORE])
        old = {r["commit_id"] for r in reviews if r["login"] == CODEX}
        old |= {
            next(h for h in COMMITS if h.startswith(c["body"].rsplit("`", 2)[1])) for c in comments
        }
        got = _ev(reviews=reviews, comments=comments)
        assert got["status"] == "ok", got
        assert set(got["reviewed_heads"]) == old


def test_adding_evidence_never_lowers_the_count_and_clean_evidence_never_changes_it():
    rng = random.Random(7)
    for _ in range(200):
        reviews, comments = _random_corpus(rng, [BEFORE, AFTER, LATER])
        base = _count(reviews=reviews, comments=comments)
        extra, _ = _random_corpus(rng, [AFTER, LATER])
        for item in extra:
            assert _count(reviews=[*reviews, item], comments=comments) >= base
        clean = _rv(rng.choice([DEVIN, RABBIT, STRANGER]), rng.choice(COMMITS), when=LATER)
        assert _count(reviews=[*reviews, clean], comments=comments) == base


# -- round state, trend, and the gate lane's one confirmation ----------------


def test_round_state_and_trend():
    assert _ev()["round_state"] == "none"
    open_ = _ev(head=H2, reviews=(_rv(CODEX, H1, P2), _rv(CODEX, H2, P2, P2)))
    assert open_["round_state"] == "open" and open_["trend"] == "rising"
    done = _ev(head=H3, reviews=(_rv(CODEX, H1, P2, P2), _rv(CODEX, H2, P2)))
    assert done["round_state"] == "complete" and done["trend"] == "falling"


def _gate(head, *extra_reviews, comments=()):
    rounds = (_rv(CODEX, H1, P2), _rv(DEVIN, H2, _devin("🟡")))
    return _ev(head=head, reviews=(*rounds, *extra_reviews), comments=comments, files=(GATE_FILE,))


def test_the_gate_lanes_confirmation_is_granted_once():
    first = _gate(H3)
    assert first["count"] == 2 and first["confirmation_exempt"] is True
    assert first["commit_approval_required"] is False


def test_a_requested_confirmation_spends_it_for_later_heads():
    marker = _cm(rb.confirmation_marker(H3), login="a-maintainer", kind="User")
    at_h3 = _gate(H3, comments=(marker,))
    assert at_h3["confirmation_requested"] is True and at_h3["confirmation_exempt"] is False
    assert at_h3["commit_approval_required"] is False  # the fix may still land
    at_h4 = _gate(H4, comments=(marker,))
    assert at_h4["confirmation_exempt"] is False and at_h4["commit_approval_required"] is True


def test_an_older_clean_review_does_not_spend_it():
    got = _gate(H3, _rv(CODEX, H1))
    assert got["confirmation_exempt"] is True


# -- the shipped cutover constant ---------------------------------------------


def test_the_shipped_cutover_is_a_real_moment_at_or_before_now():
    """An inert far-future placeholder would leave the new rule switched off; a
    date before this change was written would move open PRs retroactively."""
    ok, when = rb._parse_time(rb.ROUND_RULE_CUTOVER_ISO)
    assert ok and when is not None
    assert when >= datetime(2026, 10, 1, tzinfo=UTC)
    assert when <= datetime.now(UTC)


# -- the GraphQL read ----------------------------------------------------------


def _node(head, *, tops=(), replies=(), body="", when=AFTER, more=False, state="COMMENTED"):
    return {
        "state": state,
        "submittedAt": when,
        "body": body,
        "author": {"login": "devin-ai-integration", "__typename": "Bot"},
        "commit": {"oid": head},
        "comments": {
            "pageInfo": {"hasNextPage": more},
            "nodes": [{"replyTo": None, "body": b} for b in tops]
            + [{"replyTo": {"id": "x"}, "body": b} for b in replies],
        },
    }


def test_graphql_rows_keep_only_top_level_comments():
    rows, _ = rb._graphql_rows("reviews", [_node(H1, tops=["a"], replies=["b"])])
    assert rows[0]["top_level"] == ["a"]
    assert rows[0]["login"] == DEVIN and rows[0]["submitted_at"] == AFTER


def test_graphql_review_comments_past_one_page_are_truncated():
    with pytest.raises(rb._Truncated):
        rb._graphql_rows("reviews", [_node(H1, more=True)])


def test_graphql_submitted_review_without_a_time_is_malformed():
    with pytest.raises(ValueError):
        rb._graphql_rows("reviews", [_node(H1, when=None)])
    rows, _ = rb._graphql_rows("reviews", [_node(H1, when=None, state="PENDING")])
    assert rows[0]["state"] == "PENDING"


def test_the_reread_ignores_prose_edits_but_not_new_findings():
    """CodeRabbit marks its comments "Addressed" right after a push, which is when
    sessions commit; comparing raw bodies would read that as `unknown`."""
    first = [rb._graphql_rows("reviews", [_node(H1, tops=[_devin("🟡")])])[0][0]]
    edited = [dict(first[0], top_level=[_devin("🟡") + "\n✅ Addressed in abc"])]
    withdrawn = [dict(first[0], top_level=[_devin("🔍")])]
    assert rb._review_digest(first, rf) == rb._review_digest(edited, rf)
    assert rb._review_digest(first, rf) != rb._review_digest(withdrawn, rf)


# -- the `--check-pr` rounds row ----------------------------------------------


@pytest.fixture
def guard():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "git_push_guard_rounds", _ROOT / "scripts" / "hooks" / "git_push_guard.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_row_reports_an_open_rising_terminal_round(guard):
    got = _ev(
        head=H2,
        reviews=(
            _rv(CODEX, H1, P2),
            _rv(DEVIN, H2, _devin("🟡"), _devin("🔴", "B2")),
            _rv(CODEX, H2),  # Codex has seen this head too: no confirmation left
        ),
        files=(GATE_FILE,),
    )
    row = guard._format_rounds(got)
    assert row.startswith("2 (gate lane, terminal at 2)")
    assert f"open at {H2[:10]} ({DEVIN})" in row
    assert "findings per round 1→2 (RISING)" in row
    assert "TERMINAL: a clean review merges" in row


def test_the_row_names_grandfathered_heads_and_a_complete_round(guard):
    got = _ev(head=H3, reviews=(_rv(CODEX, H1, when=BEFORE),))
    row = guard._format_rounds(got)
    assert row.startswith("1 (ordinary lane, terminal at 4)")
    assert "complete" in row and "1 head(s) counted by the pre-cutover rule" in row
    assert "TERMINAL" not in row


def test_the_row_reads_the_evaluator_when_unpinned(guard, monkeypatch):
    monkeypatch.delenv("_TEST_ROUNDS_ROW", raising=False)
    seen = {}

    def fake(repo, pr, **kw):
        seen["args"] = (repo, pr)
        return {"status": "unknown", "errors": ["lookup_budget_exhausted"]}

    monkeypatch.setattr(guard._review_budget, "evaluate_pr", fake)
    assert guard._rounds_row("7", "o/r") == "unreadable (lookup_budget_exhausted)"
    assert seen["args"] == ("o/r", "7")
    monkeypatch.setattr(guard._review_budget, "evaluate_pr", lambda *a, **k: 1 / 0)
    assert guard._rounds_row("7", "o/r") == "unreadable (lookup failed)"


# -- fixes from the fresh-context audit (2026-10-01) ----------------------------


def test_a_rereview_of_the_round_head_itself_does_not_spend_it():
    got = _gate(H3, _rv(DEVIN, H2, when=LATER), _rv(CODEX, H2, when=LATER))
    assert got["confirmation_exempt"] is True


def test_a_deleted_author_identity_report_still_counts_before_cutover():
    """Legacy equivalence: main matched the identity template on every comment
    body, including one whose author was deleted."""
    template = "external report head={head}"
    deleted = {
        "login": None,
        "type": None,
        "body": f"external report head={H2}",
        "created_at": BEFORE,
    }
    got = _ev(comments=(deleted,), templates=(template,))
    assert got["reviewed_heads"] == [H2]


def test_an_unresolvable_identity_report_after_cutover_is_ignored_before_it_is_unknown():
    template = "external report head={head}"
    gone = "a" * 40  # force-pushed out of the PR
    after = _ev(comments=(_cm(f"external report head={gone}"),), templates=(template,))
    assert after["status"] == "ok" and after["count"] == 0
    assert after["current_head_reviewed"] is False
    before = _ev(
        comments=(_cm(f"external report head={gone}", when=BEFORE),), templates=(template,)
    )
    assert before["status"] == "unknown" and "unresolved_review_head" in before["errors"]


def test_a_stale_findings_module_is_unknown_not_a_traceback(monkeypatch):
    monkeypatch.delattr(rf, "is_finding")
    got = _ev(reviews=(_rv(DEVIN, H1, _devin("🟡")),))
    assert got["status"] == "unknown" and "review_findings_unimportable" in got["errors"]


def test_the_row_does_not_call_a_budgeted_confirmation_terminal(guard):
    row = guard._format_rounds(_gate(H3))
    assert "one exact-head confirmation request still budgeted" in row
    assert "TERMINAL" not in row


# -- the spent rule's shape after the fix-code audit (2026-10-01) ---------------
# Heads still in the PR: commit-list position. Heads a force-push removed: time,
# against the newest round's FIRST event.

T1, T2, T3, T4 = (f"2026-07-0{d}T00:00:00Z" for d in (2, 3, 4, 5))


def _rounds_h1_h3(*extra, head=H4, comments=()):
    rounds = (_rv(CODEX, H1, P2, when=T1), _rv(DEVIN, H3, _devin("🟡"), when=T2))
    return _ev(head=head, reviews=(*rounds, *extra), comments=comments, files=(GATE_FILE,))


def test_a_late_clean_review_of_an_older_live_head_does_not_spend_it():
    """Reviewers run at different speeds: Codex finishing H2 after Devin's H3
    round is not a confirmation after round two."""
    got = _rounds_h1_h3(_rv(CODEX, H2, when=T3))
    assert got["confirmation_exempt"] is True and got["commit_approval_required"] is False


# -- the marker rule (owner ruling 2026-10-01, after round 1 of #2720) ---------
# The confirmation is spent once a marker exists for a head other than the
# current one. No ordering of reviews by commit position or time is consulted.


def _marker(sha, when=AFTER):
    return _cm(rb.confirmation_marker(sha), login="a-maintainer", kind="User", when=when)


def test_a_marker_for_a_force_pushed_away_head_spends_it():
    got = _gate(H3, comments=(_marker("f" * 40),))
    assert got["confirmation_exempt"] is False and got["commit_approval_required"] is True


def test_no_free_confirmation_on_a_head_that_is_itself_a_round():
    got = _gate(H2)  # round two drew findings on the current head: fix it first
    assert got["count"] == 2 and got["confirmation_exempt"] is False
    assert got["approval_required"] is True


def test_a_clean_review_without_a_marker_does_not_spend_it():
    """The documented residual: an owner-approved UNMARKED request at round two that
    comes back clean leaves one marked confirmation still free."""
    got = _gate(H4, _rv(CODEX, H3))
    assert got["confirmation_exempt"] is True


def test_spent_never_reads_ordering():
    """Devin 🔴 / Codex P2 on #2720: reviews landing out of order, or a head later
    in the commit list, must not change whether the confirmation was used."""
    late = _rounds_h1_h3(_rv(CODEX, H2, when=T4), _rv(CODEX, "f" * 40, when=T4))
    assert late["confirmation_exempt"] is True


def test_a_bundled_coderabbit_comment_counts_its_major_behind_a_trivial():
    bundled = CR_TRIVIAL + "\n\n" + CR_MAJOR
    assert rf.is_finding(RABBIT, bundled) is True
    assert rf.is_finding(RABBIT, CR_TRIVIAL + "\n\n" + CR_TRIVIAL) is False
    assert _count(reviews=(_rv(RABBIT, H1, bundled),)) == 1


def test_the_trend_ignores_a_force_pushed_round_it_cannot_place():
    got = _ev(reviews=(_rv(CODEX, "f" * 40, P2, P2, P2), _rv(DEVIN, H1, _devin("🟡"))))
    assert got["count"] == 2 and got["trend"] is None


def test_the_fix_commit_after_a_clean_marked_confirmation_asks():
    """Fix-code audit, round 2 of #2720: the confirmed head's own confirmation is
    spent once the primary reviewed it, so the next fix commit asks."""
    got = _gate(H3, _rv(CODEX, H3), comments=(_marker(H3),))
    assert got["confirmation_exempt"] is False and got["commit_approval_required"] is True


def test_a_coderabbit_source_footer_is_not_a_header():
    trivial = CR_TRIVIAL + "\n\n_Source: Linters/SAST tools_"
    assert rf.is_finding(RABBIT, trivial) is False
    assert rf.is_finding(RABBIT, CR_MAJOR + "\n\n_Source: Learnings_") is True


def test_an_unreadable_entry_after_a_trivial_one_still_counts():
    """Only the `_Source:` footer is skipped; a later entry whose header has no
    readable severity is drift, and drift counts."""
    assert rf.is_finding(RABBIT, CR_TRIVIAL + "\n\n_⚠️ Potential issue_\n\n**Wrong.**") is True
