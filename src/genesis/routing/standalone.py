"""Standalone router bootstrap for MCP child processes.

MCP servers run as CC child processes without a fully bootstrapped
GenesisRuntime.  This module provides a lightweight router that can
make LLM calls via litellm without requiring a database connection,
event bus, or resilience state machine.

The full router in the genesis-server process is unaffected -- if
rt._router is already set, create_standalone_router() is a no-op.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from genesis.routing.types import BudgetStatus

if TYPE_CHECKING:
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)


class NullCostTracker:
    """Drop-in for CostTracker that accepts calls without DB writes.

    The Router accesses ``self.cost_tracker.db`` for neural monitor
    recording.  Setting ``db = None`` causes those code paths to
    skip gracefully (Router guards with ``if self.cost_tracker.db``).
    """

    db = None

    async def check_budget(self, *, task_id: str | None = None) -> BudgetStatus:
        return BudgetStatus.UNDER_LIMIT

    async def record(
        self,
        call_site_id: str,
        provider: str,
        result: Any,  # CallResult at runtime
        *,
        cost_known: bool = True,
    ) -> None:
        pass


def _build_standalone_router() -> Router | None:
    """Construct a lightweight, DB-free ``Router`` with real provider access.

    Loads secrets + routing config from disk and wires a Router with no
    DB-dependent components (read-only breakers, null cost tracker, no event
    bus / dead-letter). Returns the ``Router``, or ``None`` if construction
    fails (missing config/secrets) — never raises.

    Touches NO global state. Callers wanting the process-wide singleton use
    ``create_standalone_router()``; callers that just need a router instance
    (e.g. the offline attention runner's ``--l15`` path) call this directly and
    own the returned object.
    """
    try:
        from dotenv import load_dotenv

        from genesis.env import repo_root, secrets_path
        from genesis.routing.circuit_breaker import CircuitBreakerRegistry
        from genesis.routing.config import load_config
        from genesis.routing.degradation import DegradationTracker
        from genesis.routing.litellm_delegate import LiteLLMDelegate
        from genesis.routing.router import Router

        # 1. Load API keys into environment
        sp = secrets_path()
        if sp.is_file():
            load_dotenv(str(sp), override=True)

        # 2. Load routing config
        config_path = repo_root() / "config" / "model_routing.yaml"
        config = load_config(config_path, check_api_keys=False)

        # 3. Construct router-lite
        delegate = LiteLLMDelegate(config)
        # Read-only: MCP child processes load the server's breaker state but must
        # not write the shared state file (only the server owns it) — WS-3c.
        breakers = CircuitBreakerRegistry(config.providers, persist=False)
        degradation = DegradationTracker(resilience_state=None)
        cost_tracker = NullCostTracker()

        return Router(
            config=config,
            breakers=breakers,
            cost_tracker=cost_tracker,
            degradation=degradation,
            delegate=delegate,
            event_bus=None,
            dead_letter=None,
        )
    except Exception:
        logger.warning(
            "Failed to build standalone router -- "
            "LLM-dependent tools will be unavailable",
            exc_info=True,
        )
        return None


def create_standalone_router() -> None:
    """Bootstrap a lightweight router on the GenesisRuntime singleton.

    Safe to call multiple times -- skips if a router is already set. Delegates
    construction to ``_build_standalone_router()`` and assigns the result to the
    singleton; on build failure the singleton's ``_router`` stays ``None``.
    """
    from genesis.runtime._core import GenesisRuntime

    rt = GenesisRuntime.instance()
    if rt._router is not None:
        return

    router = _build_standalone_router()
    if router is not None:
        rt._router = router
        logger.info("Standalone router bootstrapped for MCP server")
