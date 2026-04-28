"""DistributionManager — routes publish requests to platform adapters."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from genesis.distribution.base import PlatformDistributor, PostResult

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


class DistributionManager:
    """Routes content to the appropriate platform distributor and records results.

    Each platform is registered as a ``PlatformDistributor`` keyed by its
    slug (e.g. ``"linkedin"``).  The manager handles dispatch, result
    recording, and error logging.
    """

    def __init__(self, db: aiosqlite.Connection | None = None) -> None:
        self._distributors: dict[str, PlatformDistributor] = {}
        self._db = db

    def register(self, distributor: PlatformDistributor) -> None:
        """Register a platform distributor."""
        self._distributors[distributor.platform] = distributor
        logger.info("Registered distributor: %s", distributor.platform)

    @property
    def available_platforms(self) -> list[str]:
        """Return list of registered platform slugs."""
        return list(self._distributors.keys())

    async def distribute(
        self,
        content: str,
        platforms: list[str],
        *,
        publish_id: str | None = None,
        visibility: str = "PUBLIC",
    ) -> list[PostResult]:
        """Distribute content to one or more platforms.

        Args:
            content: The content text to publish.
            platforms: Platform slugs to publish to.
            publish_id: Optional content_publishes row ID to update.
            visibility: Platform visibility setting.

        Returns:
            List of PostResult, one per platform attempted.
        """
        results: list[PostResult] = []

        for platform in platforms:
            distributor = self._distributors.get(platform)
            if distributor is None:
                results.append(PostResult(
                    post_id=None,
                    platform=platform,
                    url=None,
                    status="failed",
                    error=f"No distributor registered for platform: {platform}",
                ))
                continue

            result = await distributor.publish(content, visibility=visibility)
            results.append(result)

            if self._db is not None and publish_id:
                await self._update_publish_record(publish_id, result)

        return results

    async def _update_publish_record(
        self,
        publish_id: str,
        result: PostResult,
    ) -> None:
        """Update a content_publishes row with distribution results."""
        if self._db is None:
            return
        try:
            now = datetime.now(UTC).isoformat()
            await self._db.execute(
                """UPDATE content_publishes
                   SET status = ?,
                       platform_post_id = ?,
                       post_url = ?,
                       error_message = ?,
                       distributed_at = ?
                   WHERE id = ?""",
                (
                    result.status,
                    result.post_id,
                    result.url,
                    result.error,
                    now if result.status == "published" else None,
                    publish_id,
                ),
            )
            await self._db.commit()
        except Exception:
            logger.warning("Failed to update publish record %s", publish_id, exc_info=True)
