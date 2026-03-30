"""ProviderRegistry — in-memory registry backed by tool_registry DB table."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from genesis.providers.types import (
    CostTier,
    ProviderCategory,
    ProviderInfo,
)

if TYPE_CHECKING:
    import aiosqlite

    from genesis.observability import GenesisEventBus
    from genesis.providers.protocol import ToolProvider

logger = logging.getLogger("genesis.providers.registry")


class ProviderRegistry:
    """Runtime registry of ToolProvider instances.

    In-memory dict is primary (fast lookups).  DB (tool_registry table) is
    persistence/audit layer — optional, for tests or standalone use.
    """

    def __init__(
        self,
        db: aiosqlite.Connection | None = None,
        event_bus: GenesisEventBus | None = None,
    ) -> None:
        self._db = db
        self._event_bus = event_bus
        self._providers: dict[str, ToolProvider] = {}

    # ── registration ──────────────────────────────────────────────────

    def register(self, provider: ToolProvider) -> None:
        """Register a provider in-memory."""
        self._providers[provider.name] = provider
        logger.info("Registered provider: %s", provider.name)

    async def register_and_sync(self, provider: ToolProvider) -> None:
        """Register in-memory and persist to DB."""
        self.register(provider)
        if self._db is not None:
            await self._sync_to_db(provider)

    def unregister(self, name: str) -> bool:
        """Remove a provider.  Returns True if it existed."""
        removed = self._providers.pop(name, None)
        if removed:
            logger.info("Unregistered provider: %s", name)
        return removed is not None

    # ── lookup ────────────────────────────────────────────────────────

    def get(self, name: str) -> ToolProvider | None:
        return self._providers.get(name)

    def list_all(self) -> list[ToolProvider]:
        return list(self._providers.values())

    def list_by_category(self, category: ProviderCategory) -> list[ToolProvider]:
        """Return providers whose capability includes the given category."""
        return [
            p
            for p in self._providers.values()
            if category in p.capability.categories
        ]

    def route_by_content_type(self, content_type: str) -> list[ToolProvider]:
        """Find providers that handle a content type, sorted cheapest-first."""
        matches = [
            p
            for p in self._providers.values()
            if content_type in p.capability.content_types
        ]
        tier_order = {
            CostTier.FREE: 0,
            CostTier.CHEAP: 1,
            CostTier.MODERATE: 2,
            CostTier.EXPENSIVE: 3,
        }
        matches.sort(key=lambda p: tier_order.get(p.capability.cost_tier, 99))
        return matches

    def info(self, name: str) -> ProviderInfo | None:
        """Return a read-only snapshot of a provider's state."""
        p = self._providers.get(name)
        if p is None:
            return None
        return ProviderInfo(name=p.name, capability=p.capability)

    # ── invocation tracking ───────────────────────────────────────────

    async def record_invocation(self, provider_name: str) -> None:
        """Record that a provider was invoked (DB audit)."""
        if self._db is None:
            return
        try:
            from genesis.db.crud import tool_registry

            now = datetime.now(UTC).isoformat()
            provider_id = str(
                uuid.uuid5(uuid.NAMESPACE_DNS, f"provider:{provider_name}")
            )
            await tool_registry.record_invocation(
                self._db, provider_id, last_used=now
            )
        except Exception:
            logger.warning(
                "Failed to record invocation for %s", provider_name, exc_info=True
            )

    async def record_gap(
        self, content_type: str, attempted: list[str]
    ) -> str | None:
        """Record a capability gap when no provider handles a content type."""
        if self._db is None:
            return None
        try:
            from genesis.db.crud import capability_gaps

            now = datetime.now(UTC).isoformat()
            description = (
                f"No provider handles content_type={content_type}. "
                f"Tried: {', '.join(attempted)}"
            )
            gap_id = str(
                uuid.uuid5(uuid.NAMESPACE_DNS, f"content_type:{content_type}")
            )
            await capability_gaps.upsert(
                self._db,
                id=gap_id,
                description=description,
                gap_type="capability_gap",
                first_seen=now,
                last_seen=now,
            )
            return gap_id
        except Exception:
            logger.warning(
                "Failed to record gap for %s", content_type, exc_info=True
            )
            return None

    # ── DB sync ───────────────────────────────────────────────────────

    async def _sync_to_db(self, provider: ToolProvider) -> None:
        """Persist provider metadata to tool_registry table."""
        if self._db is None:
            return
        try:
            from genesis.db.crud import tool_registry

            now = datetime.now(UTC).isoformat()
            provider_id = str(
                uuid.uuid5(uuid.NAMESPACE_DNS, f"provider:{provider.name}")
            )
            cap = provider.capability
            metadata = {
                "content_types": list(cap.content_types),
                "categories": [str(c) for c in cap.categories],
                "description": cap.description,
            }
            await tool_registry.upsert(
                self._db,
                id=provider_id,
                name=provider.name,
                category=(
                    str(cap.categories[0]) if cap.categories else "uncategorized"
                ),
                description=cap.description or provider.name,
                tool_type="builtin",
                provider=provider.name,
                cost_tier=str(cap.cost_tier),
                created_at=now,
                metadata=metadata,
            )
        except Exception:
            logger.warning(
                "Failed to sync provider %s to DB", provider.name, exc_info=True
            )
