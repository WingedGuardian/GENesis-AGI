"""Init function: _init_reflex — the reflex arc's afferent nerve.

When reflex ingestion is enabled (``config/reflex.yaml`` ``ingest_enabled``,
env kill ``GENESIS_REFLEX_INGEST_OFF``), this step:

1. starts the ``ReflexIngestor`` (bus subscriber + drain worker), and
2. installs the process-wide default event bus for ``tracked_task`` so all
   background-task failures emit ``task.failed`` — the nerve was dark
   before this (only ~3 of 66 call sites passed a bus explicitly).

Both are gated together: with ingestion off, the default bus stays unset,
so other ERROR+ bus consumers (e.g. the AZ notification bridge on installs
that wire it) see no new event class either. The ego reactive path carves
``task.failed`` out unconditionally (``runtime/init/ego.py``) — reflex owns
that class.

Runs after the ``tasks`` init step; the late-binding default bus covers
tasks created earlier in bootstrap the moment it is installed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


async def init(rt: GenesisRuntime) -> None:
    """Initialize reflex signal ingestion (dark unless explicitly enabled)."""
    if rt._db is None or rt._event_bus is None:
        logger.info("Reflex ingestion skipped — db/event bus unavailable")
        return

    from genesis.reflex.config import load_reflex_config

    cfg = load_reflex_config()
    if not cfg.ingest_enabled:
        logger.info("Reflex ingestion disabled (ingest_enabled=false) — nerve stays dark")
        return

    from genesis.reflex.ingest import ReflexIngestor

    ingestor = ReflexIngestor(rt._db)
    rt._reflex_ingestor = ingestor
    # start() subscribes, launches the drain worker, AND installs the default
    # event bus for tracked_task (it owns that lifecycle so a live disable can
    # unwind it). Cancelled + unwired via ingestor.stop() in runtime shutdown.
    ingestor.start(rt._event_bus)
    logger.info("Reflex ingestion ACTIVE — default event bus installed for tracked_task")
