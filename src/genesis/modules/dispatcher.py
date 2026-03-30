"""Module dispatcher — routes pipeline signals to the correct capability module."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from genesis.modules.base import CapabilityModule
    from genesis.modules.registry import ModuleRegistry
    from genesis.pipeline.types import ResearchSignal

logger = logging.getLogger(__name__)


class ModuleDispatcher:
    """Routes pipeline signals to the module subscribed to each research profile.

    After the pipeline produces surviving signals for a profile, the dispatcher
    finds the matching module and calls its ``handle_opportunity()`` method.
    """

    def __init__(self, module_registry: ModuleRegistry) -> None:
        self._registry = module_registry

    def _resolve_module(self, profile_name: str) -> CapabilityModule | None:
        """Find the enabled module subscribed to a given research profile."""
        for name in self._registry.list_enabled():
            mod = self._registry.get(name)
            if mod and mod.get_research_profile_name() == profile_name:
                return mod
        return None

    async def dispatch(
        self,
        profile_name: str,
        signals: list[ResearchSignal],
        *,
        router: Any = None,
    ) -> dict | None:
        """Dispatch surviving signals to the matching module.

        Returns an action proposal dict if the module finds an opportunity,
        None otherwise. The proposal will have ``requires_approval: True``
        for user-facing decisions.
        """
        mod = self._resolve_module(profile_name)
        if mod is None:
            logger.debug("No enabled module for profile %s", profile_name)
            return None

        # Build opportunity dict appropriate to the module type.
        # Prediction markets module expects {"market": Market, ...} and
        # handles scanning internally via its scanner.
        # Crypto ops module expects {"signals": [...], "router": ...}.
        # We use the scanner-based path for prediction markets and the
        # signal-based path for everything else.
        has_scanner = hasattr(mod, "_scanner") and hasattr(mod._scanner, "scan")

        if has_scanner:
            # Module has a market scanner — run it to get structured Market objects
            try:
                markets = await mod._scanner.scan()
                if not markets:
                    logger.debug("Scanner returned no markets for %s", profile_name)
                    return None
                # Try each market until one produces a proposal
                for market in markets[:10]:  # Cap at 10 to avoid runaway LLM calls
                    opportunity = {"market": market, "router": router}
                    proposal = await mod.handle_opportunity(opportunity)
                    if proposal is not None:
                        return proposal
            except Exception:
                logger.warning("Scanner dispatch failed for %s", profile_name, exc_info=True)
            return None

        # Signal-based path: pass raw signal content to the module
        opportunity = {
            "signals": [s.content for s in signals],
            "router": router,
        }
        try:
            return await mod.handle_opportunity(opportunity)
        except Exception:
            logger.warning("Dispatch to %s failed", mod.name, exc_info=True)
            return None
