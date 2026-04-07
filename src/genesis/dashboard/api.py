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


import genesis.dashboard.auth  # noqa: F401,E402 — registers before_request + auth routes
import genesis.dashboard.routes  # noqa: F401,E402 — triggers route registration via side effects

genesis_dashboard = blueprint

__all__ = ["genesis_dashboard", "blueprint", "dashboard_page"]
