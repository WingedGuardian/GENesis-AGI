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
        # 1. Bootstrap runtime (readonly — bridge owns the cognitive loop)
        try:
            from genesis.runtime import GenesisRuntime

            await GenesisRuntime.instance().bootstrap(mode="readonly")
        except ImportError:
            pass  # Genesis not installed
        except Exception:
            logger.exception("Genesis bootstrap failed")

        # 2. Locate AZ's Flask webapp
        webapp = None
        try:
            import sys

            run_ui = sys.modules.get("__main__")
            webapp = getattr(run_ui, "webapp", None)
            if webapp is None:
                import run_ui as run_ui_mod

                webapp = getattr(run_ui_mod, "webapp", None)

            if webapp is None:
                logger.warning("Could not find Flask webapp")
            else:
                # Legacy neural monitor — served at /genesis/monitor.
                # Health + pause routes come from the dashboard blueprint.
                from pathlib import Path

                from flask import send_from_directory

                tmpl_dir = Path(__file__).resolve().parent.parent.parent / "templates"

                _monitor_rules = {r.rule for r in webapp.url_map.iter_rules()}
                if "/genesis/monitor" not in _monitor_rules:
                    @webapp.route("/genesis/monitor")
                    def neural_monitor():
                        return send_from_directory(str(tmpl_dir), "neural_monitor.html")

        except Exception:
            logger.exception("Failed to initialize webapp")

        # 3. Register all Genesis blueprints via adapter
        if webapp is not None:
            try:
                from genesis.hosting.agent_zero.adapter import AgentZeroAdapter

                adapter = AgentZeroAdapter()
                adapter.register_blueprints(webapp)
                adapter.register_overlay(webapp)
            except Exception:
                logger.exception("Blueprint registration failed")
