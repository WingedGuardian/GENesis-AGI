"""Single-use approval HTTP handler for recovery confirmation.

Runs a tiny HTTP server on the host's Tailscale IP (port 8888). Telegram
messages include a one-click approval URL. User clicks from phone. No
bidirectional Telegram needed — Guardian only sends, never reads.

Uses stdlib only (http.server, secrets).
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from genesis.guardian.config import ApprovalConfig

logger = logging.getLogger(__name__)


class _ApprovalState:
    """Thread-safe shared state for the approval handler."""

    def __init__(self) -> None:
        self.pending_token: str | None = None
        self.approved: bool = False
        self.approved_at: str | None = None
        self.created_at: str | None = None
        self.expiry_s: int = 3600
        self._lock = threading.Lock()

    def create_token(self, expiry_s: int = 3600) -> str:
        with self._lock:
            self.pending_token = secrets.token_urlsafe(32)
            self.approved = False
            self.approved_at = None
            self.created_at = datetime.now(UTC).isoformat()
            self.expiry_s = expiry_s
            return self.pending_token

    def try_approve(self, token: str) -> bool:
        with self._lock:
            if self.pending_token is None or token != self.pending_token:
                return False
            if self.approved:
                return False  # Already used
            if self._is_expired():
                return False
            self.approved = True
            self.approved_at = datetime.now(UTC).isoformat()
            self.pending_token = None  # Invalidate after use
            return True

    def _is_expired(self) -> bool:
        if not self.created_at:
            return True
        try:
            created = datetime.fromisoformat(self.created_at)
            elapsed = (datetime.now(UTC) - created).total_seconds()
            return elapsed > self.expiry_s
        except (ValueError, TypeError):
            return True


# Module-level state shared between handler and caller
_state = _ApprovalState()


class _ApprovalHandler(BaseHTTPRequestHandler):
    """HTTP handler for approval URLs."""

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")

        if path.startswith("/approve/"):
            token = path[len("/approve/"):]
            if _state.try_approve(token):
                self._respond(200, {
                    "status": "approved",
                    "message": "Recovery approved. Guardian will proceed.",
                })
                logger.info("Recovery approved via web link")
            else:
                self._respond(404, {
                    "status": "invalid",
                    "message": "Token invalid, expired, or already used.",
                })
        elif path == "/health":
            self._respond(200, {"status": "ok"})
        else:
            self._respond(404, {"status": "not_found"})

    def _respond(self, code: int, body: dict) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode("utf-8"))

    def log_message(self, format: str, *args: object) -> None:
        # Suppress default stderr logging — use our logger
        logger.debug("HTTP: %s", format % args)


class ApprovalServer:
    """Manages the approval HTTP server lifecycle.

    Starts in a background thread, generates single-use tokens,
    and auto-shuts down after approval or timeout.
    """

    def __init__(self, config: ApprovalConfig) -> None:
        self._config = config
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def is_approved(self) -> bool:
        return _state.approved

    def start(self) -> str:
        """Start the approval server and return a new approval URL.

        Returns the full approval URL including token.
        """
        token = _state.create_token(expiry_s=self._config.token_expiry_s)

        host = self._config.bind_host or "0.0.0.0"  # noqa: S104
        port = self._config.port

        self._server = HTTPServer((host, port), _ApprovalHandler)
        self._server.timeout = 1  # Allow periodic shutdown checks

        self._thread = threading.Thread(
            target=self._serve,
            daemon=True,
            name="guardian-approval",
        )
        self._thread.start()

        # Build URL — use bind_host if specified, else generic
        url_host = self._config.bind_host or "localhost"
        url = f"http://{url_host}:{port}/approve/{token}"
        logger.info("Approval server started on %s:%d", host, port)
        return url

    def stop(self) -> None:
        """Stop the approval server."""
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        logger.debug("Approval server stopped")

    def wait_for_approval(self, timeout_s: float = 3600) -> bool:
        """Block until approval is received or timeout expires.

        Returns True if approved, False if timed out.
        """
        import time

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if _state.approved:
                return True
            time.sleep(1)
        return False

    def _serve(self) -> None:
        """Serve requests until shutdown."""
        if self._server:
            self._server.serve_forever()
