"""Backup monitoring + configuration routes — status, trigger, log, config.

Configuration is split by responsibility: the destination/credential env vars
(GENESIS_BACKUP_*) are written through the hardened secrets writer reused from
``secrets.py``; the cron SCHEDULE is managed by ``scripts/manage_backup_cron.sh``
(never by editing the crontab inline from this web process).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import urllib.parse
from pathlib import Path

from flask import jsonify, request

from genesis.dashboard._blueprint import blueprint
from genesis.dashboard.auth import is_authenticated

logger = logging.getLogger(__name__)

_HOME = Path.home()
_STATUS_FILE = _HOME / ".genesis" / "backup_status.json"
_BACKUP_SCRIPT = _HOME / "genesis" / "scripts" / "backup.sh"
_CRON_SCRIPT = _HOME / "genesis" / "scripts" / "manage_backup_cron.sh"
_BACKUP_LOG = _HOME / "genesis" / "logs" / "backup.log"
_BACKUP_DIR = _HOME / "backups" / "genesis-backups"

_BACKENDS = {"none", "local", "smb"}
_CRON_FIELD_RE = re.compile(r"^[0-9*,/-]+$")
_NAS_RE = re.compile(r"^//[^/\s]+/[^\s]+$")
_REPO_RE = re.compile(r"^(https?://|git@|ssh://).+")


# ── Helpers ───────────────────────────────────────────────────────────

def _strip_url_creds(url: str | None) -> str | None:
    """Remove any embedded ``user:token@`` from an http(s) URL.

    The status/config reads are reachable unauthenticated; a backup repo URL
    that embeds a token must never be echoed back in the clear.
    """
    if not url:
        return url
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.netloc and "@" in parts.netloc:
            host = parts.netloc.split("@", 1)[1]
            return urllib.parse.urlunsplit(parts._replace(netloc=host))
    except ValueError:
        pass
    return url


def _current_cron_line() -> str | None:
    """The active (uncommented) backup.sh crontab line, if any."""
    try:
        out = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        s = line.strip()
        if "backup.sh" in s and not s.startswith("#"):
            return s
    return None


def _current_schedule() -> tuple[str | None, bool]:
    """(cron expression, enabled) for the backup job."""
    line = _current_cron_line()
    if not line:
        return None, False
    fields = line.split()
    if len(fields) >= 5:
        return " ".join(fields[:5]), True
    return None, True


def _valid_cron(expr: str) -> bool:
    fields = expr.split()
    return len(fields) == 5 and all(_CRON_FIELD_RE.match(f) for f in fields)


# ── Routes ────────────────────────────────────────────────────────────

@blueprint.route("/api/genesis/backup/status")
def backup_status():
    """Return last backup status from status file + cron + repo info."""
    result = {
        "configured": _BACKUP_SCRIPT.is_file(),
        "repo_configured": _BACKUP_DIR.is_dir(),
    }

    if _STATUS_FILE.is_file():
        try:
            result["last_backup"] = json.loads(_STATUS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            result["last_backup"] = None
    else:
        result["last_backup"] = None

    line = _current_cron_line()
    result["cron_schedule"] = line

    if _BACKUP_DIR.is_dir():
        try:
            remote = subprocess.run(
                ["git", "-C", str(_BACKUP_DIR), "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=5,
            )
            repo = remote.stdout.strip() if remote.returncode == 0 else None
            result["repo"] = _strip_url_creds(repo)
        except (subprocess.TimeoutExpired, OSError):
            result["repo"] = None
    else:
        result["repo"] = None

    return jsonify(result)


@blueprint.route("/api/genesis/backup/config")
def backup_config_get():
    """Current backup configuration for the dashboard form.

    Non-sensitive fields are always returned (with the repo URL credential-
    stripped). The NAS share/user are infra detail returned only to
    authenticated callers; passphrase/NAS password are NEVER returned — only
    a boolean indicating whether they are set.
    """
    from genesis.dashboard.routes.secrets import _key_value

    schedule, enabled = _current_schedule()
    result = {
        "repo": _strip_url_creds(_key_value("GENESIS_BACKUP_REPO")),
        "tier2_backend": _key_value("GENESIS_BACKUP_TIER2_BACKEND") or "none",
        "schedule": schedule,
        "schedule_enabled": enabled,
        "passphrase_set": bool(_key_value("GENESIS_BACKUP_PASSPHRASE")),
        "nas_pass_set": bool(_key_value("GENESIS_BACKUP_NAS_PASS")),
    }
    # Filesystem paths/shares are infra detail — only for authenticated callers.
    if is_authenticated():
        result["local_path"] = _key_value("GENESIS_BACKUP_LOCAL_PATH")
        result["nas"] = _key_value("GENESIS_BACKUP_NAS")
        result["nas_user"] = _key_value("GENESIS_BACKUP_NAS_USER")
    return jsonify(result)


@blueprint.route("/api/genesis/backup/config", methods=["POST"])
def backup_config_set():
    """Update backup destination/credentials (secrets.env) and schedule (cron).

    Env changes take effect on the next backup run (backup.sh sources
    secrets.env directly) — no server restart required.
    """
    # Privileged write (credentials + cron) — gate it. No-op when the dashboard
    # has no password configured (is_authenticated() returns True), so a
    # passwordless install is unaffected; a password-protected one is enforced.
    if not is_authenticated():
        return jsonify({"error": "authentication required"}), 401

    from genesis.dashboard.routes.secrets import _key_value, _update_secrets_file

    data = request.get_json(silent=True) or {}
    errors: list[str] = []
    warnings: list[str] = []
    env_updates: dict[str, str] = {}

    def _clean(val: str) -> str | None:
        """Reject control chars / overlong values; return the trimmed value."""
        v = val.strip()
        if "\n" in v or "\x00" in v:
            return None
        if len(v) > 500:
            return None
        return v

    repo = (data.get("repo") or "").strip()
    if repo:
        if not _REPO_RE.match(repo) or _clean(repo) is None:
            errors.append("repo must be an https://, ssh://, or git@ URL")
        else:
            env_updates["GENESIS_BACKUP_REPO"] = repo
            current = _key_value("GENESIS_BACKUP_REPO")
            if current and current != repo:
                warnings.append(
                    "Changing the repo URL does not migrate existing backup "
                    "history. Delete ~/backups/genesis-backups and re-clone, "
                    "or keep the old repo reachable for restores."
                )

    backend = (data.get("tier2_backend") or "").strip()
    if backend:
        if backend not in _BACKENDS:
            errors.append(f"tier2_backend must be one of {sorted(_BACKENDS)}")
        else:
            env_updates["GENESIS_BACKUP_TIER2_BACKEND"] = backend

    local_path = (data.get("local_path") or "").strip()
    if local_path:
        if not local_path.startswith("/") or _clean(local_path) is None:
            errors.append("local_path must be an absolute path")
        else:
            env_updates["GENESIS_BACKUP_LOCAL_PATH"] = local_path

    nas = (data.get("nas") or "").strip()
    if nas:
        if not _NAS_RE.match(nas):
            errors.append("nas must look like //host/share")
        else:
            env_updates["GENESIS_BACKUP_NAS"] = nas

    nas_user = (data.get("nas_user") or "").strip()
    if nas_user:
        if _clean(nas_user) is None:
            errors.append("nas_user is invalid")
        else:
            env_updates["GENESIS_BACKUP_NAS_USER"] = nas_user

    # Cross-field: a selected backend needs its destination (new or already set).
    if backend == "smb" and not (nas or _key_value("GENESIS_BACKUP_NAS")):
        errors.append("smb backend requires a NAS share (//host/share)")
    if backend == "local" and not (
        local_path or _key_value("GENESIS_BACKUP_LOCAL_PATH")
    ):
        errors.append("local backend requires a local_path")

    # Secrets — only written when a non-empty value is supplied, so leaving the
    # field blank never blanks an existing secret.
    nas_pass = data.get("nas_pass")
    if nas_pass:
        cleaned = _clean(nas_pass)
        if cleaned is None:
            errors.append("nas_pass is invalid")
        else:
            env_updates["GENESIS_BACKUP_NAS_PASS"] = cleaned

    passphrase = data.get("passphrase")
    if passphrase:
        cleaned = _clean(passphrase)
        if cleaned is None:
            errors.append("passphrase is invalid")
        else:
            env_updates["GENESIS_BACKUP_PASSPHRASE"] = cleaned
            current = _key_value("GENESIS_BACKUP_PASSPHRASE")
            if current and current != cleaned:
                warnings.append(
                    "Rotating the passphrase does NOT re-encrypt existing "
                    "backups. Keep the old passphrase until you have verified "
                    "a fresh backup with the new one."
                )

    # Schedule — install/remove the cron line via the wrapper script.
    cron_action: tuple[str, str | None] | None = None
    if "schedule_enabled" in data or data.get("schedule"):
        schedule = (data.get("schedule") or "").strip()
        if data.get("schedule_enabled", True):
            if not _valid_cron(schedule):
                errors.append(
                    "schedule must be a 5-field cron expression "
                    "(e.g. '0 */6 * * *')"
                )
            else:
                cron_action = ("install", schedule)
        else:
            cron_action = ("remove", None)

    if errors:
        return jsonify({"error": "Validation failed", "details": errors}), 422

    if env_updates:
        try:
            _update_secrets_file(env_updates)
            for k, v in env_updates.items():
                os.environ[k] = v
        except Exception:
            logger.error("Failed to write backup secrets", exc_info=True)
            return jsonify({"error": "Failed to write secrets.env"}), 500

    schedule_result: str | None = None
    if cron_action is not None:
        action, expr = cron_action
        cmd = ["bash", str(_CRON_SCRIPT), action]
        if expr:
            cmd.append(expr)
        try:
            # Local crontab op with no external watchdog — a hung crontab would
            # block this request thread, so a short bound is justified here.
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.error("Backup cron update failed: %s", exc, exc_info=True)
            return jsonify({"error": "Failed to update backup schedule"}), 500
        if proc.returncode != 0:
            logger.error("Backup cron update rc=%d: %s", proc.returncode,
                         proc.stderr.strip())
            return jsonify({
                "error": "Failed to update backup schedule",
                "details": proc.stderr.strip(),
            }), 500
        schedule_result = proc.stdout.strip()

    logger.info("Backup config updated: keys=%s schedule=%s",
                sorted(env_updates.keys()), cron_action[0] if cron_action else None)
    return jsonify({
        "status": "ok",
        "updated": sorted(env_updates.keys()),
        "schedule": schedule_result,
        "warnings": warnings,
        "needs_restart": False,
    })


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
        return jsonify({"lines": lines[-50:]})
    except OSError as exc:
        return jsonify({"lines": [], "error": str(exc)})
