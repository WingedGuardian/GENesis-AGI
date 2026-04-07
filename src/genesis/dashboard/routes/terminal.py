"""Chat terminal routes — built-in web terminal via flask-sock.

Provides a WebSocket endpoint at ``/ws/terminal`` that bridges the
browser (xterm.js) to a PTY session.  Also provides a REST status
endpoint used by the dashboard Chat tab.

If flask-sock is not installed, the WebSocket endpoint is unavailable
but the dashboard still loads (graceful degradation).
"""

from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING

from flask import jsonify

from genesis.dashboard._blueprint import blueprint

try:
    from flask_sock import Sock

    from genesis.dashboard.terminal_session import TerminalSession

    _FLASK_SOCK_AVAILABLE = True
except ImportError:
    _FLASK_SOCK_AVAILABLE = False

if TYPE_CHECKING:
    from flask import Flask
    from simple_websocket import Server as WebSocket

logger = logging.getLogger(__name__)

# Concurrent session tracking
_active_sessions: set[str] = set()
_session_lock = threading.Lock()
_MAX_SESSIONS = 3


def register_terminal_ws(app: Flask) -> None:
    """Wire the WebSocket endpoint onto a Flask app.

    Called from the standalone adapter during bootstrap.
    Must be called AFTER blueprint registration (the REST routes
    are on the blueprint; the WebSocket route is on the app).
    """
    if not _FLASK_SOCK_AVAILABLE:
        logger.warning("flask-sock not installed — terminal WebSocket disabled")
        return

    sock = Sock(app)

    @sock.route("/ws/terminal")
    def terminal_ws(ws: WebSocket) -> None:
        """WebSocket handler — bridges xterm.js to a PTY session."""
        # Auth check — WebSocket runs in request context, can access session
        from genesis.dashboard.auth import get_dashboard_password, is_authenticated

        if get_dashboard_password() and not is_authenticated():
            ws.close(message="Authentication required")
            return

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

        try:
            while not closed.is_set():
                try:
                    msg = ws.receive(timeout=1)
                except TimeoutError:
                    msg = None
                if msg is None:
                    # simple_websocket >=1.0 returns None on timeout
                    # (older versions raised TimeoutError)
                    if not session.is_alive():
                        break
                    continue
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

    logger.info("Terminal WebSocket registered on /ws/terminal")


# ── REST + page routes ────────────────────────────────────────────────

@blueprint.route("/api/genesis/terminal/status")
def terminal_status():
    """Return terminal type and active session count."""
    with _session_lock:
        active = len(_active_sessions)
    return jsonify({
        "type": "websocket",
        "endpoint": "/ws/terminal",
        "available": _FLASK_SOCK_AVAILABLE,
        "active_sessions": active,
        "max_sessions": _MAX_SESSIONS,
    })


@blueprint.route("/vendor/xterm/<path:filename>")
def vendor_xterm_static(filename):
    """Serve xterm.js vendor assets for the terminal page.

    Scoped to /vendor/xterm/ only — must NOT catch all /vendor/* requests
    because AZ's Flask app serves its own vendor assets (Google icons, etc.)
    from a different static directory.
    """
    from pathlib import Path

    from flask import send_from_directory

    xterm_dir = Path(__file__).resolve().parent.parent / "webui" / "vendor" / "xterm"
    return send_from_directory(str(xterm_dir), filename)


@blueprint.route("/genesis/terminal")
def terminal_page():
    """Standalone terminal page — opened in a new window from the Chat tab."""
    from flask import render_template_string

    return render_template_string(_TERMINAL_PAGE_HTML)


_TERMINAL_PAGE_HTML = """\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Genesis Terminal</title>
  <link rel="stylesheet" href="/vendor/xterm/xterm.css">
  <script src="/vendor/xterm/xterm.js"></script>
  <script src="/vendor/xterm/addon-fit.js"></script>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    html, body { width: 100%; height: 100%; overflow: hidden; background: #1a1a2e; }
    #terminal { width: 100%; height: 100%; }
  </style>
</head>
<body>
  <div id="terminal"></div>
  <script>
    const term = new Terminal({
      cursorBlink: true,
      fontSize: 14,
      fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
      theme: { background: "#1a1a2e", foreground: "#e0e0e0", cursor: "#66bb6a" },
    });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(document.getElementById("terminal"));
    fit.fit();

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/terminal`);
    ws.binaryType = "arraybuffer";

    ws.onopen = () => {
      const dims = fit.proposeDimensions();
      if (dims) ws.send(JSON.stringify({ resize: { rows: dims.rows, cols: dims.cols } }));
      // Prefill Claude Code launch command; user chooses when to execute.
      setTimeout(() => ws.send("claude --dangerously-skip-permissions"), 500);
    };

    ws.onmessage = (evt) => {
      if (evt.data instanceof ArrayBuffer) {
        term.write(new Uint8Array(evt.data));
      } else {
        term.write(evt.data);
      }
    };

    ws.onclose = () => {
      term.write("\\r\\n\\x1b[90m[Connection closed]\\x1b[0m\\r\\n");
    };

    term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(data);
    });

    term.onResize(({ rows, cols }) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ resize: { rows, cols } }));
      }
    });

    window.addEventListener("resize", () => fit.fit());
  </script>
</body>
</html>
"""
