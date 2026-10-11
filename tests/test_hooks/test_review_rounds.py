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


# -- reflection keys: what a round reflection must answer for ----------------
#
# A round reflection names every finding of the open round by an immutable key
# (`c<comment id>`, `r<review id>/<k>`, `i<issue comment id>`). The keys ride on
# the same evaluation as the count, and must never change the count.


def _keyed(rv, review_id, comment_ids):
    return dict(rv, id=review_id, top_level_ids=list(comment_ids))


def test_an_open_round_keys_each_finding_by_its_comment_id():
    got = _ev(
        head=H2,
        reviews=(
            _keyed(_rv(CODEX, H1, P2), 10, [100]),
            _keyed(_rv(CODEX, H2, P2, P3, P1), 11, [111, 112, 113]),
        ),
    )
    assert got["round_state"] == "open"
    # The P3 is informational: it opens no round and owes no disposition.
    assert got["open_keys"] == ["c111", "c113"]
    assert got["reflection_keys"] == "ok"
    assert got["rounds"][-1]["reviews"] == [
        {"id": 11, "login": CODEX, "submitted_at": AFTER, "finding_keys": ["c111", "c113"]}
    ]


def test_body_findings_and_a_codex_findings_comment_have_keys():
    body = (
        "**Actionable comments posted: 0**\n<details>\n"
        "<summary>⚠️ Outside diff range comments (2)</summary>\n</details>"
    )
    rabbit = _keyed(_rv(RABBIT, H5, body=body), 20, [])
    bulb = dict(_cm(_bulb(H5)), id=30)
    got = _ev(reviews=(rabbit,), comments=(bulb,))
    assert got["open_keys"] == ["r20:2", "i30"]
    assert got["reflection_keys"] == "ok"


def test_a_missing_id_makes_the_keys_unknown_and_never_the_count():
    keyed = _ev(head=H2, reviews=(_keyed(_rv(DEVIN, H2, _devin("🟡")), 40, [400]),))
    bare = _ev(head=H2, reviews=(_rv(DEVIN, H2, _devin("🟡")),))
    assert keyed["reflection_keys"] == "ok" and keyed["open_keys"] == ["c400"]
    assert bare["status"] == "ok" and bare["count"] == keyed["count"] == 1
    assert bare["reflection_keys"] == "unknown" and bare["open_keys"] == []


def test_a_complete_round_owes_nothing_and_a_late_review_stays_on_its_head():
    """Stated residual: no late attachment. A finding submitted on H1 after H2
    was pushed belongs to H1's round, which is complete, so nothing is owed."""
    got = _ev(
        head=H2,
        reviews=(
            _keyed(_rv(CODEX, H1, P2), 10, [100]),
            _keyed(_rv(DEVIN, H1, _devin("🟡"), when=LATER), 41, [410]),
        ),
    )
    assert got["round_state"] == "complete"
    assert got["open_keys"] == [] and got["reflection_keys"] == "ok"


def test_a_legacy_round_at_head_cannot_be_keyed():
    got = _ev(head=H2, reviews=(_keyed(_rv(CODEX, H2, P2, when=BEFORE), 10, [100]),))
    assert got["round_state"] == "open"
    assert got["reflection_keys"] == "unknown"


def test_who_reported_and_who_is_expected():
    """Round 1 expects nobody (the reader waits the whole window); later rounds
    expect every reviewer of an earlier head. A clean review and a Codex clean
    comment both report."""
    first = _ev(head=H1, reviews=(_keyed(_rv(DEVIN, H1, _devin("🟡")), 40, [400]),))
    assert first["expected_reviewers"] == []
    assert first["reviewers_reported"] == [DEVIN]
    later = _ev(
        head=H2,
        reviews=(
            _keyed(_rv(DEVIN, H1, _devin("🟡")), 40, [400]),
            _keyed(_rv(RABBIT, H1), 50, []),
            _keyed(_rv(RABBIT, H2), 51, []),
        ),
        comments=(_clean(H2[:10]),),
    )
    assert later["expected_reviewers"] == [RABBIT, DEVIN]
    assert later["reviewers_reported"] == [CODEX, RABBIT]


def test_ids_never_change_any_existing_field():
    """B1a-1's acceptance as a property: the same evidence with and without ids
    evaluates identically outside the new reflection fields."""
    new = {"open_keys", "reflection_keys", "rounds"}
    rng = random.Random(20261007)
    for _ in range(200):
        reviews, comments = _random_corpus(rng, [BEFORE, AFTER, LATER])
        keyed = [
            dict(
                r,
                id=1000 + i,
                top_level_ids=[5000 + 10 * i + j for j in range(len(r["top_level"]))],
            )
            for i, r in enumerate(reviews)
        ]
        plain = _ev(reviews=reviews, comments=comments)
        withids = _ev(reviews=keyed, comments=comments)
        assert {k: v for k, v in plain.items() if k not in new} == {
            k: v for k, v in withids.items() if k not in new
        }
        strip = [{k: v for k, v in r.items() if k != "reviews"} for r in withids.get("rounds", [])]
        assert strip == [
            {k: v for k, v in r.items() if k != "reviews"} for r in plain.get("rounds", [])
        ]


def test_unknown_carries_the_reflection_fields():
    got = rb._unknown("x")
    assert got["open_keys"] == [] and got["reflection_keys"] == "unknown"
    assert got["reviewers_reported"] == [] and got["expected_reviewers"] == []


@pytest.mark.parametrize(
    "raw, want",
    [
        ("6030876162", 6030876162),
        (6030876162, 6030876162),
        (None, None),
        ("", None),
        ("-1", None),
        ("12a", None),
        ("١٢", None),
        (True, None),
        (0, None),
    ],
)
def test_node_ids_come_from_full_database_id(raw, want):
    """`fullDatabaseId` is a BigInt sent as a string; ids already pass 2**31."""
    node = {} if raw is None else {"fullDatabaseId": raw}
    assert rb._node_id(node) == want


def test_the_query_never_asks_for_the_deprecated_database_id():
    """`databaseId` on reviews and review comments is deprecated (schema
    introspection, 2026-10-07). If GitHub removes it, a query naming it errors
    and every PR's budget goes unknown."""
    query = rb._graphql_query(list(rb._GRAPHQL_CONNECTIONS))
    assert "fullDatabaseId" in query
    assert " databaseId" not in query and "{databaseId" not in query


def test_graphql_rows_carry_ids_parallel_to_top_level():
    node = _node(H1, tops=["a", "b"], replies=["r"])
    node["fullDatabaseId"] = "7"
    node["comments"]["nodes"][0]["fullDatabaseId"] = "71"
    rows, _ = rb._graphql_rows("reviews", [node])
    assert rows[0]["id"] == 7
    assert rows[0]["top_level"] == ["a", "b"] and rows[0]["top_level_ids"] == [71, None]
    comment = {
        "fullDatabaseId": "9",
        "body": "x",
        "createdAt": AFTER,
        "author": {"login": "chatgpt-codex-connector", "__typename": "Bot"},
    }
    assert rb._graphql_rows("comments", [comment])[0][0]["id"] == 9


def test_the_reread_never_compares_ids():
    """A comment deleted and reposted between the two reads changes only its
    id; comparing ids would make that `unknown`, a path the count never had."""
    first = rb._graphql_rows("reviews", [_node(H1, tops=[_devin("🟡")])])[0]
    first[0].update(id=7, top_level_ids=[400])
    reposted = [dict(first[0], id=8, top_level_ids=[401])]
    assert rb._review_digest(first, rf) == rb._review_digest(reposted, rf)


def test_a_mixed_old_and_new_round_at_head_has_partial_keys():
    """A pre-cutover Codex P1 and a post-cutover Devin finding on one head: only
    Devin's is keyed, so the keys must not read as complete."""
    got = _ev(
        head=H2,
        reviews=(
            _keyed(_rv(CODEX, H2, P1, when=BEFORE), 10, [100]),
            _keyed(_rv(DEVIN, H2, _devin("🟡")), 40, [400]),
        ),
    )
    assert got["round_state"] == "open" and got["open_keys"] == ["c400"]
    assert got["reflection_keys"] == "unknown"


@pytest.mark.parametrize(
    "extra, reports",
    [
        (_rv(CODEX, H2, state="PENDING"), False),  # pending: not a report
        (_rv("a-maintainer", H2), False),  # a human never reports
        (_rv("github-actions[bot]", H2), False),  # nor a workflow bot
        (_rv(RABBIT, H2, when=BEFORE), True),  # before cutover still reports
    ],
)
def test_what_counts_as_reporting_on_the_head(extra, reports):
    got = _ev(head=H2, reviews=(extra,))
    assert (extra["login"] in got["reviewers_reported"]) is reports


def test_a_codex_findings_comment_reports_on_its_head():
    got = _ev(head=H5, comments=(dict(_cm(_bulb(H5)), id=30),))
    assert got["reviewers_reported"] == [CODEX]


def test_a_review_counted_twice_keys_its_findings_once():
    row = _keyed(_rv(DEVIN, H2, _devin("🟡")), 40, [400])
    got = _ev(head=H2, reviews=(row, dict(row)))
    assert got["open_keys"] == ["c400"]


def test_an_absurdly_long_id_is_none_not_a_crash():
    assert rb._node_id({"fullDatabaseId": "9" * 5000}) is None


def test_a_clean_pre_cutover_primary_review_at_head_makes_keys_unknown():
    """Accepted, fail-closed: the old rule never read findings, so a head the
    primary reviewed before the cutover cannot be told clean from unkeyed."""
    got = _ev(
        head=H2,
        reviews=(
            _keyed(_rv(CODEX, H2, when=BEFORE), 10, []),
            _keyed(_rv(DEVIN, H2, _devin("🟡")), 40, [400]),
        ),
    )
    assert got["open_keys"] == ["c400"] and got["reflection_keys"] == "unknown"


def test_a_clean_review_of_an_earlier_head_makes_its_reviewer_expected():
    """Deliberate: a reviewer that reviewed an earlier push, even clean, is
    expected again, though that head was never a round."""
    got = _ev(
        head=H2,
        reviews=(_rv(RABBIT, H1), _keyed(_rv(DEVIN, H2, _devin("🟡")), 40, [400])),
    )
    assert got["count"] == 1
    assert got["expected_reviewers"] == [RABBIT]


def test_a_huge_body_count_is_one_key_not_a_billion():
    """Round 1 of #3040: the count comes from reviewer text, so it must never
    size anything. One key per review carries it."""
    body = (
        "**Actionable comments posted: 0**\n<details>\n"
        "<summary>⚠️ Outside diff range comments (999999999)</summary>\n</details>"
    )
    got = _ev(reviews=(_keyed(_rv(RABBIT, H5, body=body), 20, []),))
    assert got["open_keys"] == ["r20:999999999"]


def test_a_changed_body_count_changes_the_key():
    def body(n):
        return (
            "**Actionable comments posted: 0**\n<details>\n"
            f"<summary>♻️ Duplicate comments ({n})</summary>\n</details>"
        )

    one = _ev(reviews=(_keyed(_rv(RABBIT, H5, body=body(1)), 20, []),))
    two = _ev(reviews=(_keyed(_rv(RABBIT, H5, body=body(2)), 20, []),))
    assert one["open_keys"] == ["r20:1"] and two["open_keys"] == ["r20:2"]


_TEMPLATE = "external report head={head}"
_GONE = "f" * 40  # a commit a force-push removed from the PR


@pytest.mark.parametrize(
    "branch, reviews, comments, templates, reporters",
    [
        ("review after cutover", (_rv(DEVIN, H2),), (), (), [DEVIN]),
        ("review before cutover", (_rv(DEVIN, H2, when=BEFORE),), (), (), [DEVIN]),
        ("primary review", (_rv(CODEX, H2),), (), (), [CODEX]),
        ("pending primary review", (_rv(CODEX, H2, state="PENDING"),), (), (), []),
        ("human review", (_rv("a-maintainer", H2),), (), (), []),
        ("workflow bot review", (_rv("github-actions[bot]", H2),), (), (), []),
        ("codeql review", (_rv("github-advanced-security[bot]", H2),), (), (), []),
        ("codex clean comment", (), (_clean(H2[:10]),), (), [CODEX]),
        (
            "human posting codex clean text",
            (),
            (_cm(_clean(H2[:10])["body"], login="a-person", kind="User"),),
            (),
            [],
        ),
        ("codex findings comment after cutover", (), (_cm(_bulb(H2)),), (), [CODEX]),
        ("codex findings comment before cutover", (), (_cm(_bulb(H2), when=BEFORE),), (), [CODEX]),
        (
            "findings comment citing two commits, before cutover",
            (),
            (_cm(_bulb(H2, H3), when=BEFORE),),
            (),
            [],
        ),
        (
            "another bot posting a codex-style findings comment",
            (),
            (_cm(_bulb(H2), login=DEVIN),),
            (),
            [],
        ),
        (
            "identity template",
            (),
            (_cm("external report head=" + H2, login="a-person", kind="User"),),
            (_TEMPLATE,),
            ["identity:0"],
        ),
        (
            "identity template, deleted author",
            (),
            (
                {
                    "login": None,
                    "type": None,
                    "body": "external report head=" + H2,
                    "created_at": AFTER,
                },
            ),
            (_TEMPLATE,),
            ["identity:0"],
        ),
        (
            "second identity template",
            (),
            (_cm("second report head=" + H2, login="a-person", kind="User"),),
            (_TEMPLATE, "second report head={head}"),
            ["identity:1"],
        ),
        (
            "identity on a removed commit after cutover",
            (),
            (_cm("external report head=" + _GONE, login="a-person", kind="User"),),
            (_TEMPLATE,),
            [],
        ),
    ],
)
def test_every_evidence_branch_reports_exactly_its_reviewer(
    branch, reviews, comments, templates, reporters
):
    """Class B of #3040's round 1: each branch that recognises a reviewer's
    evidence on a head reports exactly that reviewer, and nobody else."""
    got = _ev(head=H2, reviews=reviews, comments=comments, templates=templates)
    assert got["status"] == "ok", (branch, got["errors"])
    assert got["reviewers_reported"] == reporters, branch
    assert got["expected_reviewers"] == [], branch


def test_two_maximal_body_sections_never_crash_the_lookup():
    """Round 1 of #3040, fix-code audit: a sum of two 4,300-digit counts passes
    int() and overflows str(). The key goes unknown; the count is untouched."""
    nines = "9" * 4300
    body = (
        "**Actionable comments posted: 0**\n"
        f"<details>\n<summary>⚠️ Outside diff range comments ({nines})</summary>\n</details>\n"
        f"<details>\n<summary>♻️ Duplicate comments ({nines})</summary>\n</details>"
    )
    got = _ev(reviews=(_keyed(_rv(RABBIT, H5, body=body), 20, []),))
    assert got["status"] == "ok" and got["count"] == 1
    assert got["reflection_keys"] == "unknown" and got["open_keys"] == []
