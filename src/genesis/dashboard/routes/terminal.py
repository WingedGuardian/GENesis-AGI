"""Chat terminal routes — built-in web terminal via flask-sock.

Provides a WebSocket endpoint at ``/ws/terminal`` that bridges the
browser (xterm.js) to a PTY session.  Also provides a REST status
endpoint used by the dashboard Chat tab.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING

from flask import jsonify
from flask_sock import Sock

from genesis.dashboard._blueprint import blueprint
from genesis.dashboard.terminal_session import TerminalSession

if TYPE_CHECKING:
    from flask import Flask
    from simple_websocket import Server as WebSocket

logger = logging.getLogger(__name__)

# Module-level Sock instance — initialized via register_terminal_ws().
_sock = Sock()

# Concurrent session tracking
_active_sessions: set[str] = set()
_session_lock = threading.Lock()
_MAX_SESSIONS = 3


def register_terminal_ws(app: Flask) -> None:
    """Wire the WebSocket endpoint onto a Flask app.

    Called from both standalone adapter and AZ bootstrap plugin.
    Must be called AFTER blueprint registration (the REST routes
    are on the blueprint; the WebSocket route is on the app).
    """
    _sock.init_app(app)
    logger.info("Terminal WebSocket registered on /ws/terminal")


@_sock.route("/ws/terminal")
def terminal_ws(ws: WebSocket) -> None:
    """WebSocket handler — bridges xterm.js to a PTY session."""
    # Enforce session limit
    session_id = str(id(ws))
    with _session_lock:
        if len(_active_sessions) >= _MAX_SESSIONS:
            ws.close(message="Too many terminal sessions")
            return
        _active_sessions.add(session_id)

    session = TerminalSession()
    try:
        session.start()
    except Exception:
        logger.exception("Failed to start terminal session")
        with _session_lock:
            _active_sessions.discard(session_id)
        return

    closed = threading.Event()

    def _pty_reader() -> None:
        """Background thread: read pty output, send to browser."""
        try:
            while not closed.is_set():
                data = session.read(timeout=0.1)
                if data:
                    try:
                        ws.send(data)
                    except Exception:
                        break
                elif not session.is_alive():
                    break
        finally:
            closed.set()

    reader_thread = threading.Thread(target=_pty_reader, daemon=True)
    reader_thread.start()

    # Main thread: receive browser input, write to pty
    try:
        while not closed.is_set():
            try:
                msg = ws.receive(timeout=1)
            except TimeoutError:
                if not session.is_alive():
                    break
                continue
            if msg is None:
                break
            # Try parsing as JSON control message (resize)
            if isinstance(msg, str):
                try:
                    payload = json.loads(msg)
                    if "resize" in payload:
                        r = payload["resize"]
                        session.resize(int(r["rows"]), int(r["cols"]))
                        continue
                except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                    pass
                session.write(msg)
            elif isinstance(msg, bytes):
                session.write(msg.decode("utf-8", errors="replace"))
    finally:
        closed.set()
        reader_thread.join(timeout=2)
        session.close()
        with _session_lock:
            _active_sessions.discard(session_id)
        logger.info("Terminal WebSocket closed")


# ── REST status endpoint ─────────────────────────────────────────────

@blueprint.route("/api/genesis/terminal/status")
def terminal_status():
    """Return terminal type and active session count."""
    with _session_lock:
        active = len(_active_sessions)
    return jsonify({
        "type": "websocket",
        "endpoint": "/ws/terminal",
        "active_sessions": active,
        "max_sessions": _MAX_SESSIONS,
    })
