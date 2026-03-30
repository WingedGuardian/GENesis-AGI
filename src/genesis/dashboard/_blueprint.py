"""Shared blueprint infrastructure for Genesis dashboard routes."""

from __future__ import annotations

import asyncio
import logging
from functools import wraps
from pathlib import Path

from flask import Blueprint

logger = logging.getLogger("genesis.dashboard")

TEMPLATE_DIR = Path(__file__).parent / "templates"

blueprint = Blueprint(
    "genesis_dashboard",
    __name__,
)


def _async_route(f):
    """Decorator to run async Flask route handlers."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(f(*args, **kwargs))
        finally:
            loop.close()

    return wrapper
