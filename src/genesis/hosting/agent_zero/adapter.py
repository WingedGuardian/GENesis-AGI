"""Agent Zero hosting adapter.

AZ still owns the Flask app and calls bootstrap via its extension system
(``_00_genesis_bootstrap.py``).  This adapter provides the
``HostFrameworkAdapter`` interface for host detection, health reporting,
and lifecycle management.

This adapter is NOT required for AZ integration to function — the existing
extension system handles everything.  It exists so the hosting registry
can return a formal adapter reference regardless of mode, and to centralise
blueprint registration logic that was previously duplicated between
``_00_genesis_bootstrap.py`` and ``standalone.py``.
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

    def register_blueprints(self, app: Flask) -> None:
        """Register all Genesis blueprints on a Flask app.

        Centralises the registration logic previously scattered between
        ``_00_genesis_bootstrap.py`` and ``standalone.py``.  Each blueprint
        is wrapped in its own try/except so one failure doesn't block the rest.
        """
        # Dashboard blueprint (all /api/genesis/* routes, SSE stream, etc.)
        try:
            from genesis.dashboard.api import blueprint as dash_bp

            if "genesis_dashboard" not in app.blueprints:
                app.register_blueprint(dash_bp)
                logger.info("Dashboard blueprint registered")

                try:
                    from genesis.dashboard.heartbeat import DashboardHeartbeat

                    DashboardHeartbeat(interval_seconds=60).start()
                except Exception:
                    logger.exception("Failed to start dashboard heartbeat")
        except Exception:
            logger.exception("Failed to register dashboard blueprint")

        # Terminal WebSocket
        try:
            from genesis.dashboard.routes.terminal import register_terminal_ws

            register_terminal_ws(app)
        except Exception:
            logger.exception("Failed to register terminal WebSocket")

        # Outreach API blueprint
        try:
            from genesis.outreach.api import init_outreach_api, outreach_api
            from genesis.runtime import GenesisRuntime

            rt = GenesisRuntime.instance()
            if rt.db:
                init_outreach_api(db=rt.db)

            if "outreach_api" not in app.blueprints:
                app.register_blueprint(outreach_api)
                logger.info("Outreach API blueprint registered")
        except Exception:
            logger.exception("Failed to register outreach blueprint")

    def register_overlay(self, app: Flask) -> None:
        """Register AZ-specific UI overlay (skip in standalone mode).

        Registers the genesis_ui blueprint (static assets + HTML injection)
        that rewires AZ's web UI to show Genesis branding and data.
        """
        try:
            from genesis.hosting.agent_zero.overlay import blueprint as ui_bp
            from genesis.hosting.agent_zero.overlay import register_injection

            if "genesis_ui" not in app.blueprints:
                app.register_blueprint(ui_bp)
                register_injection(app)
                logger.info("Genesis UI overlay blueprint registered")
        except Exception:
            logger.exception("Failed to register UI overlay blueprint")
