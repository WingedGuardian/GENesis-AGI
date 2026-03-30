"""Init function: _init_observability."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


def init(rt: GenesisRuntime) -> None:
    """Initialize observability: event bus, logging, activity tracker."""
    try:
        from genesis.observability import (
            GenesisEventBus,
            configure_logging,
        )
        from genesis.observability.provider_activity import ProviderActivityTracker

        configure_logging(level=logging.INFO)
        rt._event_bus = GenesisEventBus()
        if rt._db is not None:
            rt._event_bus.enable_persistence(rt._db)
        rt._activity_tracker = ProviderActivityTracker()
        if rt._db is not None:
            rt._activity_tracker.set_db(rt._db)
        logger.info("Genesis observability initialized")
    except ImportError:
        logger.warning("genesis.observability not available")
    except Exception:
        logger.exception("Failed to initialize observability")
