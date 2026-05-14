"""Web monitoring — watches non-GitHub URLs for content changes.

Monitors blog posts, documentation pages, API changelogs, and competitor
sites for meaningful changes. Ships with an empty source list — user adds
sources via recon_sources MCP tool or dashboard when ready.

Cadence: weekly (Fridays), per config/recon_schedules.yaml.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_SOURCES_PATH = Path.home() / ".genesis" / "web_monitoring_sources.json"


class WebMonitoringJob:
    """Monitors URLs for content changes and routes findings through intake."""

    def __init__(self, *, db: aiosqlite.Connection) -> None:
        self._db = db

    async def run(self) -> dict:
        """Run a monitoring cycle. Returns summary dict."""
        sources = self._load_sources()
        if not sources:
            logger.info("Web monitoring: no sources configured — skipping")
            return {"checked": 0, "changes": 0, "sources_count": 0}

        checked = 0
        changes = 0
        errors = 0

        for source in sources:
            try:
                changed = await self._check_source(source)
                checked += 1
                if changed:
                    changes += 1
            except Exception:
                errors += 1
                logger.error(
                    "Web monitoring failed for %s",
                    source.get("url", "unknown"),
                    exc_info=True,
                )

        logger.info(
            "Web monitoring: checked=%d, changes=%d, errors=%d",
            checked, changes, errors,
        )
        return {
            "checked": checked,
            "changes": changes,
            "errors": errors,
            "sources_count": len(sources),
        }

    async def _check_source(self, source: dict) -> bool:
        """Check a single URL for changes. Returns True if changed."""
        import httpx

        url = source.get("url", "")
        if not url:
            return False

        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    logger.warning("Web monitoring: %s returned %d", url, resp.status_code)
                    return False
                body = resp.text
        except Exception:
            logger.warning("Web monitoring: failed to fetch %s", url, exc_info=True)
            return False

        # Compute content hash.
        content_hash = hashlib.sha256(body.encode()).hexdigest()[:32]

        # Check against last known hash.
        last_hash = await self._get_last_hash(url)
        if last_hash == content_hash:
            return False  # No change

        # Content changed — store the new hash and route through intake.
        await self._store_hash(url, content_hash)

        # Only route if we had a previous hash (skip first-time baseline).
        if last_hash is not None:
            await self._route_change(source, body, url)

        return last_hash is not None

    async def _get_last_hash(self, url: str) -> str | None:
        """Get last content hash for a URL from the DB."""
        try:
            cursor = await self._db.execute(
                "SELECT content_hash FROM web_monitoring_hashes WHERE url = ?",
                (url,),
            )
            row = await cursor.fetchone()
            return row[0] if row else None
        except Exception:
            # Table might not exist yet — create it.
            await self._ensure_table()
            return None

    async def _store_hash(self, url: str, content_hash: str) -> None:
        """Store or update the content hash for a URL."""
        await self._ensure_table()
        now = datetime.now(UTC).isoformat()
        await self._db.execute(
            "INSERT INTO web_monitoring_hashes (url, content_hash, checked_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(url) DO UPDATE SET content_hash = excluded.content_hash, "
            "checked_at = excluded.checked_at",
            (url, content_hash, now),
        )
        await self._db.commit()

    async def _ensure_table(self) -> None:
        """Create the hash tracking table if it doesn't exist."""
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS web_monitoring_hashes ("
            "  url TEXT PRIMARY KEY,"
            "  content_hash TEXT NOT NULL,"
            "  checked_at TEXT NOT NULL"
            ")"
        )
        await self._db.commit()

    async def _route_change(self, source: dict, body: str, url: str) -> None:
        """Route a detected change through the intake pipeline."""
        label = source.get("label", url)
        # Extract a content summary (first 500 chars of body, cleaned).
        summary = body[:500].strip()
        content = (
            f"Content change detected: {label}\n"
            f"URL: {url}\n\n"
            f"Content preview:\n{summary}"
        )

        try:
            from genesis.surplus.intake import IntakeSource, run_intake
            await run_intake(
                content=content,
                source=IntakeSource.WEB_MONITORING,
                source_task_type="web_monitoring",
                db=self._db,
            )
        except Exception:
            logger.warning(
                "Intake failed for web monitoring change at %s", url,
                exc_info=True,
            )

    @staticmethod
    def _load_sources() -> list[dict]:
        """Load monitored URL sources.

        Format: [{"url": "https://...", "label": "Blog Name", "category": "blog"}]
        """
        if not _SOURCES_PATH.exists():
            return []
        try:
            data = json.loads(_SOURCES_PATH.read_text())
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            logger.error("Failed to load web monitoring sources", exc_info=True)
            return []
