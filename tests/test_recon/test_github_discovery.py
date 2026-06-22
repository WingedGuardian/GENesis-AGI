"""Tests for the GitHub Discovery ranking engine."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from genesis.recon.github_discovery import (
    DEFAULT_WEIGHTS,
    RepoCandidate,
    discover,
    score_activity,
    score_candidate,
    score_maturity,
    score_momentum,
    search_repos,
)

_NOW = datetime(2026, 6, 22, tzinfo=UTC)


def _iso(days_ago: int) -> str:
    """ISO timestamp `days_ago` before _NOW, with a Z suffix like gh returns."""
    from datetime import timedelta

    return (_NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── score_momentum ───────────────────────────────────────────────────────────


def test_momentum_young_fast_beats_old_slow():
    young = score_momentum(3000, _iso(60), _NOW)   # 50 stars/day
    old = score_momentum(3000, _iso(1500), _NOW)   # 2 stars/day
    assert young > old
    assert 0.0 <= old <= young <= 1.0


def test_momentum_floors_denominator_so_one_day_hype_does_not_max_out():
    # A 1-day-old repo should NOT score near-1 just because stars/age is huge:
    # denominator is floored at 30 days.
    hype = score_momentum(300, _iso(1), _NOW)
    assert hype < 0.9


def test_momentum_handles_missing_date():
    assert score_momentum(1000, "", _NOW) == 0.0


# ── score_activity ───────────────────────────────────────────────────────────


def test_activity_recent_beats_stale():
    recent = score_activity(_iso(1), _NOW)
    stale = score_activity(_iso(150), _NOW)
    assert recent > stale
    assert 0.0 <= stale <= recent <= 1.0


def test_activity_pushed_today_is_near_one():
    assert score_activity(_iso(0), _NOW) > 0.95


def test_activity_handles_missing_date():
    assert score_activity(None, _NOW) == 0.0


# ── score_maturity ───────────────────────────────────────────────────────────


def test_maturity_proven_beats_brand_new():
    mature = score_maturity(_iso(730), _NOW)   # 2 years
    brand_new = score_maturity(_iso(10), _NOW)  # 10 days
    assert mature > brand_new
    assert 0.0 <= brand_new <= mature <= 1.0


def test_maturity_brand_new_is_low():
    assert score_maturity(_iso(5), _NOW) < 0.1


# ── score_candidate ──────────────────────────────────────────────────────────


def _repo(full_name, stars, created_days, pushed_days):
    return {
        "fullName": full_name,
        "url": f"https://github.com/{full_name}",
        "stargazersCount": stars,
        "createdAt": _iso(created_days),
        "pushedAt": _iso(pushed_days),
        "description": "a repo",
        "language": "Python",
    }


def test_score_candidate_returns_populated_dataclass():
    cand = score_candidate(_repo("a/b", 1200, 400, 3), _NOW)
    assert isinstance(cand, RepoCandidate)
    assert cand.full_name == "a/b"
    assert cand.url == "https://github.com/a/b"
    assert cand.stars == 1200
    assert cand.language == "Python"
    assert 0.0 <= cand.score <= 1.0
    # Composite equals the weighted sum of the three components.
    expected = (
        DEFAULT_WEIGHTS.momentum * cand.momentum
        + DEFAULT_WEIGHTS.activity * cand.activity
        + DEFAULT_WEIGHTS.maturity * cand.maturity
    )
    assert cand.score == pytest.approx(expected)


def test_score_candidate_strong_outranks_weak():
    strong = score_candidate(_repo("x/strong", 3000, 90, 2), _NOW)   # young-ish, active, traction
    stale = score_candidate(_repo("y/stale", 3000, 1500, 500), _NOW)  # old, abandoned
    assert strong.score > stale.score


def test_score_candidate_survives_missing_fields():
    cand = score_candidate({"fullName": "only/name"}, _NOW)
    assert cand.full_name == "only/name"
    assert cand.stars == 0
    assert 0.0 <= cand.score <= 1.0


# ── search_repos ─────────────────────────────────────────────────────────────


def _gh_json(*repos: dict) -> str:
    return json.dumps(list(repos))


@pytest.mark.asyncio
async def test_search_repos_parses_scores_and_sorts_desc():
    raw = _gh_json(
        _repo("slow/old", 3000, 1500, 500),    # should rank last
        _repo("fast/young", 3000, 90, 2),       # should rank first
    )
    with patch("genesis.recon.github_discovery.run_gh", AsyncMock(return_value=raw)):
        results = await search_repos("agent memory", limit=10, now=_NOW)
    assert [c.full_name for c in results] == ["fast/young", "slow/old"]
    assert all(isinstance(c, RepoCandidate) for c in results)


@pytest.mark.asyncio
async def test_search_repos_fetches_wide_then_cuts_to_limit():
    # P1-C: must fetch the API page cap (100) from gh, NOT `limit`, so scoring
    # selects from the full pool rather than a stars-biased top-`limit` slice.
    raw = _gh_json(*[_repo(f"r/{i}", 100 * i, 200, 5) for i in range(1, 6)])
    mock = AsyncMock(return_value=raw)
    with patch("genesis.recon.github_discovery.run_gh", mock):
        results = await search_repos("mcp", limit=2, now=_NOW)
    assert len(results) == 2  # cut to limit locally
    called_args = mock.call_args.args
    assert "100" in called_args  # fetched wide
    assert "2" not in called_args  # did NOT pass limit to gh


@pytest.mark.asyncio
async def test_search_repos_excludes_forks_in_query():
    mock = AsyncMock(return_value="[]")
    with patch("genesis.recon.github_discovery.run_gh", mock):
        await search_repos("rag", now=_NOW)
    query_arg = next(a for a in mock.call_args.args if "rag" in a)
    assert "fork:false" in query_arg


@pytest.mark.asyncio
async def test_search_repos_filters_archived():
    archived = _repo("dead/archived", 5000, 400, 3)
    archived["isArchived"] = True
    raw = _gh_json(archived, _repo("live/repo", 800, 200, 1))
    with patch("genesis.recon.github_discovery.run_gh", AsyncMock(return_value=raw)):
        results = await search_repos("harness", now=_NOW)
    assert [c.full_name for c in results] == ["live/repo"]


@pytest.mark.asyncio
async def test_search_repos_handles_empty_and_malformed():
    for bad in ("", "not json", '{"not": "a list"}'):
        with patch("genesis.recon.github_discovery.run_gh", AsyncMock(return_value=bad)):
            assert await search_repos("x", now=_NOW) == []


# ── discover (multi-topic) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_discover_dedupes_across_queries_keeping_best_score():
    # Same repo appears in two topic searches; keep one entry (highest score).
    q1 = _gh_json(_repo("dup/repo", 1000, 300, 5), _repo("a/one", 500, 300, 5))
    q2 = _gh_json(_repo("dup/repo", 1000, 300, 5), _repo("b/two", 700, 300, 5))
    with patch("genesis.recon.github_discovery.run_gh", AsyncMock(side_effect=[q1, q2])), \
            patch("genesis.recon.github_discovery.asyncio.sleep", AsyncMock()):
        results = await discover(["agents", "memory"], limit_per_query=10, now=_NOW)
    names = [c.full_name for c in results]
    assert names.count("dup/repo") == 1
    assert set(names) == {"dup/repo", "a/one", "b/two"}


@pytest.mark.asyncio
async def test_discover_applies_total_cap_and_spaces_calls():
    raw = _gh_json(_repo("a/1", 900, 300, 5), _repo("b/2", 800, 300, 5))
    sleep_mock = AsyncMock()
    with patch("genesis.recon.github_discovery.run_gh", AsyncMock(return_value=raw)), \
            patch("genesis.recon.github_discovery.asyncio.sleep", sleep_mock):
        results = await discover(["q1", "q2"], total_cap=3, now=_NOW)
    assert len(results) <= 3
    # Spacing between the 2 queries → exactly one inter-call sleep.
    assert sleep_mock.await_count == 1
