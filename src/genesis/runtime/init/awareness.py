"""Init function: _init_awareness."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


async def init(rt: GenesisRuntime) -> None:
    """Initialize the AwarenessLoop and all signal collectors."""
    try:
        from genesis.awareness.loop import AwarenessLoop
        from genesis.awareness.signals import (
            BudgetCollector,
            ContainerMemoryCollector,
            ConversationCollector,
            CriticalFailureCollector,
            ErrorSpikeCollector,
            JobHealthCollector,
            MemoryBacklogCollector,
            OutreachEngagementCollector,
            ReconFindingsCollector,
            StrategicTimerCollector,
            TaskQualityCollector,
        )

        collectors = [
            ConversationCollector(),
            TaskQualityCollector(),
            OutreachEngagementCollector(),
            ReconFindingsCollector(),
            MemoryBacklogCollector(),
            BudgetCollector(),
            ErrorSpikeCollector(),
            CriticalFailureCollector(),
            StrategicTimerCollector(),
            ContainerMemoryCollector(),
            JobHealthCollector(runtime=rt),
        ]

        rt._awareness_loop = AwarenessLoop(
            db=rt._db,
            collectors=collectors,
            interval_minutes=5,
            event_bus=rt._event_bus,
        )
        await rt._awareness_loop.start()
        logger.info("Genesis awareness loop started (5m interval)")
    except ImportError:
        logger.warning("genesis.awareness not available")
    except Exception:
        logger.exception("Failed to initialize awareness loop")
