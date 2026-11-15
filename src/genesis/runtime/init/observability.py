"""Init function: _init_observability."""

from __future__ import annotations

import asyncio
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
        # Tracing backbone: wire the span writer + activate capture per config.
        # Capture is OFF if config/observability.yaml sets spans.enabled=false OR
        # GENESIS_SPANS_DISABLED=1 (the env kill switch, honored in set_writer).
        from genesis.observability.span_config import load_spans_config

        rt._span_writer = SpanWriter()
        if rt._db is not None:
            rt._span_writer.set_db(rt._db, process="server")
        _spans_enabled, _ = load_spans_config()
        _spans.set_writer(rt._span_writer, enabled=_spans_enabled)
        logger.info("Genesis observability initialized")
    except ImportError:
        logger.warning("genesis.observability not available")
    except Exception:
        logger.exception("Failed to initialize observability")

    _start_failure_count_sweeper(rt)


def _start_failure_count_sweeper(rt: GenesisRuntime) -> None:
    """Declare coalesced CC failure counts of sessions that went quiet.

    Kept apart from the bus setup above so a failure here never costs the bus.
    Cancelled in ``GenesisRuntime.shutdown``.
    """
    if rt._event_bus is None:
        return
    running = getattr(rt, "_failure_count_sweeper_task", None)
    if running is not None and not running.done():
        return  # already started (re-entered init)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop (a synchronous caller): nothing to own the task
    try:
        from genesis.cc.invoker import run_failure_count_sweeper
        from genesis.util.tasks import tracked_task

        rt._failure_count_sweeper_task = tracked_task(
            run_failure_count_sweeper(),
            name="cc-failure-count-sweeper",
        )
    except Exception:
        logger.exception("Failed to start the CC failure-count sweeper")
