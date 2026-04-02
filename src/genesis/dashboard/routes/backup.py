"""Backup monitoring routes — status, trigger, config."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from flask import jsonify

from genesis.dashboard._blueprint import blueprint

logger = logging.getLogger(__name__)

_HOME = Path.home()
_STATUS_FILE = _HOME / ".genesis" / "backup_status.json"
_BACKUP_SCRIPT = _HOME / "genesis" / "scripts" / "backup.sh"
_BACKUP_LOG = _HOME / "genesis" / "logs" / "backup.log"
_BACKUP_DIR = _HOME / "backups" / "genesis-backups"


@blueprint.route("/api/genesis/backup/status")
def backup_status():
    """Return last backup status from status file + cron info."""
    result = {"configured": _BACKUP_SCRIPT.is_file()}

    # Read structured status file
    if _STATUS_FILE.is_file():
        try:
            data = json.loads(_STATUS_FILE.read_text())
            result["last_backup"] = data
        except (json.JSONDecodeError, OSError):
            result["last_backup"] = None
    else:
        result["last_backup"] = None

    # Check cron schedule
    try:
        cron_output = subprocess.run(
            ["crontab", "-l"],
            capture_output=True, text=True, timeout=5,
        )
        if cron_output.returncode == 0:
            for line in cron_output.stdout.splitlines():
                if "backup.sh" in line and not line.strip().startswith("#"):
                    result["cron_schedule"] = line.strip()
                    break
            else:
                result["cron_schedule"] = None
        else:
            result["cron_schedule"] = None
    except (subprocess.TimeoutExpired, OSError):
        result["cron_schedule"] = None

    # Backup repo info
    if _BACKUP_DIR.is_dir():
        try:
            remote = subprocess.run(
                ["git", "-C", str(_BACKUP_DIR), "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=5,
            )
            result["repo"] = remote.stdout.strip() if remote.returncode == 0 else None
        except (subprocess.TimeoutExpired, OSError):
            result["repo"] = None
    else:
        result["repo"] = None

    return jsonify(result)


@blueprint.route("/api/genesis/backup/trigger", methods=["POST"])
def backup_trigger():
    """Trigger a manual backup run (async — returns immediately)."""
    if not _BACKUP_SCRIPT.is_file():
        return jsonify({"error": "Backup script not found"}), 404

    try:
        proc = subprocess.Popen(
            ["bash", str(_BACKUP_SCRIPT)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # reparent to init — avoids zombie accumulation
        )
        logger.info("Manual backup triggered (pid %d)", proc.pid)
        return jsonify({"status": "triggered", "pid": proc.pid})
    except Exception as exc:
        logger.error("Failed to trigger backup: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@blueprint.route("/api/genesis/backup/log")
def backup_log():
    """Return the last N lines of backup log."""
    if not _BACKUP_LOG.is_file():
        return jsonify({"lines": [], "error": "Log file not found"})

    try:
        lines = _BACKUP_LOG.read_text().splitlines()
        # Return last 50 lines
        return jsonify({"lines": lines[-50:]})
    except OSError as exc:
        return jsonify({"lines": [], "error": str(exc)})
