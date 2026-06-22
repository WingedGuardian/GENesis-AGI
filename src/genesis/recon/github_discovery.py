"""GitHub Discovery — proactively find new repos in the user's domains.

Ranking engine (this module) + an on-demand MCP tool. A scheduled job that
files curated findings is layered on top separately. The engine searches via
the gh CLI, scores each repo on three axes, and returns a ranked shortlist:

  momentum  — stars-per-day-since-creation (a first-sight growth proxy; no
              star history needed), log-damped, denominator floored so a
              one-day-old hype repo can't dominate.
  activity  — recency of the last push (exponential decay): alive vs abandoned.
  maturity  — repo age (saturating): penalizes brand-new/unproven repos.

Scoring functions are pure and take an explicit ``now`` so they're
deterministic under test.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime

from genesis.recon.gh_cli import run_gh

logger = logging.getLogger(__name__)

# gh search returns at most 100/page. Fetch the full page and score locally so
# the momentum metric isn't defeated by gh's stars-ordered top-`limit` slice.
_GH_SEARCH_PAGE = 100
_GH_FIELDS = "fullName,url,stargazersCount,createdAt,pushedAt,description,language,isArchived"
# GitHub search API allows 30 req/min → ≥2s between topic queries.
_QUERY_SPACING_S = 2.0

# ── Scoring tunables (eyeballed at the foundation checkpoint, easy to adjust) ──
_MOMENTUM_AGE_FLOOR_DAYS = 30.0   # don't reward 1-day-old stars-per-day spikes
_MOMENTUM_SATURATION = 50.0       # stars/day at which momentum ≈ 1.0
_ACTIVITY_TAU_DAYS = 45.0         # push-recency decay constant
_MATURITY_TAU_DAYS = 180.0        # age-maturity rise constant


@dataclass(frozen=True)
class ScoreWeights:
    """Weights for the composite score. Sum need not be 1 (kept ≈1 here)."""

    momentum: float = 0.4
    activity: float = 0.3
    maturity: float = 0.3


# Default mirrors the inbox tool-scoring rubric (momentum 40 / activity 30 /
# maturity 30). The architect flagged push-recency as the noisiest signal; if
# the checkpoint shows activity misleading, shift toward .5/.2/.3.
DEFAULT_WEIGHTS = ScoreWeights()


@dataclass(frozen=True)
class RepoCandidate:
    """A scored GitHub repo discovered for the user's review."""

    full_name: str
    url: str
    stars: int
    created_at: str
    pushed_at: str
    description: str
    language: str
    momentum: float
    activity: float
    maturity: float
    score: float


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _days_between(iso: str | None, now: datetime) -> float | None:
    """Fractional days between an ISO timestamp and ``now``. None if unparsable."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (now - dt).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return None


def score_momentum(stars: int, created_at: str | None, now: datetime) -> float:
    """Stars-per-day-since-creation, log-damped. Floor the age so very young
    repos aren't rewarded for a high transient rate."""
    age = _days_between(created_at, now)
    if age is None:
        return 0.0
    denom = max(age, _MOMENTUM_AGE_FLOOR_DAYS)
    rate = max(stars, 0) / denom
    return _clamp01(math.log10(rate + 1.0) / math.log10(_MOMENTUM_SATURATION + 1.0))


def score_activity(pushed_at: str | None, now: datetime) -> float:
    """Exponential decay on days-since-last-push. Unknown push date → 0."""
    days = _days_between(pushed_at, now)
    if days is None:
        return 0.0
    return _clamp01(math.exp(-max(days, 0.0) / _ACTIVITY_TAU_DAYS))


def score_maturity(created_at: str | None, now: datetime) -> float:
    """Saturating rise with age: brand-new repos score low (unproven)."""
    age = _days_between(created_at, now)
    if age is None:
        return 0.0
    return _clamp01(1.0 - math.exp(-max(age, 0.0) / _MATURITY_TAU_DAYS))


def score_candidate(
    repo: dict, now: datetime, weights: ScoreWeights = DEFAULT_WEIGHTS
) -> RepoCandidate:
    """Score a single ``gh search repos --json`` item into a RepoCandidate.

    Tolerant of missing keys (gh omits null descriptions/languages).
    """
    full_name = str(repo.get("fullName") or repo.get("full_name") or "")
    url = str(repo.get("url") or "")
    stars = int(repo.get("stargazersCount") or repo.get("stars") or 0)
    created = str(repo.get("createdAt") or repo.get("created_at") or "")
    pushed = str(repo.get("pushedAt") or repo.get("pushed_at") or "")
    description = str(repo.get("description") or "")
    language = str(repo.get("language") or "")

    momentum = score_momentum(stars, created, now)
    activity = score_activity(pushed, now)
    maturity = score_maturity(created, now)
    # Keep full precision so `score` is exactly the weighted sum of the stored
    # components (display rounding happens at the MCP-tool boundary).
    score = _clamp01(
        weights.momentum * momentum
        + weights.activity * activity
        + weights.maturity * maturity
    )
    return RepoCandidate(
        full_name=full_name,
        url=url,
        stars=stars,
        created_at=created,
        pushed_at=pushed,
        description=description,
        language=language,
        momentum=momentum,
        activity=activity,
        maturity=maturity,
        score=score,
    )


# ── Search & discovery ───────────────────────────────────────────────────────


async def search_repos(
    query: str,
    *,
    limit: int = 10,
    now: datetime | None = None,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
) -> list[RepoCandidate]:
    """Search GitHub for repos matching ``query``, scored and ranked desc.

    Fetches a wide page from gh and scores locally (so a high-momentum,
    lower-star repo isn't pre-filtered away), excludes forks and archived
    repos, then returns the top ``limit`` by composite score. Returns [] on any
    gh failure or malformed output (callers treat that as "no results").
    """
    now = now or datetime.now(UTC)
    full_query = query if "fork:" in query else f"{query} fork:false"

    raw = await run_gh(
        "gh", "search", "repos", full_query,
        "--sort", "stars",
        "--limit", str(_GH_SEARCH_PAGE),
        "--json", _GH_FIELDS,
    )
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("github_discovery: malformed gh JSON for query %r", query)
        return []
    if not isinstance(items, list):
        return []

    candidates = [
        score_candidate(r, now, weights)
        for r in items
        if isinstance(r, dict) and not r.get("isArchived")
    ]
    candidates.sort(key=lambda c: c.score, reverse=True)
    logger.info(
        "github_discovery: query=%r fetched=%d ranked=%d returned=%d",
        query, len(items), len(candidates), min(limit, len(candidates)),
    )
    return candidates[:limit]


async def discover(
    queries: list[str],
    *,
    limit_per_query: int = 10,
    total_cap: int | None = None,
    now: datetime | None = None,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
    spacing_s: float = _QUERY_SPACING_S,
) -> list[RepoCandidate]:
    """Run several topic searches, dedupe by repo (keep highest score), rank.

    Spaces calls by ``spacing_s`` to respect GitHub's 30 req/min search limit.
    """
    now = now or datetime.now(UTC)
    best: dict[str, RepoCandidate] = {}

    for i, query in enumerate(queries):
        if i > 0 and spacing_s:
            await asyncio.sleep(spacing_s)
        for cand in await search_repos(
            query, limit=limit_per_query, now=now, weights=weights
        ):
            if not cand.full_name:
                continue
            prev = best.get(cand.full_name)
            if prev is None or cand.score > prev.score:
                best[cand.full_name] = cand

    ranked = sorted(best.values(), key=lambda c: c.score, reverse=True)
    return ranked[:total_cap] if total_cap else ranked
