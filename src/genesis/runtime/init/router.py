"""Init function: _init_router."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


def init(rt: GenesisRuntime) -> None:
    """Initialize the router stack: Router, circuit breakers, cost tracking, dead letter."""
    from genesis.env import repo_root

    config_path = repo_root() / "config" / "model_routing.yaml"
    try:
        from genesis.routing.circuit_breaker import CircuitBreakerRegistry
        from genesis.routing.config import load_config
        from genesis.routing.cost_tracker import CostTracker
        from genesis.routing.degradation import DegradationTracker
        from genesis.routing.litellm_delegate import LiteLLMDelegate
        from genesis.routing.router import Router

        if not config_path.exists():
            logger.error("Routing config not found at %s", config_path)
            return

        config = load_config(config_path)
        delegate = LiteLLMDelegate(config)
        breakers = CircuitBreakerRegistry(config.providers)
        cost_tracker = CostTracker(db=rt._db, event_bus=rt._event_bus)
        degradation = DegradationTracker()

        from genesis.routing.dead_letter import DeadLetterQueue

        dead_letter = DeadLetterQueue(db=rt._db, event_bus=rt._event_bus)

        rt._circuit_breakers = breakers
        rt._cost_tracker = cost_tracker
        rt._dead_letter_queue = dead_letter

        rt._router = Router(
            config=config,
            breakers=breakers,
            cost_tracker=cost_tracker,
            degradation=degradation,
            delegate=delegate,
            event_bus=rt._event_bus,
            dead_letter=dead_letter,
        )
        if rt._activity_tracker:
            rt._router.set_activity_tracker(rt._activity_tracker)
        logger.info(
            "Genesis router created (%d providers)", len(config.providers)
        )

        from genesis.resilience.deferred_work import DeferredWorkQueue

        rt._deferred_work_queue = DeferredWorkQueue(
            db=rt._db, event_bus=rt._event_bus,
        )

        from genesis.resilience.state import ResilienceStateMachine

        rt._resilience_state_machine = ResilienceStateMachine()
        logger.info("Resilience state machine created")

        if rt._awareness_loop is not None:
            rt._awareness_loop.set_deferred_queue(rt._deferred_work_queue)
            rt._awareness_loop.set_resilience_state_machine(rt._resilience_state_machine)
            if rt._circuit_breakers is not None:
                rt._awareness_loop.set_circuit_breakers(rt._circuit_breakers)
            logger.info("Deferred queue + resilience state machine + circuit breakers injected into awareness loop")

    except ImportError:
        logger.warning("genesis.routing not available")
    except Exception:
        logger.exception("Failed to initialize router")
