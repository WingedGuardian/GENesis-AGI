"""Shared blueprint infrastructure for Genesis dashboard routes."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import TimeoutError as FuturesTimeoutError
from functools import wraps
from pathlib import Path

from flask import Blueprint

logger = logging.getLogger("genesis.dashboard")

# Substrings that identify an asyncio *loop-binding* RuntimeError — a coroutine
# or shared async object (aiosqlite connection, asyncio.Lock) reached on a loop
# other than the one it was created on, or with no running loop at all. These
# are the failures the loop-less fallback should degrade to 503; any OTHER
# RuntimeError (a real handler/dependency bug like RuntimeError("database gone"))
# must keep propagating to normal 500 error handling.
_LOOP_BINDING_SIGNATURES = (
    "different event loop",
    "different loop",
    "no current event loop",
    "no running event loop",
    "event loop is closed",
    "attached to a different",
    "bound to a different",
)


def _is_loop_binding_error(exc: RuntimeError) -> bool:
    msg = str(exc).lower()
    return any(sig in msg for sig in _LOOP_BINDING_SIGNATURES)


TEMPLATE_DIR = Path(__file__).parent / "templates"

blueprint = Blueprint(
    "genesis_dashboard",
    __name__,
    # Jinja search path for render_template — resolves relative to this package,
    # so /genesis renders identically under standalone AND Agent Zero hosting.
    template_folder="templates",
)


def _async_route(f=None, *, timeout: float | None = None):
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
    is configured (e.g. during unit tests). When a runtime loop IS
    configured but not (yet) running — the startup/shutdown window — the
    route answers 503 instead: shared async objects are bound to that loop,
    and running the handler anywhere else raises cross-loop errors that
    historically could take the whole server down.

    ``timeout`` (seconds) optionally bounds how long the Flask worker thread
    waits for the coroutine. On expiry the route returns HTTP 503 instead of
    blocking indefinitely, so a slow handler can never make the endpoint look
    dead to the dashboard or the Guardian's health probe. ``None`` (default)
    preserves the original unbounded behavior — required for long-running
    routes (reply waiters, approval waits). Usable bare (``@_async_route``)
    or parametrized (``@_async_route(timeout=15)``).
    """

    def decorate(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            from flask import current_app, jsonify

            loop = current_app.config.get("GENESIS_EVENT_LOOP")
            if loop is not None and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(fn(*args, **kwargs), loop)
                try:
                    return future.result(timeout=timeout)
                except FuturesTimeoutError:
                    # Cancel the coroutine so it doesn't keep running on the loop
                    # after we've returned — otherwise repeated polling stacks
                    # concurrent snapshots (run_coroutine_threadsafe futures
                    # schedule task cancellation on the loop thread-safely).
                    future.cancel()
                    logger.warning(
                        "async route %s exceeded %.1fs timeout — returning 503",
                        getattr(fn, "__name__", "?"), timeout or 0.0,
                    )
                    return jsonify(
                        {"status": "unavailable", "error": "request timed out"}
                    ), 503

            if loop is not None:
                # Runtime loop configured but not running (startup/shutdown
                # window). Shared async objects (aiosqlite connections,
                # asyncio locks) are bound to that loop — running the handler
                # on a fresh loop would raise mid-request. Same idiom as
                # voice_api / openclaw completions: answer 503 until ready.
                logger.warning(
                    "async route %s hit before runtime loop is running — returning 503",
                    getattr(fn, "__name__", "?"),
                )
                return jsonify(
                    {"status": "unavailable", "error": "runtime starting"}
                ), 503

            # Fallback for tests or pre-runtime contexts (no runtime loop
            # configured at all)
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(fn(*args, **kwargs))
            except RuntimeError as exc:
                # Only a loop-binding failure means "the handler touched a
                # shared async object bound to the (absent) runtime loop" —
                # aiosqlite raises "attached to a different loop" / "no current
                # event loop". This used to propagate and could crash the server
                # (observed exit 2 when a health poll landed during a broken
                # startup). Degrade THAT to 503; re-raise every other
                # RuntimeError so genuine handler/dependency bugs still surface
                # as 500s (esp. under embedded hosting, where this fallback is
                # the permanent request path).
                if not _is_loop_binding_error(exc):
                    raise
                logger.exception(
                    "async route %s failed in loop-less fallback — returning 503",
                    getattr(fn, "__name__", "?"),
                )
                return jsonify(
                    {"status": "unavailable", "error": "runtime not ready"}
                ), 503
            finally:
                loop.close()

        return wrapper

    return decorate(f) if f is not None else decorate
