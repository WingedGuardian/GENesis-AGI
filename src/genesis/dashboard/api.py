"""Flask blueprint for Genesis dashboard.

This module exists for backward compatibility. Routes are organized into
submodules under genesis.dashboard.routes.
"""

from __future__ import annotations

from pathlib import Path

from flask import send_from_directory

from genesis.dashboard._blueprint import blueprint

TEMPLATE_DIR = Path(__file__).parent / "templates"


@blueprint.route("/genesis")
def dashboard_page():
    """Serve the Genesis dashboard HTML page."""
    return send_from_directory(str(TEMPLATE_DIR), "genesis_dashboard.html")


from genesis.dashboard.routes import (  # noqa: F401,E402
    activity,
    budget,
    config,
    errors,
    events,
    health,
    modules,
    outreach,
    providers,
    recon,
    resolution,
    routing,
    services,
    state,
    vitals,
)

genesis_dashboard = blueprint

__all__ = ["genesis_dashboard", "blueprint", "dashboard_page"]
