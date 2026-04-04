"""Flask blueprint for Genesis UI overlay injection into AZ's web UI.

Serves static assets at /genesis-ui/ and injects <script>/<link> tags
into the AZ index.html response via an after_request hook.  This lets
Genesis rebrand, hide, and rewire AZ UI elements without modifying any
AZ core files.

AZ-specific — not registered in standalone mode.
"""

from __future__ import annotations

import logging
from pathlib import Path

from flask import Blueprint, Response, request, send_from_directory

logger = logging.getLogger("genesis.hosting.agent_zero.overlay")

STATIC_DIR = Path(__file__).parent / "static"

blueprint = Blueprint(
    "genesis_ui",
    __name__,
)

# ── Static asset serving ─────────────────────────────────────────────


@blueprint.route("/genesis-ui/<path:filename>")
def serve_static(filename: str):
    """Serve Genesis UI overlay assets."""
    return send_from_directory(str(STATIC_DIR), filename)


# ── HTML injection ───────────────────────────────────────────────────

_INJECT_TAGS = (
    '<link rel="stylesheet" href="/genesis-ui/genesis-overlay.css">\n'
    '<script type="module" src="/genesis-ui/genesis-overlay.js"></script>\n'
)


def register_injection(app) -> None:
    """Register an after_request hook that injects Genesis overlay assets.

    Called once from the bootstrap extension after the blueprint is
    registered.  The hook only touches HTML responses that contain
    ``</head>`` and haven't already been injected.
    """

    @app.after_request
    def _inject_genesis_overlay(response: Response) -> Response:
        # Only inject into the main index page — component fragments and
        # static files must not be touched (they use direct passthrough mode).
        if request.path != "/":
            return response
        ct = response.content_type or ""
        if "text/html" not in ct:
            return response
        try:
            html = response.get_data(as_text=True)
        except RuntimeError:
            return response  # direct passthrough mode — skip
        if "/genesis-ui/" in html or "</head>" not in html:
            return response

        html = html.replace("</head>", _INJECT_TAGS + "</head>")
        response.set_data(html)
        return response

    logger.info("Genesis UI overlay injection registered")
