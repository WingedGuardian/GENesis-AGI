"""Backup monitoring + configuration routes — status, trigger, log, config.

Configuration is split by responsibility: the destination/credential env vars
(GENESIS_BACKUP_*) are written through the hardened secrets writer reused from
``secrets.py``; the backup SCHEDULE is the ``genesis-backup.timer`` systemd USER
unit (the source of truth), managed via ``systemctl --user`` + an ``OnCalendar``
drop-in.

Why systemd, not crontab: ``genesis-server.service`` runs ``NoNewPrivileges=yes``,
which neutralises the setgid ``crontab`` binary — ``crontab -l`` returns
``Permission denied`` from inside the service, so the old crontab read/write path
was silently dead (the dashboard reported "Not scheduled" while cron backups ran).
``systemctl --user`` talks to the session manager over D-Bus (not setgid), so it
works under the sandbox, and the drop-in file lives under ``$HOME`` (writable via
the unit's ``ReadWritePaths=%h``). This completes the migration PR #907 began.
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
from genesis.util.systemd import systemctl_env

logger = logging.getLogger(__name__)

_HOME = Path.home()
_STATUS_FILE = _HOME / ".genesis" / "backup_status.json"
_BACKUP_SCRIPT = _HOME / "genesis" / "scripts" / "backup.sh"
_BACKUP_LOG = _HOME / "genesis" / "logs" / "backup.log"
_BACKUP_DIR = _HOME / "backups" / "genesis-backups"

# Systemd user units (source of truth for the backup schedule).
_TIMER_UNIT = "genesis-backup.timer"
_SERVICE_UNIT = "genesis-backup.service"
_TIMER_DROPIN = (
    _HOME / ".config" / "systemd" / "user"
    / "genesis-backup.timer.d" / "schedule.conf"
)

# Preset schedules exposed by the dashboard. The value is the SHORT OnCalendar
# spec we write into the drop-in; systemd normalises it on load to the long form
# below, which is what ``systemctl show -p TimersCalendar`` reports.
_INTERVAL_TO_CALENDAR = {
    "3h": "00/3:10",
    "6h": "00/6:10",
    "12h": "00/12:10",
    "daily": "04:10",
}
# Reverse map keyed on systemd's NORMALIZED OnCalendar form (verified via
# ``systemd-analyze calendar``). test_calendar_maps_are_consistent guards drift.
_CALENDAR_TO_INTERVAL = {
    "*-*-* 00/3:10:00": "3h",
    "*-*-* 00/6:10:00": "6h",
    "*-*-* 00/12:10:00": "12h",
    "*-*-* 04:10:00": "daily",
}

# The unauthenticated /status route echoes the parsed backup_status.json. Project
# it through this allowlist so a future field added to backup.sh's status line
# (e.g. an infra path) can never auto-leak. The raw off-site TARGET is
# deliberately NOT here — it comes from /config's auth-gated _key_value instead.
_STATUS_SAFE_FIELDS = frozenset({
    "timestamp", "success", "sqlite_lines", "qdrant_collections",
    "transcript_files", "memory_files", "secrets_encrypted", "duration_s",
    "failure_reason", "tier2_status", "offsite_confirmed", "tier2_backend",
    "snapshot_id", "snapshot_count", "pruned_count", "tier1_pushed",
})

_BACKENDS = {"none", "local", "smb"}
_NAS_RE = re.compile(r"^//[^/\s]+/[^\s]+$")
_REPO_RE = re.compile(r"^(https?://|git@|ssh://).+")
_ONCALENDAR_RE = re.compile(r"OnCalendar=(.+?)\s*;")


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


def _systemctl(*args: str, timeout: int = 5):
    """Run ``systemctl --user <args>`` with the D-Bus session env injected.

    Returns the CompletedProcess, or ``None`` on timeout/OSError. Works under the
    hardened genesis-server namespace because D-Bus (unlike setgid ``crontab``) is
    unaffected by ``NoNewPrivileges`` — the same path ``services.py`` already uses.
    """
    try:
        return subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True, text=True, timeout=timeout, env=systemctl_env(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return None


def _parse_show(stdout: str) -> dict[str, str]:
    """Parse ``systemctl show -p KEY`` ``KEY=VALUE`` lines into a dict."""
    props: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            props[key] = value
    return props


def _interval_from_calendar(timers_calendar: str | None) -> str | None:
    """Map systemd's ``TimersCalendar`` property back to a preset key.

    ``TimersCalendar`` looks like ``{ OnCalendar=*-*-* 00/6:10:00 ; next_elapse=…}``.
    Returns the preset key, ``"custom"`` for an unrecognised OnCalendar, or
    ``None`` when the property is absent/empty.
    """
    if not timers_calendar:
        return None
    m = _ONCALENDAR_RE.search(timers_calendar)
    if not m:
        return None
    return _CALENDAR_TO_INTERVAL.get(m.group(1).strip(), "custom")


def _timer_state() -> dict:
    """Backup schedule state read from the systemd user timer (source of truth).

    Best-effort: any systemctl failure degrades to a disabled/None reading rather
    than raising — the status route must never 500 on a schedule probe.
    """
    state = {
        "mechanism": "systemd-timer", "enabled": False, "active": False,
        "next_run": None, "last_trigger": None, "interval": None,
    }
    r = _systemctl("is-enabled", _TIMER_UNIT)
    if r is not None:
        state["enabled"] = r.stdout.strip() == "enabled"
    r = _systemctl("is-active", _TIMER_UNIT)
    if r is not None:
        state["active"] = r.stdout.strip() == "active"
    r = _systemctl("show", _TIMER_UNIT,
                   "-p", "NextElapseUSecRealtime",
                   "-p", "LastTriggerUSec",
                   "-p", "TimersCalendar")
    if r is not None and r.returncode == 0:
        props = _parse_show(r.stdout)
        state["next_run"] = props.get("NextElapseUSecRealtime") or None
        state["last_trigger"] = props.get("LastTriggerUSec") or None
        state["interval"] = _interval_from_calendar(props.get("TimersCalendar"))
    return state


def _set_timer_schedule(interval_key: str) -> bool:
    """Write the OnCalendar drop-in for a preset and reload systemd.

    The drop-in RESETS the additive base (an empty ``OnCalendar=`` line) before
    setting the new value — systemd treats multiple ``OnCalendar=`` as additive,
    so without the reset the template's 6h schedule would *also* keep firing.
    A bare ``daemon-reload`` is sufficient for an already-active timer to
    recompute its next elapse (verified) — no restart needed.
    """
    calendar = _INTERVAL_TO_CALENDAR.get(interval_key)
    if calendar is None:
        return False
    try:
        _TIMER_DROPIN.parent.mkdir(parents=True, exist_ok=True)
        _TIMER_DROPIN.write_text(
            f"[Timer]\nOnCalendar=\nOnCalendar={calendar}\n"
        )
    except OSError:
        logger.error("Failed to write backup timer drop-in", exc_info=True)
        return False
    r = _systemctl("daemon-reload", timeout=10)
    return r is not None and r.returncode == 0


def _set_timer_enabled(enabled: bool) -> bool:
    """Enable+start (``enable --now``) or disable+stop (``disable --now``) the
    backup timer. On an already-active timer ``enable --now`` is a no-op start
    (it does not restart), which is fine — the drop-in reload already recomputed
    the schedule."""
    verb = "enable" if enabled else "disable"
    r = _systemctl(verb, "--now", _TIMER_UNIT, timeout=10)
    return r is not None and r.returncode == 0


def _backup_repo_url() -> str | None:
    """The Tier-1 (GitHub) origin URL of the local backups clone, cred-stripped."""
    if not _BACKUP_DIR.is_dir():
        return None
    try:
        remote = subprocess.run(
            ["git", "-C", str(_BACKUP_DIR), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return _strip_url_creds(remote.stdout.strip()) if remote.returncode == 0 else None


def _destinations(status: dict | None, repo: str | None) -> dict:
    """Build the two-tier destination health view for the dashboard.

    Tier-1 = GitHub (git push); Tier-2 = off-site (none/local/smb). The Tier-2
    TARGET (NAS share / local path) is infra detail — included ONLY for
    authenticated callers, mirroring /config's gating. It is read from the
    (auth-gated) secrets, NEVER from the status file, so /status stays safe to
    serve unauthenticated.
    """
    from genesis.dashboard.routes.secrets import _key_value

    status = status or {}
    tier1 = {
        "repo": repo,
        "pushed": status.get("tier1_pushed"),
        "last": status.get("timestamp"),
    }
    backend = (status.get("tier2_backend")
               or _key_value("GENESIS_BACKUP_TIER2_BACKEND") or "none")
    tier2 = {
        "backend": backend,
        "status": status.get("tier2_status"),
        "confirmed": status.get("offsite_confirmed"),
        "snapshot_id": status.get("snapshot_id"),
        "snapshot_count": status.get("snapshot_count"),
    }
    if is_authenticated():
        if backend == "smb":
            tier2["target"] = _strip_url_creds(
                _key_value("GENESIS_BACKUP_NAS")) or None
        elif backend == "local":
            tier2["target"] = _key_value("GENESIS_BACKUP_LOCAL_PATH") or None
    return {"tier1": tier1, "tier2": tier2}


# ── Routes ────────────────────────────────────────────────────────────

@blueprint.route("/api/genesis/backup/status")
def backup_status():
    """Last backup status + schedule (systemd timer) + both destinations.

    Reachable unauthenticated, so the parsed status file is projected through
    ``_STATUS_SAFE_FIELDS`` and the Tier-2 target is withheld unless authenticated.
    """
    result = {
        "configured": _BACKUP_SCRIPT.is_file(),
        "repo_configured": _BACKUP_DIR.is_dir(),
    }

    last_backup: dict | None = None
    if _STATUS_FILE.is_file():
        try:
            raw = json.loads(_STATUS_FILE.read_text())
            if isinstance(raw, dict):
                # Allowlist projection — a future field in backup.sh's status
                # line cannot auto-leak through this unauthenticated route.
                last_backup = {k: v for k, v in raw.items()
                               if k in _STATUS_SAFE_FIELDS}
        except (json.JSONDecodeError, OSError):
            last_backup = None
    result["last_backup"] = last_backup

    result["repo"] = _backup_repo_url()
    result["schedule"] = _timer_state()
    result["destinations"] = _destinations(last_backup, result["repo"])

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

    timer = _timer_state()
    result = {
        "repo": _strip_url_creds(_key_value("GENESIS_BACKUP_REPO")),
        "tier2_backend": _key_value("GENESIS_BACKUP_TIER2_BACKEND") or "none",
        "schedule_enabled": timer["enabled"],
        # Preset key ("6h"/…), "custom" for a hand-edited OnCalendar, or None.
        "schedule_interval": timer["interval"],
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
    """Update backup destination/credentials (secrets.env) and schedule (timer).

    Env changes take effect on the next backup run (backup.sh sources
    secrets.env directly) — no server restart required. The schedule is applied
    immediately to the ``genesis-backup.timer`` systemd user unit.
    """
    # Privileged write (credentials + schedule) — gate it. No-op when the dashboard
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

    # Schedule — managed via the systemd user timer. Disabling must NOT require a
    # valid interval (a host on a hand-edited "custom" schedule must still be able
    # to turn backups off), so interval is validated only on the enable path.
    schedule_action: tuple[str, str | None] | None = None
    if "schedule_enabled" in data or "schedule_interval" in data:
        if data.get("schedule_enabled", True):
            interval = (data.get("schedule_interval") or "").strip()
            if interval and interval not in _INTERVAL_TO_CALENDAR:
                errors.append(
                    "schedule_interval must be one of "
                    f"{sorted(_INTERVAL_TO_CALENDAR)}"
                )
            else:
                schedule_action = ("enable", interval or None)
        else:
            schedule_action = ("disable", None)

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
    if schedule_action is not None:
        action, interval = schedule_action
        if action == "enable":
            # Write the schedule (if the user picked one) BEFORE enabling, so the
            # daemon-reload's recompute is already in place when the timer starts.
            if interval and not _set_timer_schedule(interval):
                return jsonify(
                    {"error": "Failed to write backup schedule (drop-in)"}), 500
            if not _set_timer_enabled(True):
                return jsonify(
                    {"error": "Schedule written but failed to enable the backup "
                              "timer — retry, or check `systemctl --user status "
                              "genesis-backup.timer`"}), 500
            schedule_result = "enabled"
        else:
            if not _set_timer_enabled(False):
                return jsonify({"error": "Failed to disable the backup timer"}), 500
            schedule_result = "disabled"

    logger.info("Backup config updated: keys=%s schedule=%s",
                sorted(env_updates.keys()),
                schedule_action[0] if schedule_action else None)
    return jsonify({
        "status": "ok",
        "updated": sorted(env_updates.keys()),
        "schedule": schedule_result,
        "warnings": warnings,
        "needs_restart": False,
    })


@blueprint.route("/api/genesis/backup/trigger", methods=["POST"])
def backup_trigger():
    """Trigger a manual backup run via the systemd service (async).

    Runs the SAME ``genesis-backup.service`` the timer fires, so Run-Now and the
    scheduled run are identical — and the service's un-hardened namespace has the
    gpg-agent socket + /tmp access backup.sh needs (the hardened genesis-server
    namespace does not, so the old in-process ``bash backup.sh`` was a latent bug).
    ``--no-block`` is REQUIRED: a ``Type=oneshot`` start otherwise blocks until the
    backup finishes (~5 min), hanging this request thread.
    """
    if not _BACKUP_SCRIPT.is_file():
        return jsonify({"error": "Backup script not found"}), 404

    r = _systemctl("start", "--no-block", _SERVICE_UNIT, timeout=10)
    if r is None or r.returncode != 0:
        err = (r.stderr.strip() if r is not None else "systemctl unavailable")
        logger.error("Failed to start %s: %s", _SERVICE_UNIT, err)
        return jsonify({"error": err or "Failed to start backup service"}), 500
    logger.info("Manual backup triggered via %s", _SERVICE_UNIT)
    return jsonify({"status": "triggered", "unit": _SERVICE_UNIT})


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
