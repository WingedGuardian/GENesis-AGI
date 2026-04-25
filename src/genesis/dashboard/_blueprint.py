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
    """Decorator to run async Flask route handlers.

    When the Genesis runtime loop is available (stored in
    ``app.config["GENESIS_EVENT_LOOP"]`` by the standalone adapter), the
    coroutine is dispatched to that loop via
    :func:`asyncio.run_coroutine_threadsafe`.  This ensures shared async
    objects (aiosqlite connections, asyncio locks, ``tracked_task`` calls)
    operate on the same event loop that created them.

    Flask 3.x stores request/app context on :mod:`contextvars` and
    ``asyncio.Task`` copies the calling thread's context automatically, so
    ``request``, ``current_app``, ``jsonify`` etc. remain accessible inside
    the coroutine even though it executes on the runtime thread.

    Falls back to a per-request ``new_event_loop()`` when no runtime loop
    is configured (e.g. during unit tests).
    """

    @wraps(f)
    def wrapper(*args, **kwargs):
        from flask import current_app

        loop = current_app.config.get("GENESIS_EVENT_LOOP")
        if loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(f(*args, **kwargs), loop)
            return future.result()

        # Fallback for tests or pre-runtime contexts
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(f(*args, **kwargs))
        finally:
            loop.close()

    return wrapper
