"""OpenClawAdapter — registers the /v1/chat/completions blueprint."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flask import Flask

logger = logging.getLogger("genesis.hosting.openclaw")


class OpenClawAdapter:
    """Registers Genesis as an OpenClaw LLM provider."""

    name = "OpenClaw"

    def register_blueprints(self, app: Flask) -> None:
        """Register the /v1/chat/completions blueprint on *app*."""
        from genesis.hosting.openclaw.completions import blueprint

        if "openclaw_completions" not in app.blueprints:
            app.register_blueprint(blueprint)
            logger.info("OpenClaw completions blueprint registered at /v1/chat/completions")
