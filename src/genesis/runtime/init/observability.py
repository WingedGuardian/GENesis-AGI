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
        from genesis.observability import spans as _spans
        from genesis.observability.provider_activity import ProviderActivityTracker
        from genesis.observability.span_writer import SpanWriter

        configure_logging(level=logging.INFO)
        rt._event_bus = GenesisEventBus()
        if rt._db is not None:
            rt._event_bus.enable_persistence(rt._db)
        rt._activity_tracker = ProviderActivityTracker()
        if rt._db is not None:
            rt._activity_tracker.set_db(rt._db)
        # Tracing backbone: wire the span writer + activate capture. Honors
        # GENESIS_SPANS_DISABLED (kill switch read inside set_writer). Dark until
        # a later phase instruments route_call / operation boundaries.
        rt._span_writer = SpanWriter()
        if rt._db is not None:
            rt._span_writer.set_db(rt._db, process="server")
        _spans.set_writer(rt._span_writer)
        logger.info("Genesis observability initialized")
    except ImportError:
        logger.warning("genesis.observability not available")
    except Exception:
        logger.exception("Failed to initialize observability")
