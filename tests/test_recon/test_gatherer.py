"""Tests for ReconGatherer — watchlist release monitoring."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from genesis.db import schema
from genesis.db.crud import observations
from genesis.recon.gatherer import GatherResult, ReconGatherer, _release_hash

# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        for ddl in schema.TABLES.values():
            await conn.execute(ddl)
        await conn.commit()
        yield conn


def _fake_releases(repo: str = "anthropics/claude-code") -> list[dict]:
    """Return a list of fake GitHub release dicts."""
    return [
        {
            "tag_name": "v2.1.80",
            "name": "v2.1.80",
            "published_at": "2026-03-19T22:08:48Z",
            "body": "## What's changed\n\n- Added rate_limits field",
            "html_url": f"https://github.com/{repo}/releases/tag/v2.1.80",
        },
        {
            "tag_name": "v2.1.79",
            "name": "v2.1.79",
            "published_at": "2026-03-18T22:29:36Z",
            "body": "Bug fixes and improvements",
            "html_url": f"https://github.com/{repo}/releases/tag/v2.1.79",
        },
    ]


_WATCHLIST = [
    {
        "name": "Claude Code",
        "repo": "anthropics/claude-code",
        "track": ["releases", "commits"],
        "priority": "high",
        "notes": "Primary intelligence layer.",
    },
    {
        "name": "Cognee",
        "repo": "topoteretes/cognee",
        "track": ["releases", "commits"],
        "priority": "medium",
        "notes": "Knowledge graph.",
    },
    {
        "name": "NoReleases",
        "repo": "example/no-releases",
        "track": ["commits"],
        "priority": "low",
        "notes": "Only commits.",
    },
]


# ── helpers ─────────────────────────────────────────────────────────────────


def _mock_subprocess(stdout: str, returncode: int = 0):
    """Create a mock for asyncio.create_subprocess_exec."""
    mock_proc = AsyncMock()
    mock_proc.communicate.return_value = (
        stdout.encode(),
        b"",
    )
    mock_proc.returncode = returncode
    mock_proc.pid = 12345

    async def create_subprocess(*args, **kwargs):
        return mock_proc

    return create_subprocess


# ── tests ───────────────────────────────────────────────────────────────────


class TestReleaseHash:
    def test_deterministic(self):
        h1 = _release_hash("anthropics/claude-code", "v2.1.80")
        h2 = _release_hash("anthropics/claude-code", "v2.1.80")
        assert h1 == h2

    def test_different_for_different_tags(self):
        h1 = _release_hash("anthropics/claude-code", "v2.1.80")
        h2 = _release_hash("anthropics/claude-code", "v2.1.79")
        assert h1 != h2

    def test_different_for_different_repos(self):
        h1 = _release_hash("anthropics/claude-code", "v1.0.0")
        h2 = _release_hash("frdel/agent-zero", "v1.0.0")
        assert h1 != h2

    def test_length(self):
        h = _release_hash("anthropics/claude-code", "v2.1.80")
        assert len(h) == 16


class TestGatherResult:
    def test_defaults(self):
        r = GatherResult()
        assert r.checked == 0
        assert r.new_findings == 0
        assert r.errors == 0
        assert r.details == []

    def test_frozen(self):
        r = GatherResult(checked=1, new_findings=2, errors=0)
        with pytest.raises(AttributeError):
            r.checked = 5  # type: ignore[misc]


class TestGatherReleases:
    @pytest.mark.asyncio
    async def test_stores_new_findings(self, db):
        gatherer = ReconGatherer(db)
        releases_json = json.dumps(_fake_releases())

        with (
            patch.object(ReconGatherer, "_load_watchlist", return_value=_WATCHLIST),
            patch.object(
                ReconGatherer, "_run_gh", return_value=releases_json
            ),
        ):
            result = await gatherer.gather_releases()

        assert result.checked == 2  # Claude Code + Cognee (both track releases)
        # Claude Code gets 2 releases, Cognee also gets 2 (same mock)
        assert result.new_findings == 4
        assert result.errors == 0

        # Verify observations in DB
        rows = await observations.query(
            db, source="recon", type="finding", category="github_releases"
        )
        assert len(rows) == 4

    @pytest.mark.asyncio
    async def test_dedup_on_second_run(self, db):
        gatherer = ReconGatherer(db)
        releases_json = json.dumps(_fake_releases())

        with (
            patch.object(ReconGatherer, "_load_watchlist", return_value=_WATCHLIST),
            patch.object(
                ReconGatherer, "_run_gh", return_value=releases_json
            ),
        ):
            result1 = await gatherer.gather_releases()
            result2 = await gatherer.gather_releases()

        assert result1.new_findings == 4
        assert result2.new_findings == 0  # all deduped

        rows = await observations.query(
            db, source="recon", type="finding", category="github_releases"
        )
        assert len(rows) == 4  # not 8

    @pytest.mark.asyncio
    async def test_filters_by_track(self, db):
        gatherer = ReconGatherer(db)
        releases_json = json.dumps(_fake_releases())

        with (
            patch.object(ReconGatherer, "_load_watchlist", return_value=_WATCHLIST),
            patch.object(
                ReconGatherer, "_run_gh", return_value=releases_json
            ),
        ):
            result = await gatherer.gather_releases()

        # NoReleases project should be skipped (only tracks commits)
        assert result.checked == 2  # not 3

    @pytest.mark.asyncio
    async def test_handles_gh_failure(self, db):
        gatherer = ReconGatherer(db)

        with (
            patch.object(ReconGatherer, "_load_watchlist", return_value=_WATCHLIST),
            patch.object(ReconGatherer, "_run_gh", return_value=""),
        ):
            result = await gatherer.gather_releases()

        assert result.checked == 2
        assert result.new_findings == 0
        assert result.errors == 0  # empty response is not an error, just no data

    @pytest.mark.asyncio
    async def test_handles_invalid_json(self, db):
        gatherer = ReconGatherer(db)

        with (
            patch.object(ReconGatherer, "_load_watchlist", return_value=_WATCHLIST),
            patch.object(ReconGatherer, "_run_gh", return_value="not json"),
        ):
            result = await gatherer.gather_releases()

        assert result.checked == 2
        assert result.new_findings == 0
        assert result.errors == 0

    @pytest.mark.asyncio
    async def test_handles_unexpected_format(self, db):
        gatherer = ReconGatherer(db)

        with (
            patch.object(ReconGatherer, "_load_watchlist", return_value=_WATCHLIST),
            patch.object(
                ReconGatherer, "_run_gh", return_value='{"not": "a list"}'
            ),
        ):
            result = await gatherer.gather_releases()

        assert result.new_findings == 0

    @pytest.mark.asyncio
    async def test_empty_watchlist(self, db):
        gatherer = ReconGatherer(db)

        with patch.object(ReconGatherer, "_load_watchlist", return_value=[]):
            result = await gatherer.gather_releases()

        assert result.checked == 0
        assert result.new_findings == 0

    @pytest.mark.asyncio
    async def test_project_without_repo(self, db):
        gatherer = ReconGatherer(db)
        no_repo = [{"name": "NoRepo", "track": ["releases"], "priority": "low"}]

        with patch.object(ReconGatherer, "_load_watchlist", return_value=no_repo):
            result = await gatherer.gather_releases()

        assert result.checked == 1
        assert result.new_findings == 0

    @pytest.mark.asyncio
    async def test_observation_content_format(self, db):
        gatherer = ReconGatherer(db)
        releases_json = json.dumps([_fake_releases()[0]])

        high_only = [w for w in _WATCHLIST if w["priority"] == "high"]

        with (
            patch.object(ReconGatherer, "_load_watchlist", return_value=high_only),
            patch.object(
                ReconGatherer, "_run_gh", return_value=releases_json
            ),
        ):
            await gatherer.gather_releases()

        rows = await observations.query(
            db, source="recon", type="finding", category="github_releases"
        )
        assert len(rows) == 1
        content = rows[0]["content"]
        assert "Claude Code v2.1.80" in content
        assert "Released: 2026-03-19" in content
        assert "Source: https://github.com/" in content

    @pytest.mark.asyncio
    async def test_observation_priority_matches_watchlist(self, db):
        gatherer = ReconGatherer(db)
        releases_json = json.dumps([_fake_releases()[0]])

        high_only = [w for w in _WATCHLIST if w["priority"] == "high"]

        with (
            patch.object(ReconGatherer, "_load_watchlist", return_value=high_only),
            patch.object(
                ReconGatherer, "_run_gh", return_value=releases_json
            ),
        ):
            await gatherer.gather_releases()

        rows = await observations.query(
            db, source="recon", type="finding", category="github_releases"
        )
        assert rows[0]["priority"] == "high"


class TestRunGh:
    @pytest.mark.asyncio
    async def test_success(self, db):
        gatherer = ReconGatherer(db)

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=_mock_subprocess('["ok"]'),
        ):
            result = await gatherer._run_gh("gh", "api", "test")

        assert result == '["ok"]'

    @pytest.mark.asyncio
    async def test_nonzero_exit(self, db):
        gatherer = ReconGatherer(db)

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=_mock_subprocess("error", returncode=1),
        ):
            result = await gatherer._run_gh("gh", "api", "test")

        assert result == ""

    @pytest.mark.asyncio
    async def test_timeout(self, db):
        gatherer = ReconGatherer(db)
        captured_mock = {}

        async def slow_subprocess(*args, **kwargs):
            mock = AsyncMock()
            mock.pid = 12345

            async def slow_communicate():
                await asyncio.sleep(100)
                return b"", b""

            mock.communicate = slow_communicate
            captured_mock["proc"] = mock
            return mock

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=slow_subprocess,
        ):
            result = await gatherer._run_gh("gh", "api", "test")

        assert result == ""
        # Verify process cleanup: kill and wait must be called
        proc = captured_mock["proc"]
        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_os_error(self, db):
        gatherer = ReconGatherer(db)

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=OSError("gh not found"),
        ):
            result = await gatherer._run_gh("gh", "api", "test")

        assert result == ""


class TestBodyTruncation:
    @pytest.mark.asyncio
    async def test_long_body_truncated(self, db):
        gatherer = ReconGatherer(db)
        long_body = "x" * 2000
        releases = [{
            "tag_name": "v1.0.0",
            "name": "v1.0.0",
            "published_at": "2026-01-01T00:00:00Z",
            "body": long_body,
            "html_url": "https://github.com/test/repo/releases/tag/v1.0.0",
        }]
        high_only = [{"name": "Test", "repo": "test/repo", "track": ["releases"], "priority": "medium"}]

        with (
            patch.object(ReconGatherer, "_load_watchlist", return_value=high_only),
            patch.object(
                ReconGatherer, "_run_gh", return_value=json.dumps(releases)
            ),
        ):
            await gatherer.gather_releases()

        rows = await observations.query(
            db, source="recon", type="finding", category="github_releases"
        )
        assert len(rows) == 1
        content = rows[0]["content"]
        assert "... (truncated)" in content
        # Content should be bounded — body was 2000 chars, truncated to ~1000
        assert len(content) < 1500
