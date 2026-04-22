"""Init function: direct session spawner.

Creates a dedicated CCInvoker + DirectSessionRunner and wires the MCP
tools.  Placed after ``cc_relay`` in bootstrap (needs session_manager)
but accesses ``outreach_pipeline`` lazily via runtime ref.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


async def init(rt: GenesisRuntime) -> None:
    """Initialize the direct session spawner."""
    if rt._session_manager is None:
        logger.warning("Session manager not available — skipping direct session")
        return

    try:
        from genesis.cc.direct_session import DirectSessionRunner
        from genesis.cc.invoker import CCInvoker
        from genesis.cc.session_config import SessionConfigBuilder

        # Dedicated invoker for the runner — avoids _active_proc race
        # under Semaphore(2) with the shared invoker (architect finding #5).
        # Uses simpler callbacks: status changes log only, no resilience
        # state machine wiring (the shared invoker handles that).
        async def _on_status_change(status_str: str) -> None:
            logger.info("Direct session invoker status: %s", status_str)

        invoker = CCInvoker(on_cc_status_change=_on_status_change)

        runner = DirectSessionRunner(
            invoker=invoker,
            session_manager=rt._session_manager,
            config_builder=SessionConfigBuilder(),
            runtime=rt,
        )
        rt._direct_session_runner = runner

        # Wire MCP tools
        from genesis.mcp.health.direct_session_tools import init_direct_session_tools
        init_direct_session_tools(runner)

        logger.info("Direct session spawner initialized")

    except Exception:
        logger.exception("Failed to initialize direct session spawner")
