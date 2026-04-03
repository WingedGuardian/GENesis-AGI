"""Agent Zero hosting adapter — formalizes the existing AZ integration.

AZ still owns the Flask app and calls bootstrap via its extension system
(``_00_genesis_bootstrap.py``).  This adapter provides the
``HostFrameworkAdapter`` interface for host detection, health reporting,
and lifecycle management.

This adapter is NOT required for AZ integration to work — the existing
extension system handles everything.  It exists so the hosting registry
can return a formal adapter reference regardless of mode.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flask import Flask

logger = logging.getLogger("genesis.hosting.agent_zero")


class AgentZeroAdapter:
    """Formal adapter wrapping AZ's existing integration."""

    name = "Agent Zero"

    async def bootstrap(self) -> None:
        """Verify runtime is bootstrapped (AZ handles this via extension)."""
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.instance()
        if not rt.is_bootstrapped:
            logger.warning(
                "Runtime not bootstrapped by AZ extension — bootstrapping in readonly mode",
            )
            await rt.bootstrap(mode="readonly")

    async def serve(self) -> None:
        """No-op — AZ owns the event loop and Flask app."""

    async def shutdown(self) -> None:
        """No-op — AZ owns shutdown."""

    def get_flask_app(self) -> Flask | None:
        """Return AZ's Flask app if accessible."""
        run_ui = sys.modules.get("__main__")
        return getattr(run_ui, "webapp", None)
