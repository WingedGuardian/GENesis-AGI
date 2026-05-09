"""ReconGatherer — checks watchlist projects for GitHub releases and stars via gh CLI."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from genesis.db.crud import observations

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
_WATCHLIST_PATH = _CONFIG_DIR / "recon_watchlist.yaml"
_GH_TIMEOUT = 15  # seconds — network calls are slower than local git
_RELEASES_PER_PROJECT = 5
_MAX_BODY_CHARS = 1000


@dataclass(frozen=True)
class GatherResult:
    """Summary of a recon gathering run."""

    checked: int = 0
    new_findings: int = 0
    errors: int = 0
    details: list[str] = field(default_factory=list)


def _release_hash(repo: str, tag_name: str) -> str:
    """Deterministic content hash for a release — used for dedup."""
    return hashlib.sha256(f"{repo}:{tag_name}".encode()).hexdigest()[:16]


def _stars_hash(repo: str, count: int) -> str:
    """Deterministic content hash for a star count — deduplicates unchanged counts."""
    return hashlib.sha256(f"{repo}:stars:{count}".encode()).hexdigest()[:16]


class ReconGatherer:
    """Gathers GitHub releases for watchlist projects via the gh CLI.

    Findings are informational — visible via dashboard and recon_findings MCP
    tool. Push alerts are NOT sent; those are reserved for critical infra issues
    (see health_outreach.py).
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def gather_releases(self) -> GatherResult:
        """Check all watchlist projects for new releases. Returns summary."""
        projects = self._load_watchlist()
        release_projects = [
            p for p in projects if "releases" in p.get("track", [])
        ]

        if not release_projects:
            return GatherResult(details=["No projects track releases"])

        checked = 0
        new_total = 0
        errors = 0
        details: list[str] = []

        for project in release_projects:
            try:
                new_count = await self._check_releases(project)
                checked += 1
                new_total += new_count
                if new_count > 0:
                    details.append(
                        f"{project['name']}: {new_count} new release(s)"
                    )
            except Exception:
                errors += 1
                details.append(f"{project['name']}: error checking releases")
                logger.error(
                    "Failed to check releases for %s",
                    project.get("name", "unknown"),
                    exc_info=True,
                )

        result = GatherResult(
            checked=checked,
            new_findings=new_total,
            errors=errors,
            details=details,
        )
        logger.info(
            "Recon gather: checked=%d, new=%d, errors=%d",
            result.checked,
            result.new_findings,
            result.errors,
        )
        return result

    async def gather_stars(self) -> GatherResult:
        """Check star counts for watchlist projects. Store changes as findings."""
        projects = self._load_watchlist()
        star_projects = [
            p for p in projects if "stars" in p.get("track", [])
        ]

        if not star_projects:
            return GatherResult(details=["No projects track stars"])

        checked = 0
        new_total = 0
        errors = 0
        details: list[str] = []

        for project in star_projects:
            try:
                stored = await self._check_stars(project)
                checked += 1
                if stored:
                    new_total += 1
                    details.append(stored)
            except Exception:
                errors += 1
                details.append(f"{project['name']}: error checking stars")
                logger.error(
                    "Failed to check stars for %s",
                    project.get("name", "unknown"),
                    exc_info=True,
                )

        result = GatherResult(
            checked=checked, new_findings=new_total,
            errors=errors, details=details,
        )
        logger.info(
            "Star gather: checked=%d, new=%d, errors=%d",
            result.checked, result.new_findings, result.errors,
        )
        return result

    async def _check_stars(self, project: dict) -> str | None:
        """Check a single project's star count. Returns detail string if changed."""
        repo = project.get("repo", "")
        if not repo:
            return None

        raw = await self._run_gh(
            "gh", "api", f"repos/{repo}", "--jq", ".stargazers_count",
        )
        if not raw:
            return None

        try:
            count = int(raw)
        except ValueError:
            logger.warning("Non-integer star count for %s: %s", repo, raw)
            return None

        content_hash = _stars_hash(repo, count)

        if await observations.exists_by_hash(
            self._db, source="recon", content_hash=content_hash
        ):
            return None  # Count unchanged

        # Get previous count for delta
        prev_count = await self._get_previous_star_count(repo)
        delta = count - prev_count if prev_count is not None else None

        name = project.get("name", repo)
        delta_str = f" ({'+' if delta > 0 else ''}{delta} since last check)" if delta is not None else ""
        content = f"{name}: {count} stars{delta_str}"

        now = datetime.now(UTC).isoformat()
        await observations.create(
            self._db,
            id=str(uuid.uuid4()),
            source="recon",
            type="finding",
            category="github_stars",
            content=content,
            priority=project.get("priority", "medium"),
            created_at=now,
            content_hash=content_hash,
        )

        detail = f"{name}: {count} stars{delta_str}"
        logger.info("Star count recorded: %s", detail)
        return detail

    async def _get_previous_star_count(self, repo: str) -> int | None:
        """Get the most recent star count for a repo from observations."""
        try:
            cursor = await self._db.execute(
                "SELECT content FROM observations "
                "WHERE source = 'recon' AND category = 'github_stars' "
                "AND content LIKE ? "
                "ORDER BY created_at DESC LIMIT 1",
                (f"%{repo.split('/')[-1]}%",),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            # Content format: "Name: 30 stars (+5 since last check)"
            text = row[0] if isinstance(row, tuple) else row["content"]
            # Extract the number before " stars"
            parts = text.split(" stars")[0].rsplit(": ", 1)
            if len(parts) == 2:
                return int(parts[1])
        except (ValueError, IndexError, Exception):
            pass
        return None

    async def _check_releases(self, project: dict) -> int:
        """Check a single project for new releases. Returns count of new findings."""
        repo = project.get("repo", "")
        if not repo:
            logger.warning("Watchlist project %s has no repo", project.get("name"))
            return 0

        raw = await self._run_gh(
            "gh", "api",
            f"repos/{repo}/releases",
            "--jq", f".[0:{_RELEASES_PER_PROJECT}]",
        )
        if not raw:
            logger.warning("No release data from gh for %s", repo)
            return 0

        try:
            releases = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Invalid JSON from gh for %s: %s", repo, raw[:200])
            return 0

        if not isinstance(releases, list):
            logger.warning("Unexpected release format for %s: %s", repo, type(releases))
            return 0

        new_count = 0
        for release in releases:
            if not isinstance(release, dict):
                continue

            tag_name = release.get("tag_name", "")
            if not tag_name:
                continue

            content_hash = _release_hash(repo, tag_name)

            if await observations.exists_by_hash(
                self._db, source="recon", content_hash=content_hash
            ):
                continue

            # New release — store it
            name = release.get("name", tag_name)
            published = release.get("published_at", "")
            body = release.get("body", "") or ""
            html_url = release.get("html_url", "")

            if len(body) > _MAX_BODY_CHARS:
                body = body[:_MAX_BODY_CHARS] + "\n... (truncated)"

            content = f"{project['name']} {name}\n\nReleased: {published}\n{body}"
            if html_url:
                content += f"\n\nSource: {html_url}"

            priority = project.get("priority", "medium")
            now = datetime.now(UTC).isoformat()

            await observations.create(
                self._db,
                id=str(uuid.uuid4()),
                source="recon",
                type="finding",
                category="github_releases",
                content=content,
                priority=priority,
                created_at=now,
                content_hash=content_hash,
            )
            new_count += 1

        return new_count

    async def _run_gh(self, *args: str) -> str:
        """Run gh CLI command with timeout. Returns stdout or empty on failure."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_GH_TIMEOUT
            )
            if proc.returncode != 0:
                logger.warning(
                    "gh command failed (rc=%d): %s — %s",
                    proc.returncode,
                    " ".join(args),
                    stderr.decode("utf-8", errors="replace")[:200],
                )
                return ""
            return stdout.decode("utf-8", errors="replace").strip()
        except TimeoutError:
            logger.warning("gh command timed out: %s", " ".join(args))
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(ChildProcessError):
                await proc.wait()
            return ""
        except OSError:
            logger.warning("gh command failed to start: %s", " ".join(args), exc_info=True)
            return ""

    @staticmethod
    def _load_watchlist() -> list[dict]:
        """Load the hardcoded project watchlist."""
        if not _WATCHLIST_PATH.exists():
            return []
        try:
            import yaml

            with open(_WATCHLIST_PATH) as f:
                data = yaml.safe_load(f)
            return data.get("projects", []) if data else []
        except Exception:
            logger.error("Failed to load watchlist", exc_info=True)
            return []
