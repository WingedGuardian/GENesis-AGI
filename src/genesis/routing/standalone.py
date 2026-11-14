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
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from genesis.routing.types import BudgetStatus

if TYPE_CHECKING:
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

_external_secret_keys: frozenset[str] | None = None


def configure_external_secrets(keys: frozenset[str]) -> None:
    """Freeze the dedicated external process policy before any bootstrap."""
    from genesis.caller_context import EXTERNAL_IDENTITY_ENV_VARS

    global _external_secret_keys
    selected = frozenset(keys) - EXTERNAL_IDENTITY_ENV_VARS - {
        "GENESIS_REPO_ROOT", "GENESIS_HOME", "SECRETS_PATH",
    }
    if _external_secret_keys is not None and _external_secret_keys != selected:
        raise RuntimeError("external secrets policy already configured")
    _external_secret_keys = selected
    for marker in EXTERNAL_IDENTITY_ENV_VARS:
        os.environ.pop(marker, None)


def load_external_secrets(path: Path) -> frozenset[str]:
    """Import selected missing keys, with inherited values winning interpolation.

    Isolate python-dotenv's private adapter here: the public dotenv_values uses
    file-first interpolation, even when its result is inserted with setdefault.
    Return parsed key names for the environment recorder, never values.
    """
    from dotenv.main import DotEnv, _load_dotenv_disabled

    if _external_secret_keys is None:
        raise RuntimeError("external secrets policy not configured")
    if _load_dotenv_disabled() or not path.is_file():
        return frozenset()
    values = DotEnv(str(path), override=False, encoding="utf-8").dict()
    for key, value in values.items():
        if key in _external_secret_keys and value is not None:
            os.environ.setdefault(key, value)
    return frozenset(values)


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

    Loads environment values but does not set the runtime singleton. Callers wanting it use
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
        if _external_secret_keys is not None:
            load_external_secrets(sp)
        elif sp.is_file():
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

        from genesis.routing.daily_budget import DailyBudgetLedger

        return Router(
            config=config,
            breakers=breakers,
            cost_tracker=cost_tracker,
            degradation=degradation,
            delegate=delegate,
            event_bus=None,
            dead_letter=None,
            # Read-only, like the breakers above: MCP children load a
            # SNAPSHOT of the server's daily-budget counters at process
            # start (a provider exhausted later in the day is still called
            # from long-lived children) and never write the shared state
            # file — WS-3c. Their own usage also goes uncounted. Both are
            # the undercount side, backstopped by the provider's 429s.
            daily_budget=DailyBudgetLedger(persist=False),
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
