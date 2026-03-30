"""Compute availability tracking — which surplus compute tiers are live."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import aiohttp

from genesis.surplus.types import ComputeTier

logger = logging.getLogger(__name__)


class ComputeAvailability:
    """Tracks available compute endpoints for surplus tasks.

    FREE_API is always considered available (failures handled by Router retries).
    LOCAL_30B availability is determined by pinging the LM Studio endpoint.
    Results are cached to avoid hammering endpoints.
    """

    def __init__(
        self,
        *,
        lmstudio_url: str | None = None,
        ping_timeout_s: int = 3,
        cache_ttl_s: int = 60,
        clock=None,
    ):
        from genesis.env import lm_studio_health_url

        self._lmstudio_url = lmstudio_url or lm_studio_health_url()
        self._ping_timeout_s = ping_timeout_s
        self._cache_ttl_s = cache_ttl_s
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lmstudio_cached: bool | None = None
        self._lmstudio_cached_at: datetime | None = None

    async def get_available_tiers(self) -> list[ComputeTier]:
        """Return which compute tiers are currently available for surplus."""
        tiers = [ComputeTier.FREE_API]
        if await self.check_lmstudio():
            tiers.append(ComputeTier.LOCAL_30B)
        return tiers

    async def check_lmstudio(self) -> bool:
        """Check if LM Studio is available, using cache if fresh."""
        now = self._clock()
        if (
            self._lmstudio_cached is not None
            and self._lmstudio_cached_at is not None
            and (now - self._lmstudio_cached_at).total_seconds() < self._cache_ttl_s
        ):
            return self._lmstudio_cached

        result = await self._ping_lmstudio()
        self._lmstudio_cached = result
        self._lmstudio_cached_at = now
        return result

    async def _ping_lmstudio(self) -> bool:
        """HTTP GET to LM Studio endpoint. Returns True if 200."""
        try:
            timeout = aiohttp.ClientTimeout(total=self._ping_timeout_s)
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(self._lmstudio_url) as resp,
            ):
                return resp.status == 200
        except (aiohttp.ClientError, TimeoutError, OSError):
            logger.debug("LM Studio ping failed: %s", self._lmstudio_url)
            return False

    # GROUNDWORK(v4-rate-tracking): add per-provider rate limit tracking
