"""Init function: _init_health_data."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


def init(rt: GenesisRuntime) -> None:
    """Initialize health data service — reads from all other subsystems."""
    try:
        from genesis.observability.health_data import HealthDataService

        rt._health_data = HealthDataService(
            circuit_breakers=rt._circuit_breakers,
            routing_config=rt._router.config if rt._router else None,
            cost_tracker=rt._cost_tracker,
            cc_budget=rt._cc_budget_tracker,
            deferred_queue=rt._deferred_work_queue,
            dead_letter=rt._dead_letter_queue,
            db=rt._db,
            surplus_scheduler=rt._surplus_scheduler,
            learning_scheduler=rt._learning_scheduler,
            resilience_state_machine=rt._resilience_state_machine,
            activity_tracker=rt._activity_tracker,
        )

        from genesis.mcp.health_mcp import init_health_mcp

        init_health_mcp(
            rt._health_data,
            event_bus=rt._event_bus,
            activity_tracker=rt._activity_tracker,
            job_retry_registry=rt._job_retry_registry,
        )

        logger.info("Genesis health data service initialized")
    except ImportError:
        logger.warning("genesis.observability.health_data not available")
    except Exception:
        logger.exception("Failed to initialize health data service")
