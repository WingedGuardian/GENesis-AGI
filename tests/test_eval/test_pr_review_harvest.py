"""Tests for eval/pr_review_harvest.py — merged-PR review-finding harvest.

All gh traffic goes through the injectable ``runner`` seam; the fake below
dispatches on argv shape and records every call so tests can assert the
exact CLI surface (incl. that inline comments come from the ``/comments``
API endpoint, NOT ``gh pr view``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from genesis.eval.pr_review_harvest import (
    harvest_pr_review_findings,
    parse_severity,
)

# ── fake runner ──────────────────────────────────────────────────────────────


def _fake_runner(
    *,
    repo_rc: int = 0,
    pr_list: list[dict] | None = None,
    pr_list_rc: int = 0,
    comments: dict[int, list[dict]] | None = None,
    reviews: dict[int, list[dict]] | None = None,
    fail_comments_for: tuple[int, ...] = (),
):
    """Return an async runner dispatching on argv, with a ``.calls`` log."""
    comments = comments or {}
    reviews = reviews or {}
    calls: list[list[str]] = []

    async def runner(argv: list[str]) -> tuple[int, str, str]:
        calls.append(list(argv))
        if argv[:3] == ["gh", "repo", "view"]:
            if repo_rc != 0:
                return repo_rc, "", "gh: not a repo"
            return 0, "acme/widget\n", ""
        if argv[:3] == ["gh", "pr", "list"]:
            if pr_list_rc != 0:
                return pr_list_rc, "", "gh: search failed"
            return 0, json.dumps(pr_list or []), ""
        if argv[:2] == ["gh", "api"] and argv[2].endswith("/comments"):
            number = int(argv[2].rstrip("/").split("/")[-2])
            if number in fail_comments_for:
                return 1, "", "HTTP 502"
            return 0, json.dumps(comments.get(number, [])), ""
        if argv[:2] == ["gh", "api"] and argv[2].endswith("/reviews"):
            number = int(argv[2].rstrip("/").split("/")[-2])
            return 0, json.dumps(reviews.get(number, [])), ""
        raise AssertionError(f"unexpected argv: {argv}")

    runner.calls = calls
    return runner


def _comment(body: str, *, author: str = "codex-bot", path: str = "src/a.py") -> dict:
    return {"body": body, "user": {"login": author}, "path": path}


async def _one_pr_observation(db, number: int, repo: str = "acme/widget") -> dict:
    cursor = await db.execute(
        "SELECT * FROM observations WHERE id = ?",
        (f"prrev-{repo.replace('/', '-')}-{number}",),
    )
    rows = await cursor.fetchall()
    assert len(rows) == 1
    return dict(rows[0])


# ── severity parsing ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("P1: this races under load", "blocker"),
        ("[P2] consider bounding the retry loop", "should_fix"),
        ("p3 nit about naming", "note"),
        ("BLOCKER: writes to the shared connection", "blocker"),
        ("This is a Should-Fix in my opinion", "should_fix"),
        ("NOTE: docstring drift", "note"),
        ("looks good to me", "unlabeled"),
        ("", "unlabeled"),
        # P4 is not in the vocabulary — never guessed into a bucket
        ("P4 hypothetical severity", "unlabeled"),
    ],
)
def test_parse_severity_matrix(body, expected):
    assert parse_severity(body) == expected


# ── harvest behavior ─────────────────────────────────────────────────────────


async def test_harvest_writes_findings_and_review_count(db):
    """Inline comments AND the /reviews count are merged into one row per PR."""
    merged_at = datetime.now(UTC).isoformat()
    runner = _fake_runner(
        pr_list=[{"number": 101, "mergedAt": merged_at, "title": "fix: races"}],
        comments={101: [
            _comment("P1: race on shutdown"),
            _comment("[P2] tighten the timeout"),
            _comment("just curious — why 30s?", author="human"),
        ]},
        reviews={101: [{"state": "COMMENTED"}, {"state": "APPROVED"}]},
    )

    summary = await harvest_pr_review_findings(db, runner=runner)

    assert summary["repo"] == "acme/widget"
    assert summary["prs_seen"] == 1
    assert summary["findings_total"] == 3
    assert summary["by_severity"] == {
        "blocker": 1, "should_fix": 1, "note": 0, "unlabeled": 1,
    }
    assert summary["errors"] == []

    obs = await _one_pr_observation(db, 101)
    assert obs["source"] == "recon"
    assert obs["category"] == "pr_review_findings"
    assert obs["type"] == "pr_review_findings"
    content = json.loads(obs["content"])
    assert content["pr"] == 101
    assert content["title"] == "fix: races"
    assert content["merged_at"] == merged_at
    assert content["review_count"] == 2
    severities = [f["severity"] for f in content["findings"]]
    assert severities == ["blocker", "should_fix", "unlabeled"]
    assert content["findings"][0]["author"] == "codex-bot"
    assert content["findings"][0]["path"] == "src/a.py"
    assert all(len(f["excerpt"]) <= 120 for f in content["findings"])


async def test_harvest_argv_shapes(db):
    """The gh surface is exact: pulls/N/comments --paginate (NOT gh pr view),
    a merged:>= search on pr list, and the caller-supplied limit."""
    runner = _fake_runner(
        pr_list=[{"number": 7, "mergedAt": datetime.now(UTC).isoformat(), "title": "t"}],
    )
    await harvest_pr_review_findings(db, lookback_days=14, limit=25, runner=runner)

    repo_call, list_call, comments_call, reviews_call = runner.calls
    assert repo_call[:3] == ["gh", "repo", "view"]
    assert "nameWithOwner" in " ".join(repo_call)

    assert list_call[:3] == ["gh", "pr", "list"]
    assert list_call[list_call.index("--state"):][:2] == ["--state", "merged"]
    expected_since = (datetime.now(UTC) - timedelta(days=14)).date().isoformat()
    assert f"merged:>={expected_since}" in list_call
    assert list_call[list_call.index("--limit"):][:2] == ["--limit", "25"]

    # The production lesson, pinned: findings come from the inline review
    # comments endpoint, which `gh pr view --json reviews,comments` misses.
    assert comments_call[:2] == ["gh", "api"]
    assert comments_call[2] == "repos/acme/widget/pulls/7/comments"
    assert "--paginate" in comments_call
    assert reviews_call[2] == "repos/acme/widget/pulls/7/reviews"


async def test_harvest_explicit_repo_skips_resolve(db):
    runner = _fake_runner(pr_list=[])
    summary = await harvest_pr_review_findings(db, repo="me/mine", runner=runner)
    assert summary["repo"] == "me/mine"
    assert all(c[:3] != ["gh", "repo", "view"] for c in runner.calls)
    assert "--repo" in runner.calls[0]


async def test_double_harvest_single_row_per_pr(db):
    """Deterministic prrev-<n> id → re-harvest updates in place, no dupes."""
    merged_at = datetime.now(UTC).isoformat()
    runner = _fake_runner(
        pr_list=[{"number": 5, "mergedAt": merged_at, "title": "t"}],
        comments={5: [_comment("P1: bad")]},
    )
    await harvest_pr_review_findings(db, runner=runner)
    await harvest_pr_review_findings(db, runner=runner)

    cursor = await db.execute(
        "SELECT COUNT(*) FROM observations WHERE category = 'pr_review_findings'",
    )
    assert (await cursor.fetchone())[0] == 1
    await _one_pr_observation(db, 5)  # asserts exactly one row


async def test_harvest_sets_expires_at_90d(db):
    runner = _fake_runner(
        pr_list=[{"number": 9, "mergedAt": datetime.now(UTC).isoformat(), "title": "t"}],
    )
    await harvest_pr_review_findings(db, runner=runner)

    obs = await _one_pr_observation(db, 9)
    expires = datetime.fromisoformat(obs["expires_at"])
    delta = expires - datetime.now(UTC)
    assert timedelta(days=89) < delta < timedelta(days=91)


async def test_per_pr_failure_skips_but_harvest_continues(db):
    """One bad PR must not kill the harvest: it lands in errors, the rest
    are still written."""
    merged_at = datetime.now(UTC).isoformat()
    runner = _fake_runner(
        pr_list=[
            {"number": 101, "mergedAt": merged_at, "title": "good"},
            {"number": 102, "mergedAt": merged_at, "title": "bad"},
            {"number": 103, "mergedAt": merged_at, "title": "also good"},
        ],
        comments={101: [_comment("P2: hmm")], 103: []},
        fail_comments_for=(102,),
    )
    summary = await harvest_pr_review_findings(db, runner=runner)

    assert summary["prs_seen"] == 3
    assert len(summary["errors"]) == 1
    assert "102" in summary["errors"][0]
    assert summary["findings_total"] == 1

    await _one_pr_observation(db, 101)
    await _one_pr_observation(db, 103)
    cursor = await db.execute(
        "SELECT COUNT(*) FROM observations WHERE id LIKE 'prrev-%-102'",
    )
    assert (await cursor.fetchone())[0] == 0


async def test_repo_resolve_failure_returns_error_dict(db):
    runner = _fake_runner(repo_rc=1)
    summary = await harvest_pr_review_findings(db, runner=runner)
    assert "error" in summary
    assert "prs_seen" not in summary


async def test_pr_list_failure_returns_error_dict(db):
    runner = _fake_runner(pr_list_rc=1)
    summary = await harvest_pr_review_findings(db, runner=runner)
    assert "error" in summary


async def test_paginated_concatenated_arrays_are_parsed(db):
    """``gh api --paginate`` emits one JSON array per page back to back —
    the harvest must parse the concatenation, not choke on it."""
    merged_at = datetime.now(UTC).isoformat()
    page1 = json.dumps([_comment("P1: a")])
    page2 = json.dumps([_comment("P3: b"), _comment("NOTE: c")])

    async def runner(argv: list[str]) -> tuple[int, str, str]:
        if argv[:3] == ["gh", "repo", "view"]:
            return 0, "acme/widget", ""
        if argv[:3] == ["gh", "pr", "list"]:
            return 0, json.dumps(
                [{"number": 1, "mergedAt": merged_at, "title": "t"}],
            ), ""
        if argv[2].endswith("/comments"):
            return 0, page1 + "\n" + page2, ""
        return 0, "[]", ""

    summary = await harvest_pr_review_findings(db, runner=runner)
    assert summary["findings_total"] == 3
    assert summary["by_severity"]["blocker"] == 1
    assert summary["by_severity"]["note"] == 2
