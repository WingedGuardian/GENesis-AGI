"""Self-update routes — check for updates, view status, trigger update."""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import subprocess
from pathlib import Path

from flask import jsonify, request

from genesis.dashboard._blueprint import blueprint

logger = logging.getLogger(__name__)

_HOME = Path.home()
_GENESIS_ROOT = _HOME / "genesis"
_UPDATE_SCRIPT = _GENESIS_ROOT / "scripts" / "update.sh"
_DB_PATH = _GENESIS_ROOT / "data" / "genesis.db"
_FAILURE_FILE = _HOME / ".genesis" / "last_update_failure.json"


def _git(*args: str, timeout: int = 10) -> str | None:
    """Run a git command in the Genesis repo. Returns stdout or None on error."""
    try:
        result = subprocess.run(
            ["git", "-C", str(_GENESIS_ROOT), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _query_db(sql: str, params: tuple = ()) -> list[dict]:
    """Run a read-only query against genesis.db. Returns list of row dicts."""
    if not _DB_PATH.is_file():
        return []
    try:
        conn = sqlite3.connect(str(_DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except (sqlite3.Error, OSError):
        return []


@blueprint.route("/api/genesis/updates/status")
def update_status():
    """Current version, update availability, and last update result."""
    # Current version
    tag = _git("describe", "--tags", "--always") or "unknown"
    commit = _git("rev-parse", "--short", "HEAD") or "unknown"

    # Check for unresolved update_available observations
    update_available = None
    obs_rows = _query_db(
        "SELECT content, created_at FROM observations "
        "WHERE type = 'genesis_update_available' AND resolved = 0 "
        "ORDER BY created_at DESC LIMIT 1"
    )
    if obs_rows:
        try:
            content = json.loads(obs_rows[0]["content"])
            update_available = {
                "commits_behind": content.get("commits_behind"),
                "target_tag": content.get("target_tag"),
                "target_commit": content.get("target_commit"),
                "summary": content.get("summary"),
                "detected_at": obs_rows[0]["created_at"],
            }
        except (json.JSONDecodeError, KeyError):
            pass

    # Last update attempt
    last_update = None
    hist_rows = _query_db(
        "SELECT old_tag, new_tag, old_commit, new_commit, status, "
        "failure_reason, started_at, completed_at "
        "FROM update_history ORDER BY started_at DESC LIMIT 1"
    )
    if hist_rows:
        last_update = hist_rows[0]

    # Failure context file (persists until next successful update)
    last_failure = None
    if _FAILURE_FILE.is_file():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            last_failure = json.loads(_FAILURE_FILE.read_text())

    return jsonify({
        "current_version": tag,
        "current_commit": commit,
        "update_available": update_available,
        "last_update": last_update,
        "last_failure": last_failure,
        "update_script_found": _UPDATE_SCRIPT.is_file(),
    })


@blueprint.route("/api/genesis/updates/check", methods=["POST"])
def update_check():
    """Force an upstream check (git fetch + compare)."""
    # git fetch produces no stdout on success; None means the command failed
    if _git("fetch", "origin", "main", timeout=30) is None:
        return jsonify({"error": "git fetch failed"}), 502

    # Count commits behind
    behind_str = _git("rev-list", "--count", "HEAD..origin/main")
    commits_behind = int(behind_str) if behind_str and behind_str.isdigit() else 0

    target_tag = None
    if commits_behind > 0:
        target_tag = _git(
            "describe", "--tags", "--abbrev=0", "origin/main"
        )

    # Summary of what changed
    summary = None
    if commits_behind > 0:
        summary = _git(
            "log", "--oneline", "--no-merges", "HEAD..origin/main",
        )

    return jsonify({
        "commits_behind": commits_behind,
        "target_tag": target_tag,
        "summary": summary,
    })


@blueprint.route("/api/genesis/updates/apply", methods=["POST"])
def update_apply():
    """Trigger a CC-supervised update."""
    if not _UPDATE_SCRIPT.is_file():
        return jsonify({"error": "Update script not found"}), 404

    # Check if an update is already in progress (simple PID file guard)
    pid_file = _HOME / ".genesis" / "update_in_progress.pid"
    if pid_file.is_file():
        try:
            old_pid = int(pid_file.read_text().strip())
            # Check if process is still running (kill -0 returns 0 if alive)
            result = subprocess.run(["kill", "-0", str(old_pid)],
                                    capture_output=True, timeout=2)
            if result.returncode == 0:
                return jsonify({
                    "error": "Update already in progress",
                    "pid": old_pid,
                }), 409
            # Process is gone — stale PID file
            pid_file.unlink(missing_ok=True)
        except (ValueError, subprocess.TimeoutExpired, OSError):
            pid_file.unlink(missing_ok=True)

    use_cc = request.get_json(silent=True) or {}
    supervised = use_cc.get("supervised", True)

    if supervised:
        return _apply_supervised(pid_file)
    return _apply_direct(pid_file)


def _apply_direct(pid_file: Path) -> tuple:
    """Run update.sh directly (no CC supervision)."""
    try:
        proc = subprocess.Popen(
            ["bash", str(_UPDATE_SCRIPT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(proc.pid))
        logger.info("Direct update triggered (pid %d)", proc.pid)
        return jsonify({"status": "triggered", "pid": proc.pid, "supervised": False})
    except Exception as exc:
        logger.error("Failed to trigger update: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


_UPDATE_PROMPT = """\
You are managing a Genesis self-update. Run the update script and supervise the outcome.

1. Run: bash {update_script} 2>&1
   Capture and review the full output.

2. If exit 0 (success):
   - Verify health endpoint responds: curl -sf http://localhost:5000/api/genesis/health
   - Verify Genesis is importable: python -c "from genesis.runtime import GenesisRuntime"
   - Check version changed: git describe --tags --always
   - If verification passes, report success.
   - If anything is off, diagnose and fix.

3. If exit non-zero (failure/rollback):
   - Read {failure_file} for structured failure context.
   - Diagnose the root cause.
   - If it's a targeted fix (missing column, import error, config mismatch),
     apply the fix and retry the update.
   - If you can't resolve it confidently, write a clear diagnostic report and stop.

4. Any fixes you commit should use conventional commit format (fix: ...).

5. When done, write a one-line summary to {summary_file} with the outcome
   (e.g., "success: updated v3.0a3 -> v3.0a4" or "failed: <reason>").

Use mechanical judgment: targeted fixes yes, architectural decisions no.
If in doubt, stop and report rather than guessing.\
"""


def _apply_supervised(pid_file: Path) -> tuple:
    """Spawn a background CC session to run and supervise the update."""
    summary_file = _HOME / ".genesis" / "last_update_summary.txt"
    prompt = _UPDATE_PROMPT.format(
        update_script=_UPDATE_SCRIPT,
        failure_file=_FAILURE_FILE,
        summary_file=summary_file,
    )

    try:
        proc = subprocess.Popen(
            [
                "claude", "-p", prompt,
                "--model", "sonnet",
                "--dangerously-skip-permissions",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(_GENESIS_ROOT),
        )
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(proc.pid))
        logger.info("CC-supervised update triggered (pid %d)", proc.pid)
        return jsonify({
            "status": "triggered",
            "pid": proc.pid,
            "supervised": True,
        })
    except Exception as exc:
        logger.error("Failed to spawn CC update session: %s", exc, exc_info=True)
        # Fall back to direct update
        logger.info("Falling back to direct update")
        return _apply_direct(pid_file)
