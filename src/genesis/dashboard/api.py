"""Flask blueprint for Genesis dashboard.

This module exists for backward compatibility. Routes are organized into
submodules under genesis.dashboard.routes.
"""

from __future__ import annotations

from flask import make_response, render_template, request

from genesis.dashboard._blueprint import blueprint


@blueprint.route("/genesis")
def dashboard_page():
    """Serve the Genesis dashboard HTML page.

    Rendered through Jinja (blueprint template_folder) so the template can be
    split into partials. ETag + conditional handling preserved manually —
    ``send_from_directory`` gave 304s for free; ``render_template`` does not.
    """
    resp = make_response(render_template("genesis_dashboard.html"))
    resp.headers["Cache-Control"] = "no-cache"
    resp.add_etag()
    return resp.make_conditional(request)


import genesis.dashboard.auth  # noqa: F401,E402 — registers before_request + auth routes
import genesis.dashboard.routes  # noqa: F401,E402 — triggers route registration via side effects

genesis_dashboard = blueprint

__all__ = ["genesis_dashboard", "blueprint", "dashboard_page"]
