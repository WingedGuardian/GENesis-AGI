"""Provider activity snapshot from ProviderActivityTracker."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.observability.activity import ProviderActivityTracker


async def provider_activity(activity_tracker: ProviderActivityTracker | None) -> list[dict]:
    """Per-provider call metrics from ProviderActivityTracker."""
    if activity_tracker is None:
        return []
    result = await activity_tracker.summary_with_db_fallback()
    return result if isinstance(result, list) else []
