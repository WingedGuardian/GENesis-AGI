"""The ``--check-pr`` ``delivery`` row: whether a delivery read was recorded at head.

The row reads the owner's marker through ``delivery_marker``'s own parser and
``select``, so every case drives the REAL marker module and fakes only its one
``gh`` call, plus the guard's own seams (the PR file list, the base and the carry
helper). Nothing here touches GitHub.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

REPO = "o/r"
HEAD = "a" * 40
OLD = "b" * 40
BASE = "c" * 40
STAMP = "2026-11-17T10:00:00Z"
EARLIER = "2026-11-17T09:00:00Z"
LATER = "2026-11-17T11:00:00Z"
BODY = "Closes #7\n\nThe change."


@pytest.fixture(scope="module")
def guard():
    spec = importlib.util.spec_from_file_location(
        "git_push_guard_delivery_row", _ROOT / "scripts" / "hooks" / "git_push_guard.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def block(
    head=HEAD,
    verdict="DELIVERS",
    scope="CLEAN",
    checker="other",
    basis="issue:7",
    intended="read",
    prose="- DONE: the fix `a.py:1`",
    aside=False,
):
    extra = f" intended={intended}" if basis.startswith("issue:") else ""
    text = (
        f"<!-- genesis-delivery-check: head={head} basis={basis} verdict={verdict}"
        f" scope={scope}{extra} checker={checker} -->\n{prose}"
    )
    return ("(aside)\n" + text) if aside else text


def comment(body, cid=1, stamp=STAMP, login="o", assoc="OWNER"):
    return {"id": cid, "login": login, "author_association": assoc, "body": body, "stamp": stamp}


def issue(edited=None, comments=(), events=(), total=None, filtered=None):
    """The basis issue as the GraphQL read returns it."""
    comments, events = list(comments), list(events)
    return {
        "__typename": "Issue",
        "lastEditedAt": edited,
        "comments": {"totalCount": len(comments) if total is None else total, "nodes": comments},
        "timelineItems": {
            "filteredCount": len(events) if filtered is None else filtered,
            "nodes": events,
        },
    }


class World:
    """What the fake ``gh`` returns, per read. A value of ``"error"`` makes that read fail."""

    def __init__(self):
        self.comments = []
        self.reviews = []
        self.pr_body = BODY
        self.linked = []
        self.base_events = []
        self.base_filtered = None
        self.issue = issue()
        self.pr_renames = []  # PR retitle times
        self.graphql = None  # "error" to fail the GraphQL read
        self.head_time = STAMP  # the read's head commit time; None: the commit is unknown
        self.pr_edited = None  # the PR body's lastEditedAt
        self.calls = []

    def gh(self, args, *, timeout=None):
        import delivery_marker as dm

        self.calls.append((args, timeout))
        joined = " ".join(args)
        if args[:2] == ["api", "graphql"]:
            if self.graphql == "error":
                raise dm.Refused("gh api graphql failed: boom")
            events = [
                {"__typename": "BaseRefChangedEvent", "createdAt": t} for t in self.base_events
            ]
            events += [{"__typename": "RenamedTitleEvent", "createdAt": t} for t in self.pr_renames]
            filtered = len(events) if self.base_filtered is None else self.base_filtered
            return json.dumps(
                {
                    "data": {
                        "repository": {
                            "object": (
                                None
                                if self.head_time is None
                                else {"committedDate": self.head_time}
                            ),
                            "pullRequest": {
                                "lastEditedAt": self.pr_edited,
                                "timelineItems": {
                                    "filteredCount": filtered,
                                    "nodes": events,
                                },
                            },
                            "issueOrPullRequest": self.issue,
                        }
                    }
                }
            )
        if "/reviews" in joined:
            rows = self.reviews
        elif "/comments" in joined:
            rows = self.comments
        elif args[:2] == ["pr", "view"]:
            return "".join(json.dumps(r) + "\n" for r in self.linked)
        elif args[0] == "api" and "/pulls/" in joined:
            if self.pr_body == "error":
                raise dm.Refused("gh api failed: boom")
            return json.dumps({"head": HEAD, "body": self.pr_body})
        else:
            raise AssertionError(f"unexpected gh call: {args}")
        if rows == "error":
            raise dm.Refused("gh api failed: boom")
        return "".join(json.dumps(r) + "\n" for r in rows)


@pytest.fixture
def world(guard, monkeypatch):
    dm = importlib.import_module("delivery_marker")
    w = World()
    monkeypatch.setattr(dm, "_gh", w.gh)
    monkeypatch.delenv("_TEST_DELIVERY_ROW", raising=False)
    monkeypatch.setattr(guard, "_scheduled_gate_applies", lambda repo: True)
    monkeypatch.setattr(guard, "_pr_base_sha", lambda pr, repo=None: BASE)
    w.files = ["scripts/thing.py"]
    monkeypatch.setattr(guard, "_pr_changed_files", lambda pr, repo=None: w.files)
    w.carry = None
    w.carry_args = []

    def carry(reviewed, head, base, repo):
        w.carry_args.append((reviewed, head, base))
        return w.carry

    monkeypatch.setattr(guard, "_classify_base_advance_delta", carry)
    return w


def row(guard):
    return guard._delivery_row("12", REPO, HEAD)


# ── the writer-state table ───────────────────────────────────────────────────


def test_no_marker_is_advisory_and_names_the_compose_command(guard, world):
    got = row(guard)
    assert got.startswith("advisory — no delivery check recorded"), got
    assert "delivery_marker.py compose --pr 12" in got


def test_delivers_at_head_by_another_reader_is_ok(guard, world):
    world.comments = [comment(block())]
    got = row(guard)
    assert got.startswith("ok (DELIVERS at head"), got
    assert "basis issue:7" in got


@pytest.mark.parametrize("scope", ["NEEDED", "DRIFT"])
def test_a_scope_other_than_clean_is_named(guard, world, scope):
    world.comments = [comment(block(scope=scope))]
    got = row(guard)
    assert got.startswith("ok ("), got
    assert f"scope {scope}" in got


def test_builders_own_read_on_a_prose_only_pr_is_self_attested(guard, world):
    world.files = ["docs/guide.md", "CONTRIBUTING.md"]
    world.comments = [comment(block(checker="author-review"))]
    got = row(guard)
    assert got.startswith("ok (DELIVERS at head"), got
    assert "self-attested" in got


@pytest.mark.parametrize(
    "files",
    [
        ["tests/test_x.py"],
        ["config/mcp_staleness_guard.yaml"],
        ["src/genesis/identity/SOUL.md"],
        [".claude/agents/genesis-architect.md"],
        ["docs/guide.md", "scripts/thing.py"],
        [".github/README.md"],  # prose, but a critical-lane path
        ["scripts/hooks/README.md"],  # prose, but on the enforcement-hook surface
        [],
        None,
    ],
)
def test_builders_own_read_elsewhere_is_advisory(guard, world, files):
    world.files = files
    world.comments = [comment(block(checker="author-review"))]
    got = row(guard)
    assert got.startswith("advisory — the builder's own read"), got


@pytest.mark.parametrize("verdict", ["PARTIAL", "DOES-NOT", "UNCLEAR"])
def test_a_verdict_other_than_delivers_is_advisory_with_the_asked_line(guard, world, verdict):
    prose = 'Asked: "pin the base with the merge base"\n- NOT DONE: x'
    world.comments = [comment(block(verdict=verdict, prose=prose))]
    got = row(guard)
    assert got.startswith(f"advisory — {verdict} at head"), got
    assert "pin the base with the merge base" in got


def test_an_older_head_whose_own_change_is_unchanged_is_carried(guard, world):
    world.comments = [comment(block(head=OLD))]
    world.carry = "inline"
    got = row(guard)
    assert got.startswith("ok (DELIVERS carried from bbbbbbbbbbbb: own change unchanged"), got
    assert world.carry_args == [(OLD, HEAD, BASE)]


def test_an_older_head_the_helper_cannot_confirm_is_never_carried(guard, world):
    world.comments = [comment(block(head=OLD))]
    world.carry = None  # also what the helper returns for a hook-surface PR
    got = row(guard)
    assert got.startswith("advisory — check recorded at bbbbbbbbbbbb; carry unconfirmed"), got
    assert "differs" not in got


def test_a_base_change_after_the_check_is_advisory(guard, world):
    world.comments = [comment(block())]
    world.base_events = [EARLIER, LATER]
    got = row(guard)
    assert got.startswith("advisory — DELIVERS at head"), got
    assert "base changed since the check" in got


def test_a_base_change_before_the_check_is_not_reported(guard, world):
    world.comments = [comment(block())]
    world.base_events = [EARLIER]
    assert row(guard).startswith("ok (DELIVERS at head")


def test_a_base_event_list_cut_short_is_could_not_check(guard, world):
    world.comments = [comment(block())]
    world.base_events = [EARLIER]
    world.base_filtered = 200  # more events than the read returned
    got = row(guard)
    assert "base changes: could not check" in got
    assert not got.startswith("ok")


def test_an_issue_edit_after_the_check_is_advisory(guard, world):
    world.comments = [comment(block())]
    world.issue = issue(edited=LATER)
    got = row(guard)
    assert got.startswith("advisory —"), got
    assert "the issue was edited since the check" in got


def test_a_new_issue_comment_after_the_check_is_advisory(guard, world):
    world.comments = [comment(block())]
    world.issue = issue(edited=EARLIER, comments=[{"createdAt": LATER}])
    got = row(guard)
    assert got.startswith("advisory —"), got
    assert "the issue has new comments since the check" in got


def test_changes_are_judged_against_the_heads_commit_time_not_the_marker_edit(guard, world):
    """Owner ruling 2026-11-18: a re-edited marker (a carry re-recorded) must not hide
    an issue edit made after the read's head was committed."""
    world.comments = [comment(block(head=OLD), stamp=LATER)]  # marker last edited late
    world.carry = "inline"
    world.head_time = EARLIER  # the read's head was committed early
    world.issue = issue(edited=STAMP)
    got = row(guard)
    assert "the issue was edited since the check" in got, got
    graphql = [args for args, _t in world.calls if args[:2] == ["api", "graphql"]]
    assert graphql and f"head={OLD}" in graphql[0], graphql


def test_an_unknown_head_commit_is_could_not_check(guard, world):
    world.comments = [comment(block())]
    world.head_time = None
    got = row(guard)
    assert "base and issue changes: could not check" in got
    assert not got.startswith("ok")


def test_a_missing_issue_is_reported_not_skipped(guard, world):
    world.comments = [comment(block())]
    world.issue = None
    got = row(guard)
    assert "issue #7 unreadable" in got
    assert not got.startswith("ok")


def test_a_failed_graphql_read_is_named_and_never_ok(guard, world):
    world.comments = [comment(block())]
    world.graphql = "error"
    got = row(guard)
    assert "base and issue changes: could not check" in got
    assert not got.startswith("ok")


def test_a_claim_basis_reads_no_issue(guard, world):
    world.pr_body = "The change, no pointer."
    world.comments = [comment(block(basis="claim"))]
    got = row(guard)
    assert got.startswith("ok (DELIVERS at head, basis claim"), got
    assert "the issue" not in got  # no issue read on a claim basis
    assert "basis is the PR's own claim, not an issue, spec or plan" in got


@pytest.mark.parametrize(
    "basis, named",
    [
        ("ledger:" + "7" * 32, "a ledger row"),
        ("follow-up:" + "8" * 32, "a follow-up"),
    ],
)
def test_a_weak_basis_is_named_and_stays_ok(guard, world, basis, named):
    """Owner ruling 2026-11-18: name it, do not demote it."""
    world.pr_body = (
        f"Ledger: {'7' * 32}\n" if basis.startswith("ledger") else f"Follow-up: {'8' * 32}\n"
    )
    world.comments = [comment(block(basis=basis))]
    got = row(guard)
    assert got.startswith("ok (DELIVERS at head"), got
    assert f"basis is {named}, not an issue, spec or plan" in got


def test_a_huge_basis_is_bounded_in_the_row(guard, world):
    """The grammar fixes the basis's shape, not its length (GLM, round 1)."""
    name = "A" * 50_000
    world.pr_body = f"Spec: {name}\n"
    world.comments = [comment(block(basis=f"spec:{name}"))]
    got = row(guard)
    assert len(got) < 1_000, len(got)


def test_an_edited_claim_is_advisory(guard, world):
    """On a claim basis the PR body IS the basis: editing it after the read changes
    what was judged (Devin, round 1)."""
    world.pr_body = "The change, no pointer."
    world.comments = [comment(block(basis="claim"))]
    world.pr_edited = LATER
    got = row(guard)
    assert got.startswith("advisory —"), got
    assert "the PR body (its claim) was edited since the check" in got


def test_a_body_edit_on_an_issue_basis_is_not_a_claim_change(guard, world):
    world.comments = [comment(block())]
    world.pr_edited = LATER
    got = row(guard)
    assert got.startswith("ok (DELIVERS at head"), got
    assert "its claim" not in got


def test_an_issue_basis_is_not_called_weak(guard, world):
    world.comments = [comment(block())]
    assert "not an issue, spec or plan" not in row(guard)


def test_the_latest_edit_governs_at_one_head(guard, world):
    world.comments = [
        comment(block(verdict="PARTIAL"), cid=1, stamp=EARLIER),
        comment(block(), cid=2, stamp=STAMP),
    ]
    assert row(guard).startswith("ok (DELIVERS at head")


def test_the_block_at_the_current_head_governs_over_a_later_edit_at_an_old_one(guard, world):
    world.comments = [
        comment(block(), cid=1, stamp=EARLIER),
        comment(block(head=OLD, verdict="PARTIAL"), cid=2, stamp=LATER),
    ]
    assert row(guard).startswith("ok (DELIVERS at head")


def test_a_review_body_block_is_ignored(guard, world):
    world.reviews = [comment(block())]
    got = row(guard)
    assert got.startswith("advisory — no delivery check recorded"), got
    assert "review body" in got


def test_a_non_owner_block_is_ignored_and_the_owner_block_governs(guard, world):
    world.comments = [
        comment(block(verdict="PARTIAL"), cid=1, stamp=LATER, login="x", assoc="NONE"),
        comment(block(), cid=2, stamp=STAMP),
    ]
    got = row(guard)
    assert got.startswith("ok (DELIVERS at head"), got
    assert "1 ignored" in got


def test_a_malformed_owner_block_is_reported_with_its_problem(guard, world):
    world.comments = [comment("<!-- genesis-delivery-check: head=zz basis=claim -->")]
    got = row(guard)
    assert got.startswith("advisory — no usable owner delivery check recorded"), got
    assert "'head=zz' is not a valid head" in got


def test_a_later_unusable_owner_block_is_never_ok(guard, world):
    world.comments = [
        comment(block(), cid=1, stamp=STAMP),
        comment("<!-- genesis-delivery-check: head=zz basis=claim -->", cid=2, stamp=LATER),
    ]
    got = row(guard)
    assert got.startswith("advisory — DELIVERS at head"), got
    assert "a later owner block is unusable" in got


def test_an_aside_marker_is_read(guard, world):
    world.comments = [comment(block(aside=True))]
    assert row(guard).startswith("ok (DELIVERS at head")


@pytest.mark.parametrize("intended", ["not-stated", "unsupported"])
def test_an_unstated_intent_is_printed(guard, world, intended):
    world.comments = [comment(block(intended=intended))]
    got = row(guard)
    assert f"intended={intended}" in got
    assert not got.startswith("ok")


def test_a_changed_basis_is_advisory(guard, world):
    world.pr_body = "Closes #8\n"
    world.comments = [comment(block())]
    got = row(guard)
    assert got.startswith("advisory —"), got
    assert "basis changed since the check (now issue:8)" in got


def test_a_basis_that_cannot_be_derived_now_is_advisory(guard, world):
    world.pr_body = "The change, see Closes #8 mid sentence."
    world.comments = [comment(block())]
    got = row(guard)
    assert got.startswith("advisory —"), got
    assert "the PR's basis cannot be derived now" in got


def test_unreadable_comments_are_could_not_check(guard, world):
    world.comments = "error"
    assert row(guard).startswith("could not check")


def test_any_exception_is_could_not_check(guard, world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(guard, "_pr_base_sha", boom)
    world.comments = [comment(block(head=OLD))]
    assert row(guard).startswith("could not check")


def test_every_gh_read_is_bounded(guard, world):
    world.comments = [comment(block())]
    row(guard)
    assert world.calls and all(t is not None for _args, t in world.calls), world.calls


def test_another_repository_is_not_applicable(guard, world, monkeypatch):
    monkeypatch.setattr(guard, "_scheduled_gate_applies", lambda repo: False)
    assert row(guard) == "n/a (scoped to the public repo only)"
    assert world.calls == []


def test_the_test_seam_short_circuits(guard, world, monkeypatch):
    monkeypatch.setenv("_TEST_DELIVERY_ROW", "pinned")
    assert row(guard) == "pinned"
    assert world.calls == []


def test_github_text_in_the_row_is_sanitized(guard, world):
    prose = 'Asked: "evil\x1b[2Jtext"\n- NOT DONE: x'
    world.comments = [comment(block(verdict="PARTIAL", prose=prose))]
    got = row(guard)
    assert "evil" in got and "text" in got  # the Asked line is shown
    assert "\x1b" not in got


def test_a_multi_line_gh_error_stays_one_report_line(guard, world, monkeypatch):
    import delivery_marker as dm

    def broken(*a, **k):
        raise dm.Refused("gh api failed: boom\nmerge-with     : gh pr merge 12 --admin")

    monkeypatch.setattr(dm, "_comments", broken)
    got = row(guard)
    assert got.startswith("could not check"), got
    assert "\n" not in got


def test_unreadable_review_bodies_are_named_and_never_ok(guard, world):
    world.comments = [comment(block())]
    world.reviews = "error"
    got = row(guard)
    assert got.startswith("advisory — DELIVERS at head"), got
    assert "review bodies: could not check" in got


def test_an_unreadable_pr_body_leaves_the_basis_unchecked_and_says_so(guard, world):
    world.comments = [comment(block())]
    world.pr_body = "error"
    got = row(guard)
    assert got.startswith("advisory — DELIVERS at head"), got
    assert "the PR body: could not check" in got
    assert "basis changed" not in got


# ── round 1: what else may change after a read (owner rulings 2026-11-18) ─────


def test_a_claim_body_edit_before_the_read_is_not_flagged(guard, world):
    """On a claim basis the body is judged against the marker's own edit time, so a
    bot's body edit between the head commit and the read clears on a re-read."""
    world.pr_body = "The change, no pointer."
    world.comments = [comment(block(basis="claim"), stamp=LATER)]
    world.head_time = EARLIER
    world.pr_edited = STAMP  # after the commit, before the read
    got = row(guard)
    assert got.startswith("ok (DELIVERS at head"), got


def test_a_claim_pr_retitled_after_the_read_is_advisory(guard, world):
    world.pr_body = "The change, no pointer."
    world.comments = [comment(block(basis="claim"))]
    world.pr_renames = [LATER]
    got = row(guard)
    assert got.startswith("advisory —"), got
    assert "the PR title (part of its claim) changed since the check" in got


def test_a_pr_retitle_on_an_issue_basis_is_not_a_change(guard, world):
    world.comments = [comment(block())]
    world.pr_renames = [LATER]
    assert row(guard).startswith("ok (DELIVERS at head")


@pytest.mark.parametrize(
    "kind, body, shown",
    [
        ("spec:delivery-design", "Spec: delivery-design\n", "changes to the spec since the read"),
        ("plan:delivery-row", "Plan: delivery-row\n", "changes to the plan since the read"),
        ("ledger:" + "7" * 32, f"Ledger: {'7' * 32}\n", "changes to the ledger row since the read"),
        (
            "follow-up:" + "8" * 32,
            f"Follow-up: {'8' * 32}\n",
            "changes to the follow-up since the read",
        ),
    ],
)
def test_a_source_nothing_can_check_is_named_not_demoted(guard, world, kind, body, shown):
    world.pr_body = body
    world.comments = [comment(block(basis=kind))]
    got = row(guard)
    assert got.startswith("ok (DELIVERS at head"), got
    assert f"{shown}: not checked" in got


def test_an_edited_issue_comment_is_advisory(guard, world):
    world.comments = [comment(block())]
    world.issue = issue(comments=[{"createdAt": EARLIER, "lastEditedAt": LATER}])
    got = row(guard)
    assert got.startswith("advisory —"), got
    assert "an issue comment was edited since the check" in got


def test_older_issue_comments_beyond_the_page_are_named(guard, world):
    world.comments = [comment(block())]
    world.issue = issue(comments=[{"createdAt": EARLIER}], total=150)
    got = row(guard)
    assert "older issue comments: edits not checked" in got
    assert got.startswith("ok (DELIVERS at head"), got  # named, not voiding


@pytest.mark.parametrize(
    "event, shown",
    [
        ({"__typename": "RenamedTitleEvent", "createdAt": LATER}, "the issue was retitled"),
        (
            {"__typename": "ClosedEvent", "createdAt": LATER, "stateReason": "NOT_PLANNED"},
            "the issue was closed as not planned",
        ),
        ({"__typename": "ReopenedEvent", "createdAt": LATER}, "the issue was reopened"),
    ],
)
def test_an_issue_retitle_or_closure_after_the_read_is_advisory(guard, world, event, shown):
    world.comments = [comment(block())]
    world.issue = issue(events=[event])
    got = row(guard)
    assert got.startswith("advisory —"), got
    assert f"{shown} since the check" in got


def test_an_issue_closure_before_the_read_is_not_flagged(guard, world):
    world.comments = [comment(block())]
    world.issue = issue(events=[{"__typename": "ClosedEvent", "createdAt": EARLIER}])
    assert row(guard).startswith("ok (DELIVERS at head")


def test_a_cut_short_issue_timeline_is_could_not_check(guard, world):
    world.comments = [comment(block())]
    world.issue = issue(
        events=[{"__typename": "RenamedTitleEvent", "createdAt": EARLIER}], filtered=200
    )
    got = row(guard)
    assert "issue retitles and closures: could not check" in got
    assert not got.startswith("ok")


def test_a_huge_issue_number_fails_closed_inside_the_read(guard, world):
    """The issue number is parsed inside the read's try, so no row turns into a
    'lookup failed' on a pathological basis."""
    import delivery_marker as dm

    got = dm.changes_since(REPO, 12, "issue:" + "9" * 5000, HEAD, STAMP, timeout=1)
    assert got == [("base and issue changes: could not check", True)]


def test_a_voided_builder_read_never_claims_self_attest(guard, world, monkeypatch):
    """An unstated intent already voids the row, so the self-attest branch must not
    run or print 'self-attested' on it (review of the round-1 fix, L2)."""
    world.files = ["docs/guide.md"]
    world.comments = [comment(block(checker="author-review", intended="not-stated"))]
    called = []
    monkeypatch.setattr(
        guard, "_pr_changed_files", lambda pr, repo=None: called.append(pr) or world.files
    )
    got = row(guard)
    assert got.startswith("advisory —"), got
    assert "self-attested" not in got
    assert called == []
