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
async def test_explicit_repo_skips_resolve():
    run = _fake_runner({"pr": (0, "[]", "")})
    out = await gh.list_merged_prs(since_date="2026-07-09", repo="o/r", runner=run)
    assert out == {"repo": "o/r", "prs": [], "limit_hit": False}
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
