"""Tests for the scheduled, curated GitHubDiscoveryJob (files to triage queue)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from genesis.recon.github_discovery import (
    GitHubDiscoveryJob,
    RepoCandidate,
    _discovery_hash,
)


def _cand(full_name: str, score: float) -> RepoCandidate:
    return RepoCandidate(
        full_name=full_name,
        url=f"https://github.com/{full_name}",
        stars=1000,
        created_at="2025-01-01T00:00:00Z",
        pushed_at="2026-06-20T00:00:00Z",
        description="desc",
        language="Python",
        momentum=score,
        activity=score,
        maturity=score,
        score=score,
    )


# ── dedup hash (P1-A) ────────────────────────────────────────────────────────


def test_discovery_hash_is_score_bucketed_and_stable():
    # Same repo, same score tier (0.6x) → same hash (no re-file on minor drift).
    assert _discovery_hash("a/b", 0.66) == _discovery_hash("a/b", 0.64)
    # Different score tier → different hash (re-surfaces if it climbs a tier).
    assert _discovery_hash("a/b", 0.66) != _discovery_hash("a/b", 0.74)
    # Different repo → different hash.
    assert _discovery_hash("a/b", 0.5) != _discovery_hash("c/d", 0.5)


# ── run() ────────────────────────────────────────────────────────────────────


def _wire_job(monkeypatch, job, *, candidates, watchlist=None, seen=None):
    """Stub the job's external deps for a deterministic run()."""
    monkeypatch.setattr(job, "_probe_gh_auth", AsyncMock(return_value=True))
    monkeypatch.setattr(job, "_resolve_topics", lambda: ["agents"])
    monkeypatch.setattr(job, "_watchlist_names", lambda: set(watchlist or set()))
    monkeypatch.setattr(
        "genesis.recon.github_discovery.discover", AsyncMock(return_value=candidates)
    )
    seen = seen or set()

    async def fake_exists(db, *, source, content_hash, unresolved_only=False):
        return content_hash in seen

    create = AsyncMock()
    monkeypatch.setattr(
        "genesis.recon.github_discovery.observations.exists_by_hash",
        AsyncMock(side_effect=fake_exists),
    )
    monkeypatch.setattr(
        "genesis.recon.github_discovery.observations.create", create
    )
    return create


@pytest.mark.asyncio
async def test_run_skips_when_gh_not_authenticated(monkeypatch):
    job = GitHubDiscoveryJob(db=object())
    monkeypatch.setattr(job, "_probe_gh_auth", AsyncMock(return_value=False))
    result = await job.run()
    assert result["filed"] == 0
    assert result.get("skipped")


@pytest.mark.asyncio
async def test_run_files_top_survivors_capped(monkeypatch):
    job = GitHubDiscoveryJob(db=object())
    # 8 candidates 0.90..0.55, all above threshold → cap files only the top 5.
    cands = [_cand(f"r/{i}", round(0.90 - i * 0.05, 2)) for i in range(8)]
    create = _wire_job(monkeypatch, job, candidates=cands)
    result = await job.run()
    assert result["filed"] == 5
    filed_names = [c.kwargs["content"].split(" —")[0] for c in create.await_args_list]
    assert filed_names == ["r/0", "r/1", "r/2", "r/3", "r/4"]
    # Files as a triageable recon finding (NOT the knowledge base).
    kw = create.await_args_list[0].kwargs
    assert kw["source"] == "recon"
    assert kw["type"] == "finding"
    assert kw["category"] == "github_discovery"


@pytest.mark.asyncio
async def test_run_drops_below_threshold(monkeypatch):
    job = GitHubDiscoveryJob(db=object())
    cands = [_cand("good/repo", 0.80), _cand("weak/repo", 0.30)]
    create = _wire_job(monkeypatch, job, candidates=cands)
    result = await job.run()
    assert result["filed"] == 1
    assert "good/repo" in create.await_args_list[0].kwargs["content"]


@pytest.mark.asyncio
async def test_run_dedups_already_filed(monkeypatch):
    job = GitHubDiscoveryJob(db=object())
    cands = [_cand("a/seen", 0.90), _cand("b/new", 0.80)]
    create = _wire_job(
        monkeypatch, job, candidates=cands, seen={_discovery_hash("a/seen", 0.90)}
    )
    result = await job.run()
    assert result["filed"] == 1
    assert "b/new" in create.await_args_list[0].kwargs["content"]


@pytest.mark.asyncio
async def test_run_rerun_dedups_top_n_does_not_walk_pool(monkeypatch):
    """Re-running with the same pool files 0 — it must NOT dredge ranks 6-10 to
    refill the cap once the top-N are already filed (the curation guarantee)."""
    job = GitHubDiscoveryJob(db=object())
    cands = [_cand(f"r/{i}", round(0.90 - i * 0.04, 2)) for i in range(12)]  # all >0.55
    monkeypatch.setattr(job, "_probe_gh_auth", AsyncMock(return_value=True))
    monkeypatch.setattr(job, "_resolve_topics", lambda: ["t"])
    monkeypatch.setattr(job, "_watchlist_names", lambda: set())
    monkeypatch.setattr(
        "genesis.recon.github_discovery.discover", AsyncMock(return_value=cands)
    )
    seen: set[str] = set()

    async def fake_exists(db, *, source, content_hash, unresolved_only=False):
        return content_hash in seen

    async def fake_create(db, **kw):
        seen.add(kw["content_hash"])

    monkeypatch.setattr(
        "genesis.recon.github_discovery.observations.exists_by_hash",
        AsyncMock(side_effect=fake_exists),
    )
    monkeypatch.setattr(
        "genesis.recon.github_discovery.observations.create",
        AsyncMock(side_effect=fake_create),
    )
    r1 = await job.run()
    r2 = await job.run()
    assert r1["filed"] == 5
    assert r2["filed"] == 0


@pytest.mark.asyncio
async def test_run_skips_watchlist_repos(monkeypatch):
    job = GitHubDiscoveryJob(db=object())
    cands = [_cand("known/repo", 0.95), _cand("fresh/repo", 0.80)]
    create = _wire_job(
        monkeypatch, job, candidates=cands, watchlist={"known/repo"}
    )
    result = await job.run()
    assert result["filed"] == 1
    assert "fresh/repo" in create.await_args_list[0].kwargs["content"]
