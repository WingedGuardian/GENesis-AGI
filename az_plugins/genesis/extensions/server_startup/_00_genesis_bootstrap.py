"""Genesis server startup bootstrap.

Fires from run_ui.py:init_a0() BEFORE any agent is created, so Genesis
background infrastructure (awareness loop, learning scheduler, inbox monitor)
starts immediately — not on first chat message.

This is the ONLY entry point for GenesisRuntime.bootstrap().  The agent_init
extensions just copy references from the singleton to self.agent for backward
compatibility.
"""

import logging

from python.helpers.extension import Extension

logger = logging.getLogger("genesis.bootstrap")


class GenesisBootstrap(Extension):
    async def execute(self, **kwargs):
        try:
            from genesis.runtime import GenesisRuntime

            # AZ uses readonly mode — the bridge process owns the cognitive
            # loop (awareness tick, schedulers, learning).  AZ needs DB, router,
            # memory, perception, health_data for dashboard API + extensions.
            await GenesisRuntime.instance().bootstrap(mode="readonly")
        except ImportError:
            pass  # Genesis not installed
        except Exception:
            logger.exception("Genesis bootstrap failed")

        # Register health API blueprint on the Flask app
        webapp = None
        try:
            import sys
            # Access the module-level webapp from run_ui
            run_ui = sys.modules.get("__main__")
            webapp = getattr(run_ui, "webapp", None)
            if webapp is None:
                # Try importing directly
                import run_ui as run_ui_mod
                webapp = getattr(run_ui_mod, "webapp", None)

            if webapp is not None:
                import importlib.util
                from pathlib import Path

                spec = importlib.util.spec_from_file_location(
                    "genesis_api_health",
                    Path(__file__).resolve().parent.parent.parent / "api_health.py",
                )
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                blueprint = mod.blueprint

                # Avoid duplicate registration
                if "genesis_health" not in webapp.blueprints:
                    webapp.register_blueprint(blueprint)
                    logger.info("Genesis health blueprint registered")
            else:
                logger.warning("Could not find Flask webapp for health blueprint")
        except Exception:
            logger.exception("Failed to register health blueprint")

        # Register Genesis dashboard blueprint
        try:
            if webapp is not None:
                from genesis.dashboard.api import blueprint as dash_bp

                if "genesis_dashboard" not in webapp.blueprints:
                    webapp.register_blueprint(dash_bp)
                    logger.info("Genesis dashboard blueprint registered")

                # Start dashboard heartbeat
                try:
                    from genesis.dashboard.heartbeat import DashboardHeartbeat

                    _heartbeat = DashboardHeartbeat(interval_seconds=60)
                    _heartbeat.start()
                except Exception:
                    logger.exception("Failed to start dashboard heartbeat")
        except Exception:
            logger.exception("Failed to register dashboard blueprint")

        # Register Genesis UI overlay blueprint
        try:
            if webapp is not None:
                from genesis.ui.blueprint import blueprint as ui_bp
                from genesis.ui.blueprint import register_injection

                if "genesis_ui" not in webapp.blueprints:
                    webapp.register_blueprint(ui_bp)
                    register_injection(webapp)
                    logger.info("Genesis UI overlay blueprint registered")
        except Exception:
            logger.exception("Failed to register UI overlay blueprint")

        # Register outreach API blueprint
        try:
            if webapp is not None:
                from genesis.outreach.api import init_outreach_api, outreach_api
                from genesis.runtime import GenesisRuntime

                rt = GenesisRuntime.instance()
                init_outreach_api(db=rt.db)

                if "outreach_api" not in webapp.blueprints:
                    webapp.register_blueprint(outreach_api)
                    logger.info("Genesis outreach blueprint registered")
        except Exception:
            logger.exception("Failed to register outreach blueprint")
