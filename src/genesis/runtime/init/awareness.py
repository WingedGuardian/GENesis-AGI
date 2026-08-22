"""Init function: _init_awareness."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


def build_bootstrap_collectors(rt: GenesisRuntime) -> list:
    """Build the bootstrap signal-collector set (stub + runtime-backed collectors).

    Installed at awareness-loop construction, BEFORE the learning subsystem swaps
    in the real collectors (``runtime/init/learning.py::build_learning_collectors``,
    via ``AwarenessLoop.replace_collectors`` — a full replacement). Every signal
    produced here MUST be carried into the steady-state swap unless it is listed in
    ``genesis.awareness.types.BOOTSTRAP_ONLY_SIGNALS`` — enforced by the parity
    guard in ``tests/test_learning/test_extension_wiring.py``.
    """
    from genesis.awareness.signals import (
        AutonomyActivityCollector,
        BudgetCollector,
        ContainerMemoryCollector,
        ConversationCollector,
        CriticalFailureCollector,
        ErrorSpikeCollector,
        EventLoopLatencyCollector,
        GuardianActivityCollector,
        JobHealthCollector,
        LightCascadeCollector,
        OutreachEngagementCollector,
        ProcessHealthCollector,
        ReconFindingsCollector,
        SchedulerLivenessCollector,
        SentinelActivityCollector,
        StrategicTimerCollector,
        SurplusActivityCollector,
        TaskQualityCollector,
        UserGoalStalenessCollector,
        UserSessionPatternCollector,
    )

    return [
        ConversationCollector(),
        TaskQualityCollector(),
        OutreachEngagementCollector(),
        ReconFindingsCollector(),
        BudgetCollector(),
        ErrorSpikeCollector(),
        CriticalFailureCollector(),
        StrategicTimerCollector(),
        ContainerMemoryCollector(),
        JobHealthCollector(runtime=rt),
        SchedulerLivenessCollector(runtime=rt),
        EventLoopLatencyCollector(),
        LightCascadeCollector(),
        SentinelActivityCollector(),
        GuardianActivityCollector(),
        SurplusActivityCollector(),
        AutonomyActivityCollector(),
        ProcessHealthCollector(),
        UserGoalStalenessCollector(),
        UserSessionPatternCollector(),
    ]


async def init(rt: GenesisRuntime) -> None:
    """Initialize the AwarenessLoop and all signal collectors."""
    try:
        from genesis.awareness.loop import AwarenessLoop

        collectors = build_bootstrap_collectors(rt)

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
