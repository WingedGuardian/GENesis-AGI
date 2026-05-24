"""Provider criticality tiers for health monitoring and alerting.

Defines which providers are CRITICAL (alert immediately), WARNING
(dashboard orange, morning report), or INFO (dashboard note, no alert).
Used by the dashboard Provider Health card and credit exhaustion detection.
"""

from __future__ import annotations

from enum import IntEnum


class ProviderTier(IntEnum):
    """Criticality tier — higher value = more critical."""

    INFO = 1       # Fallback/optional — dashboard note, no alert
    WARNING = 2    # Primary but non-critical — dashboard orange, morning report
    CRITICAL = 3   # System-critical — alert immediately, dashboard red


# Maps activity_log provider names to their criticality tier.
# Provider names here match what ProviderActivityTracker.record() uses.
PROVIDER_TIERS: dict[str, ProviderTier] = {
    # CRITICAL — system breaks without these
    "episodic_memory_embedding": ProviderTier.CRITICAL,
    "qdrant.search": ProviderTier.CRITICAL,
    "qdrant.upsert": ProviderTier.CRITICAL,

    # WARNING — degraded experience
    "web_search": ProviderTier.WARNING,
    "web_fetch": ProviderTier.WARNING,

    # INFO — fallback/optional providers (everything else defaults to INFO)
}

# Friendly display names for dashboard
PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "episodic_memory_embedding": "Embeddings",
    "qdrant.search": "Qdrant Search",
    "qdrant.upsert": "Qdrant Upsert",
    "web_search": "Web Search",
    "web_fetch": "Web Fetch",
}


def get_tier(provider: str) -> ProviderTier:
    """Return the tier for a provider, defaulting to INFO."""
    return PROVIDER_TIERS.get(provider, ProviderTier.INFO)


def get_display_name(provider: str) -> str:
    """Return a human-friendly name for dashboard display."""
    return PROVIDER_DISPLAY_NAMES.get(provider, provider)
