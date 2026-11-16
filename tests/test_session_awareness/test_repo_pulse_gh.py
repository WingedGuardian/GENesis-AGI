"""repo_pulse_gh — merged-PR enumeration via injectable fake runners.

Locks the two DD-caught hardenings: live slug resolution (never config)
and the loud limit_hit flag on capped windows.
"""

from __future__ import annotations

import json

import pytest

from genesis.session_awareness import repo_pulse_gh as gh


def _fake_runner(responses: dict[str, tuple[int, str, str]]):
    """Runner keyed on the gh subcommand ('repo' / 'pr'). Records calls."""
    calls: list[list[str]] = []

    async def run(argv: list[str]) -> tuple[int, str, str]:
        calls.append(argv)
        return responses[argv[1]]

    run.calls = calls
    return run


def _pr(number, merged, title="t", body="b"):
    return {"number": number, "title": title, "body": body, "mergedAt": merged}


@pytest.mark.asyncio
async def test_resolves_slug_live_and_lists(monkeypatch):
    prs = [_pr(2, "2026-07-15T00:00:00Z"), _pr(1, "2026-07-14T00:00:00Z")]
    run = _fake_runner({"repo": (0, "owner/real-repo\n", ""), "pr": (0, json.dumps(prs), "")})
    out = await gh.list_merged_prs(since_date="2026-07-09", runner=run)
    assert out["repo"] == "owner/real-repo"
    assert out["limit_hit"] is False
    # sorted ascending by mergedAt for oldest-first cursor math
    assert [p["number"] for p in out["prs"]] == [1, 2]
    # the pr list call used the LIVE-resolved slug and the date search
    pr_argv = run.calls[1]
    assert pr_argv[: pr_argv.index("--repo") + 2][-1] == "owner/real-repo"
    assert "merged:>=2026-07-09" in " ".join(pr_argv)
    assert "--limit" in pr_argv and pr_argv[pr_argv.index("--limit") + 1] == "200"


@pytest.mark.asyncio
async def test_default_runner_spawn_failure_returns_error_tuple():
    """gh missing from PATH must surface as a nonzero runner result — a
    raised FileNotFoundError would bypass the error-dict path and leave no
    run row, no telemetry, no debounce (Codex P2 round 3 on #1081)."""
    rc, out, err = await gh._default_runner(["definitely-not-a-real-binary-xyz"])
    assert rc != 0
    assert out == ""
    assert "spawn failed" in err


@pytest.mark.asyncio
async def test_until_date_bounds_the_search_window():
    """Pagination support: an until_date turns the qualifier into a closed
    merged:since..until range (the worker pages down on limit_hit)."""
    run = _fake_runner({"pr": (0, "[]", "")})
    await gh.list_merged_prs(
        since_date="2026-07-09", until_date="2026-07-14", repo="o/r", runner=run
    )
    assert "merged:2026-07-09..2026-07-14" in " ".join(run.calls[0])


@pytest.mark.asyncio
async def test_fields_default_to_the_pulse_set():
    run = _fake_runner({"pr": (0, "[]", "")})
    await gh.list_merged_prs(since_date="2026-07-09", repo="o/r", runner=run)
    argv = run.calls[0]
    assert argv[argv.index("--json") + 1] == gh.PR_FIELDS


@pytest.mark.asyncio
async def test_a_caller_can_ask_for_other_fields():
    """The validation picker needs mergeCommit, which the pulse set lacks; it asks
    for it instead of widening every pulse call."""
    run = _fake_runner({"pr": (0, "[]", "")})
    await gh.list_merged_prs(
        since_date="2026-07-09", repo="o/r", fields="number,mergedAt,mergeCommit", runner=run
    )
    argv = run.calls[0]
    assert argv[argv.index("--json") + 1] == "number,mergedAt,mergeCommit"


@pytest.mark.asyncio
async def test_explicit_repo_skips_resolve():
    run = _fake_runner({"pr": (0, "[]", "")})
    out = await gh.list_merged_prs(since_date="2026-07-09", repo="o/r", runner=run)
    assert out == {"repo": "o/r", "prs": [], "limit_hit": False, "dropped": 0}
    assert len(run.calls) == 1  # no gh repo view call


@pytest.mark.asyncio
async def test_limit_hit_is_loud():
    prs = [_pr(i, f"2026-07-10T00:00:{i:02d}Z") for i in range(3)]
    run = _fake_runner({"pr": (0, json.dumps(prs), "")})
    out = await gh.list_merged_prs(since_date="2026-07-09", repo="o/r", limit=3, runner=run)
    assert out["limit_hit"] is True


@pytest.mark.asyncio
async def test_slug_resolve_failure_is_error_not_raise():
    run = _fake_runner({"repo": (1, "", "not a git repository")})
    out = await gh.list_merged_prs(since_date="2026-07-09", runner=run)
    assert out == {"error": "repo slug resolve failed"}


@pytest.mark.asyncio
async def test_pr_list_failures_are_errors_not_raise():
    for responses in (
        {"pr": (1, "", "HTTP 502")},
        {"pr": (0, "not json", "")},
        {"pr": (0, '{"a": 1}', "")},  # non-list payload
    ):
        run = _fake_runner(responses)
        out = await gh.list_merged_prs(since_date="2026-07-09", repo="o/r", runner=run)
        assert "error" in out


@pytest.mark.asyncio
async def test_malformed_pr_rows_dropped():
    raw = [
        _pr(1, "2026-07-14T00:00:00Z"),
        {"number": "2", "mergedAt": "2026-07-15T00:00:00Z"},  # str number
        {"number": 3, "mergedAt": ""},  # empty mergedAt
        {"number": 4},  # missing mergedAt
        "not a dict",
    ]
    run = _fake_runner({"pr": (0, json.dumps(raw), "")})
    out = await gh.list_merged_prs(since_date="2026-07-09", repo="o/r", runner=run)
    assert [p["number"] for p in out["prs"]] == [1]
    # limit_hit reflects the RAW count — a capped window full of junk is
    # still a capped window
    assert out["limit_hit"] is False


# ── list_open_prs (session-manager PR-4c open-PR lane) ──────────────────────


def _open_pr(number, updated, **extra):
    d = {
        "number": number,
        "title": "t",
        "url": f"https://x/pull/{number}",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "updatedAt": updated,
        "createdAt": updated,
        "author": {"login": "human", "is_bot": False},
    }
    d.update(extra)
    return d


@pytest.mark.asyncio
async def test_list_open_prs_requests_open_state_and_fields():
    prs = [_open_pr(2, "2026-08-10T00:00:00Z"), _open_pr(1, "2026-08-01T00:00:00Z")]
    run = _fake_runner({"repo": (0, "owner/real-repo\n", ""), "pr": (0, json.dumps(prs), "")})
    out = await gh.list_open_prs(runner=run)
    assert out["repo"] == "owner/real-repo"
    assert out["limit_hit"] is False
    assert {p["number"] for p in out["prs"]} == {1, 2}
    pr_argv = run.calls[1]
    assert "--state" in pr_argv and pr_argv[pr_argv.index("--state") + 1] == "open"
    # Stalest-first: a capped window must keep the aged PRs (the lane's target),
    # never gh's default newest-first order that would drop them.
    assert "--search" in pr_argv and pr_argv[pr_argv.index("--search") + 1] == "sort:updated-asc"
    joined = " ".join(pr_argv)
    assert "updatedAt" in joined and "author" in joined and "isDraft" in joined
    # age-based ONLY: never queries CI/review state
    assert "statusCheckRollup" not in joined and "reviewDecision" not in joined


@pytest.mark.asyncio
async def test_list_open_prs_explicit_repo_skips_resolve():
    run = _fake_runner({"pr": (0, "[]", "")})
    out = await gh.list_open_prs(repo="o/r", runner=run)
    assert out == {"repo": "o/r", "prs": [], "limit_hit": False}
    assert len(run.calls) == 1


@pytest.mark.asyncio
async def test_list_open_prs_failures_are_errors_not_raise():
    for responses in (
        {"pr": (1, "", "HTTP 502")},
        {"pr": (0, "not json", "")},
        {"pr": (0, '{"a": 1}', "")},
    ):
        run = _fake_runner(responses)
        out = await gh.list_open_prs(repo="o/r", runner=run)
        assert "error" in out


@pytest.mark.asyncio
async def test_list_open_prs_slug_resolve_failure_is_error():
    run = _fake_runner({"repo": (1, "", "not a git repo")})
    out = await gh.list_open_prs(runner=run)
    assert out == {"error": "repo slug resolve failed"}


@pytest.mark.asyncio
async def test_list_open_prs_malformed_rows_dropped():
    raw = [
        _open_pr(1, "2026-08-01T00:00:00Z"),
        {"number": "2", "updatedAt": "2026-08-02T00:00:00Z"},  # str number
        "not a dict",
    ]
    run = _fake_runner({"pr": (0, json.dumps(raw), "")})
    out = await gh.list_open_prs(repo="o/r", runner=run)
    assert [p["number"] for p in out["prs"]] == [1]


@pytest.mark.asyncio
async def test_list_open_prs_limit_hit_is_loud():
    prs = [_open_pr(i, "2026-08-01T00:00:00Z") for i in range(3)]
    run = _fake_runner({"pr": (0, json.dumps(prs), "")})
    out = await gh.list_open_prs(repo="o/r", limit=3, runner=run)
    assert out["limit_hit"] is True


def _queue_page(total, nodes, *, more=False, cursor=None, default="main"):
    return json.dumps({"data": {"repository": {
        "url": "https://github.com/o/r",
        "defaultBranchRef": {"name": default},
        "pullRequests": {
            "totalCount": total,
            "pageInfo": {"hasNextPage": more, "endCursor": cursor},
            "nodes": nodes,
        },
    }}})


def _queue_pr(number, title, *, draft=False, labels=None):
    labels = labels or []
    return {
        "number": number,
        "title": title,
        "isDraft": draft,
        "labels": {
            "totalCount": len(labels),
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": labels,
        },
    }


@pytest.mark.asyncio
async def test_queue_listing_paginates_graphql_and_keeps_required_fields():
    first = _queue_pr(1, "one")
    second = _queue_pr(2, "two", draft=True)
    calls = []
    responses = [
        (0, _queue_page(2, [first], more=True, cursor="cursor-1"), ""),
        (0, _queue_page(2, [second]), ""),
    ]

    async def run(argv):
        calls.append(argv)
        return responses.pop(0)

    out = await gh.list_queue_prs(repo="o/r", runner=run)

    assert out["repo"] == "o/r"
    assert out["default_branch"] == "main"
    assert out["open_total"] == out["read"] == 2
    assert out["limit_hit"] is False
    assert out["prs"] == [first, second]
    assert all(argv[:3] == ["gh", "api", "graphql"] for argv in calls)
    assert not any("--search" in argv for argv in calls)
    assert any("after=cursor-1" in argv for argv in calls[1])


@pytest.mark.asyncio
async def test_queue_listing_reports_cursor_failure_without_claiming_complete():
    first = {"number": 1, "title": "one"}
    responses = [
        (0, _queue_page(2, [first], more=True, cursor="cursor-1"), ""),
        (1, "", "cursor request failed"),
    ]

    async def run(argv):
        return responses.pop(0)

    out = await gh.list_queue_prs(repo="o/r", runner=run)

    assert out["limit_hit"] is True
    assert out["read"] == 1
    assert out["open_total"] == 2
    assert "cursor request failed" in out["error"]


@pytest.mark.asyncio
async def test_queue_listing_preserves_malformed_numbered_rows():
    malformed = _queue_pr(7, None)
    run = _fake_runner({"api": (0, _queue_page(1, [malformed]), "")})

    out = await gh.list_queue_prs(repo="o/r", runner=run)

    assert out["prs"] == [malformed]
    assert out["read"] == 1


@pytest.mark.asyncio
async def test_queue_listing_marks_total_count_mismatch():
    one = _queue_pr(1, "one")
    run = _fake_runner({"api": (0, _queue_page(3, [one]), "")})

    out = await gh.list_queue_prs(repo="o/r", runner=run)

    assert out["limit_hit"] is True
    assert out["open_total"] == 3
    assert out["read"] == 1


@pytest.mark.asyncio
async def test_queue_listing_paginates_labels_when_the_connection_is_saturated():
    first = _queue_pr(4, "labels", labels=[{"name": "first"}])
    first["labels"].update(
        totalCount=2, pageInfo={"hasNextPage": True, "endCursor": "label-cursor"}
    )
    second_page = json.dumps({"data": {"repository": {"pullRequest": {
        "labels": {
            "totalCount": 2,
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"name": "second"}],
        },
    }}}})
    responses = [(0, _queue_page(1, [first]), ""), (0, second_page, "")]

    async def run(argv):
        return responses.pop(0)

    out = await gh.list_queue_prs(repo="o/r", runner=run)

    assert [label["name"] for label in out["prs"][0]["labels"]["nodes"]] == [
        "first", "second",
    ]
    assert out["prs"][0].get("_queue_error") is None


def _comment_page(total, nodes, *, more=False, cursor=None):
    return json.dumps({"data": {"repository": {"pullRequest": {
        "comments": {
            "totalCount": total,
            "pageInfo": {"hasNextPage": more, "endCursor": cursor},
            "nodes": nodes,
        },
    }}}})


@pytest.mark.asyncio
async def test_queue_comments_read_all_pages_and_surface_cursor_failure():
    comments = [{"body": "one", "authorAssociation": "OWNER", "createdAt": "now"}]
    responses = [
        (0, _comment_page(2, comments, more=True, cursor="comment-cursor"), ""),
        (0, _comment_page(2, comments), ""),
    ]

    async def run(argv):
        return responses.pop(0)

    out = await gh.list_queue_pr_comments("o/r", 8, runner=run)

    assert out["total"] == out["read"] == 2
    assert out["limit_hit"] is False

    responses = [
        (0, _comment_page(2, comments, more=True, cursor="comment-cursor"), ""),
        (1, "", "comment page failed"),
    ]

    async def failing(argv):
        return responses.pop(0)

    failed = await gh.list_queue_pr_comments("o/r", 8, runner=failing)
    assert failed["limit_hit"] is True
    assert failed["read"] == 1
    assert "comment page failed" in failed["error"]


@pytest.mark.asyncio
async def test_queue_mergeability_reread_uses_shared_runner():
    run = _fake_runner({"pr": (0, "CONFLICTING\n", "")})

    out = await gh.read_queue_pr_mergeability("o/r", 9, runner=run)

    assert out == {"mergeable": "CONFLICTING"}
    assert run.calls[0][:3] == ["gh", "pr", "view"]
