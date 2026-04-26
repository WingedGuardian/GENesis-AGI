"""Starlette WebSocket handler for the built-in terminal.

Used in AZ mode where Flask runs inside WSGIMiddleware (which doesn't
support WebSocket).  The Starlette route is added directly to AZ's
route list in run_ui.py, bypassing the WSGI layer entirely.

In standalone mode, flask-sock handles WebSocket natively — this
module is not used.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from genesis.dashboard.terminal_session import TerminalSession
from genesis.util.tasks import tracked_task

logger = logging.getLogger(__name__)

# Concurrent session tracking (shared with routes/terminal.py via import)
_active_sessions: set[str] = set()
_session_lock = asyncio.Lock()
_MAX_SESSIONS = 3


async def terminal_ws_endpoint(websocket) -> None:  # noqa: C901
    """Starlette WebSocket endpoint — bridges xterm.js to a PTY session."""
    await websocket.accept()

    session_id = str(id(websocket))
    async with _session_lock:
        if len(_active_sessions) >= _MAX_SESSIONS:
            await websocket.close(code=1013, reason="Too many terminal sessions")
            return
        _active_sessions.add(session_id)

    session = TerminalSession()
    try:
        session.start()
    except Exception:
        logger.exception("Failed to start terminal session")
        async with _session_lock:
            _active_sessions.discard(session_id)
        await websocket.close(code=1011)
        return

    closed = asyncio.Event()

    async def _pty_reader() -> None:
        """Read pty output in executor thread, send to browser."""
        loop = asyncio.get_event_loop()
        try:
            while not closed.is_set():
                data = await loop.run_in_executor(None, session.read, 0.1)
                if data:
                    try:
                        await websocket.send_bytes(data)
                    except Exception:
                        break
                elif not session.is_alive():
                    break
        finally:
            closed.set()

    reader_task = tracked_task(_pty_reader(), name="pty-reader", logger=logger)

    try:
        while not closed.is_set():
            try:
                msg = await asyncio.wait_for(
                    websocket.receive_text(), timeout=1.0,
                )
            except TimeoutError:
                if not session.is_alive():
                    break
                continue
            except Exception:
                break

            # Try parsing as JSON control message (resize)
            try:
                payload = json.loads(msg)
                if "resize" in payload:
                    r = payload["resize"]
                    session.resize(int(r["rows"]), int(r["cols"]))
                    continue
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                pass
            session.write(msg)
    finally:
        closed.set()
        reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader_task
        session.close()
        async with _session_lock:
            _active_sessions.discard(session_id)
        logger.info("Terminal WebSocket closed (ASGI)")
