"""Terminal integration routes — ttyd lifecycle management.

Provides API endpoints for the dashboard to check terminal status and
manage the ttyd process.  The actual terminal UI is served by ttyd on
its own port and embedded in the dashboard via iframe.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

from flask import jsonify, request

from genesis.dashboard._blueprint import blueprint

logger = logging.getLogger(__name__)

# ── ttyd process state (module-level singleton) ───────────────────────

_ttyd_proc: subprocess.Popen | None = None
_TTYD_DEFAULT_PORT = 7682


def _ttyd_binary() -> str | None:
    """Return path to ttyd binary, or None if not installed."""
    return shutil.which("ttyd")


def _is_ttyd_running() -> bool:
    """Check if our managed ttyd process is still alive."""
    if _ttyd_proc is None:
        return False
    return _ttyd_proc.poll() is None


def _start_ttyd(port: int = _TTYD_DEFAULT_PORT) -> dict:
    """Start ttyd serving a claude session in the Genesis project dir."""
    global _ttyd_proc  # noqa: PLW0603

    if _is_ttyd_running():
        return {"status": "already_running", "port": port, "pid": _ttyd_proc.pid}

    binary = _ttyd_binary()
    if binary is None:
        return {"status": "not_installed", "error": "ttyd not found — run: sudo apt install ttyd"}

    # Find claude binary
    claude_bin = shutil.which("claude")
    if claude_bin is None:
        return {"status": "error", "error": "claude CLI not found in PATH"}

    import pathlib

    genesis_dir = str(pathlib.Path.home() / "genesis")

    cmd = [
        binary,
        "--port", str(port),
        "--interface", "lo",       # localhost only
        "--writable",              # allow input
        claude_bin,
    ]

    try:
        _ttyd_proc = subprocess.Popen(
            cmd,
            cwd=genesis_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("Started ttyd on port %d (pid %d)", port, _ttyd_proc.pid)
        return {"status": "started", "port": port, "pid": _ttyd_proc.pid}
    except Exception as exc:
        logger.error("Failed to start ttyd: %s", exc, exc_info=True)
        return {"status": "error", "error": str(exc)}


def _stop_ttyd() -> dict:
    """Stop the managed ttyd process."""
    global _ttyd_proc  # noqa: PLW0603

    if not _is_ttyd_running():
        _ttyd_proc = None
        return {"status": "not_running"}

    _ttyd_proc.terminate()
    try:
        _ttyd_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _ttyd_proc.kill()
        _ttyd_proc.wait(timeout=3)

    pid = _ttyd_proc.pid
    _ttyd_proc = None
    logger.info("Stopped ttyd (pid %d)", pid)
    return {"status": "stopped", "pid": pid}


# ── Routes ────────────────────────────────────────────────────────────

@blueprint.route("/api/genesis/terminal/status")
def terminal_status():
    """Return terminal availability and status."""
    binary = _ttyd_binary()
    running = _is_ttyd_running()
    port = _TTYD_DEFAULT_PORT

    return jsonify({
        "installed": binary is not None,
        "running": running,
        "port": port if running else None,
        "pid": _ttyd_proc.pid if running else None,
        "url": f"http://localhost:{port}" if running else None,
    })


@blueprint.route("/api/genesis/terminal/start", methods=["POST"])
def terminal_start():
    """Start the ttyd terminal process."""
    port = request.json.get("port", _TTYD_DEFAULT_PORT) if request.is_json else _TTYD_DEFAULT_PORT
    if not isinstance(port, int) or not (1024 <= port <= 65535):
        return jsonify({"status": "error", "error": f"Invalid port {port} — must be 1024-65535"}), 400
    result = _start_ttyd(port=port)
    status_code = 200 if result["status"] in ("started", "already_running") else 500
    return jsonify(result), status_code


@blueprint.route("/api/genesis/terminal/stop", methods=["POST"])
def terminal_stop():
    """Stop the ttyd terminal process."""
    result = _stop_ttyd()
    return jsonify(result)
