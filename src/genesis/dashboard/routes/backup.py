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
from datetime import UTC, datetime
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
_TIMER_DROPIN = _HOME / ".config" / "systemd" / "user" / "genesis-backup.timer.d" / "schedule.conf"

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
_STATUS_SAFE_FIELDS = frozenset(
    {
        "timestamp",
        "success",
        "sqlite_lines",
        "qdrant_collections",
        "transcript_files",
        "memory_files",
        "secrets_encrypted",
        "duration_s",
        "failure_reason",
        "tier2_status",
        "offsite_confirmed",
        "tier2_backend",
        "snapshot_id",
        "snapshot_count",
        "pruned_count",
        "tier1_pushed",
    }
)

_BACKENDS = {"none", "local", "smb"}
_NAS_RE = re.compile(r"^//[^/\s]+/[^\s]+$")
_REPO_RE = re.compile(r"^(https?://|git@|ssh://).+")
_ONCALENDAR_RE = re.compile(r"OnCalendar=(.+?)\s*;")
_SYSTEMD_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


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
            capture_output=True,
            text=True,
            timeout=timeout,
            env=systemctl_env(),
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
    than raising — the status route must never 500 on a schedule probe. That
    degraded reading is indistinguishable from a genuinely-disabled timer UNLESS
    a caller checks ``probe_ok`` first — ``False`` means "unknown", not
    "disabled"/"overdue-by-the-loose-fallback"; see its use in `_backup_health`.
    """
    state = {
        "mechanism": "systemd-timer",
        "enabled": False,
        "active": False,
        "next_run": None,
        "last_trigger": None,
        "interval": None,
        "probe_ok": True,
    }
    r = _systemctl("is-enabled", _TIMER_UNIT)
    if r is not None:
        state["enabled"] = r.stdout.strip() == "enabled"
    else:
        state["probe_ok"] = False
    r = _systemctl("is-active", _TIMER_UNIT)
    if r is not None:
        state["active"] = r.stdout.strip() == "active"
    else:
        state["probe_ok"] = False
    r = _systemctl(
        "show",
        _TIMER_UNIT,
        "-p",
        "NextElapseUSecRealtime",
        "-p",
        "LastTriggerUSec",
        "-p",
        "TimersCalendar",
    )
    if r is not None and r.returncode == 0:
        props = _parse_show(r.stdout)
        state["next_run"] = props.get("NextElapseUSecRealtime") or None
        state["last_trigger"] = props.get("LastTriggerUSec") or None
        state["interval"] = _interval_from_calendar(props.get("TimersCalendar"))
    else:
        state["probe_ok"] = False
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
        _TIMER_DROPIN.write_text(f"[Timer]\nOnCalendar=\nOnCalendar={calendar}\n")
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
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return _strip_url_creds(remote.stdout.strip()) if remote.returncode == 0 else None


def _resolved_backend() -> str:
    """The active Tier-2 backend, replicating ``backup_backends.sh`` resolution.

    An explicit ``GENESIS_BACKUP_TIER2_BACKEND`` wins; otherwise a configured
    ``GENESIS_BACKUP_NAS`` with no selector means ``smb`` (the documented
    backward-compat, ``scripts/lib/backup_backends.sh:_backend_resolve``); else
    ``none``. Without this, a NAS-only install (the common legacy setup) reads as
    ``none`` in the UI even while off-site backups succeed — and a config save
    would then write ``TIER2_BACKEND=none`` and silently disable them.
    """
    from genesis.dashboard.routes.secrets import _key_value

    b = (_key_value("GENESIS_BACKUP_TIER2_BACKEND") or "").strip()
    if b:
        # Clamp to the allowlist even though the dashboard's own write path
        # (backup_config_set) already validates it — secrets.env is an
        # operator-editable file by design, so a value written any other way
        # (hand edit, legacy install, future script) must not flow unvalidated
        # into the unauthenticated /status and /config GET responses.
        return b if b in _BACKENDS else "none"
    if (_key_value("GENESIS_BACKUP_NAS") or "").strip():
        return "smb"
    return "none"


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
    # `status` is backup_status.json — untrusted-by-construction (operator-
    # editable/corruptible), same as the failure_reason field _scrub_reason
    # guards. `_resolved_backend()` already clamps ITS OWN source
    # (GENESIS_BACKUP_TIER2_BACKEND) to the allowlist, but that clamp doesn't
    # cover THIS field — clamp again here so a malformed status file can't
    # smuggle an arbitrary value into `tier2["backend"]` on the unauthenticated
    # /status route.
    backend = status.get("tier2_backend") or _resolved_backend()
    if backend not in _BACKENDS:
        backend = "none"
    tier2 = {
        "backend": backend,
        "status": status.get("tier2_status"),
        "confirmed": status.get("offsite_confirmed"),
        "snapshot_id": status.get("snapshot_id"),
        "snapshot_count": status.get("snapshot_count"),
    }
    if is_authenticated():
        if backend == "smb":
            tier2["target"] = _strip_url_creds(_key_value("GENESIS_BACKUP_NAS")) or None
        elif backend == "local":
            tier2["target"] = _key_value("GENESIS_BACKUP_LOCAL_PATH") or None
    return {"tier1": tier1, "tier2": tier2}


# ── Health verdict (server-authoritative banner state) ────────────────

_INTERVAL_HOURS = {"3h": 3, "6h": 6, "12h": 12, "daily": 24}


def _scrub_reason(text: object) -> str | None:
    """Sanitize a failure_reason for the UNAUTHENTICATED banner.

    ``backup.sh`` only JSON-escapes the reason; it can still embed a home path
    (which carries a username) or raw gpg output. Strip ``/home/<user>`` → ``~``
    and cap the length before it is rendered on the unauth ``/status`` route.

    Accepts ``object``, not just ``str | None``: ``backup_status.json`` is
    untrusted-by-construction (operator-editable / corruptible), and a truthy
    NON-STRING value (a list/dict/int from a malformed record) must degrade to
    ``None`` rather than crash ``.replace()`` and 500 this unauthenticated route.
    """
    if not isinstance(text, str):
        return None
    if not text:
        return text
    scrubbed = text.replace(str(_HOME), "~")
    scrubbed = re.sub(r"/home/[^/\s]+", "~", scrubbed)
    if len(scrubbed) > 200:
        scrubbed = scrubbed[:199] + "…"
    return scrubbed


def _parse_systemd_timestamp(value: str | None) -> datetime | None:
    """Parse systemd's human-formatted timer timestamp into an aware UTC datetime.

    ``systemctl show -p NextElapseUSecRealtime`` returns a LOCALE/TZ-formatted
    string like ``"Fri 2026-07-10 18:10:00 EDT"`` — verified live against a real
    ``systemctl --user show genesis-backup.timer`` and matching this file's own
    test fixture — NEVER a raw epoch. Only the numeric ``YYYY-MM-DD HH:MM:SS``
    portion is extracted (via ``_SYSTEMD_TS_RE``) — the weekday name and tz
    abbreviation are both dropped rather than parsed: Python's ``%a``/``%Z``
    strptime directives only recognize English tokens, but ``systemctl_env()``
    copies this process's ``LC_TIME``/``LANG`` unmodified into the subprocess, so
    a non-English-locale host could otherwise render a weekday `strptime` can't
    match. The naive numeric timestamp is converted via ``.astimezone(UTC)``,
    which treats a naive datetime as system-local.

    KNOWN LIMITATION: dropping the tz abbreviation means the one hour/year a
    local wall-clock time occurs TWICE (the DST fall-back fold) is ambiguous —
    ``.astimezone()`` always resolves to the FIRST (pre-transition) occurrence,
    which can be up to 1h off from what systemd meant (confirmed:
    ``datetime.strptime("2026-11-01 01:30:00", ...).astimezone(UTC)`` picks
    ``fold=0``/EDT even when systemd meant the EST occurrence). Deliberately
    not fixed with a hardcoded abbreviation→offset map (abbreviations like
    "IST" aren't globally unique across timezones, so a map would be both
    incomplete and install-specific) — acceptable because the only consumer is
    the ``next_run``-in-the-future check against a 7-DAY (168h) overdue floor,
    where a 1h skew is immaterial, and the ambiguity self-corrects within the
    hour on the next poll.
    """
    if not value:
        return None
    v = value.strip()
    m = _SYSTEMD_TS_RE.search(v)
    if not m:
        return None
    try:
        naive = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        return naive.astimezone(UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _backup_health(
    last_backup: dict | None,
    schedule: dict | None,
    destinations: dict | None,
    *,
    configured: bool,
    resolved_backend: str,
) -> dict:
    """One authoritative backup-health verdict for the page-top banner.

    Returns ``{"state": "ok"|"warn"|"critical", "code": <slug>, "reason": <text>}``
    — precedence, first match wins. Computed server-side so the banner has ZERO
    client-side coordination: no dependency on a second config fetch (the client
    ``backupConfig``), and no client-clock staleness math. ``state == "ok"`` means
    the banner is hidden.
    """
    lb = last_backup if isinstance(last_backup, dict) else None
    sch = schedule or {}

    # 1. Recorded FAILURE (success explicitly False) — the most urgent state.
    if lb is not None and lb.get("success") is False:
        reason = "The last backup run failed"
        fr = _scrub_reason(lb.get("failure_reason"))
        if fr:
            reason += f" ({fr})"
        return {
            "state": "critical",
            "code": "failed",
            "reason": reason + " — check the Backup tab.",
        }

    # 2. Not configured — CURRENT signals only (a saved repo setting or a real
    #    clone), never a lingering prior run: a stale success record survives clone
    #    or secrets removal, and backup.sh cannot recreate the clone without the
    #    repo setting, so it must not mask the unconfigured state. Checked BEFORE the
    #    malformed-record branch so an abandoned install carrying a leftover garbage
    #    record still gets the more actionable "not configured" message.
    if not configured:
        return {
            "state": "warn",
            "code": "unconfigured",
            "reason": "Backups are not configured — your data is not being backed up. "
            "Set one up on the Backup tab.",
        }

    # 3. Configured, but no backup has ever run.
    if lb is None:
        return {
            "state": "warn",
            "code": "never_run",
            "reason": "Backups are configured but have never run — check the Backup tab.",
        }

    # 4. A present-but-malformed record (valid JSON, no boolean `success`) must not
    #    slip through to "healthy" — a `{}` or operator-repaired record reads as
    #    unreadable, not clean. (lb is non-None here — branch 3 handled None.)
    if not isinstance(lb.get("success"), bool):
        return {
            "state": "warn",
            "code": "unreadable_status",
            "reason": "The last backup status record is unreadable — check the Backup tab.",
        }

    # 5/6. Schedule-dependent checks (timer_stopped, overdue) — gated on the
    #    schedule probe having actually succeeded. When `probe_ok` is False
    #    (systemctl/D-Bus unreachable), `enabled`/`active`/`interval` all read as
    #    their zero-value defaults, indistinguishable from a genuinely-disabled
    #    timer — so these two checks must NOT run on unverified data (an active
    #    timer's overdue-ness would otherwise silently fall through to the loose
    #    7-day fallback for up to 7 days). tier1_unpushed/tier2_incomplete below
    #    do NOT read `sch` at all, so they stay reachable regardless of probe_ok —
    #    a transient systemctl outage must not mask an unrelated, already-known
    #    push/off-site problem.
    if sch.get("probe_ok") is not False:
        # 5. Scheduled but the timer is not running (stopped out-of-band / failed
        #    to start): unattended backups have silently stopped even though the
        #    last run succeeded.
        if sch.get("enabled") and not sch.get("active"):
            return {
                "state": "warn",
                "code": "timer_stopped",
                "reason": "Backups are scheduled but the timer is not running — unattended "
                "backups have stopped. Check the Backup tab.",
            }

        # 6. Overdue — the last successful backup is too old. For a KNOWN enabled
        #    interval use a pure age floor (interval*2 + 1h): an active timer can
        #    silently SKIP runs, leaving a stale timestamp while `next_run` is
        #    still in the future, so a future next_run does NOT prove recency
        #    there. For a custom/unknown/disabled schedule use a 7-day floor but
        #    suppress when a valid future `next_run` exists — a monthly custom
        #    timer legitimately runs less often than weekly and is not overdue.
        ts = lb.get("timestamp")
        if ts:
            try:
                age_h = (datetime.now(UTC) - datetime.fromisoformat(ts)).total_seconds() / 3600
            except (ValueError, TypeError):
                age_h = None
            if age_h is not None:
                interval_h = (
                    _INTERVAL_HOURS.get(sch.get("interval")) if sch.get("enabled") else None
                )
                if interval_h:
                    floor_h: float = interval_h * 2 + 1
                    overdue = age_h > floor_h
                    detail = f", well past the {interval_h}h schedule"
                else:
                    floor_h = 24 * 7
                    next_run = _parse_systemd_timestamp(sch.get("next_run"))
                    next_run_future = next_run is not None and next_run > datetime.now(UTC)
                    overdue = age_h > floor_h and not next_run_future
                    detail = ""
                if overdue:
                    return {
                        "state": "warn",
                        "code": "overdue",
                        "reason": "Backups are overdue — the last successful backup was about "
                        f"{round(age_h)}h ago{detail}. Check the Backup tab.",
                    }

    # 7. Tier-1 (GitHub) push did not complete — the local backup succeeded but
    #    commits are not replicated to the remote (a prior push failed and today's
    #    run had nothing new to commit, so `success` stays True).
    if lb.get("tier1_pushed") is False:
        return {
            "state": "warn",
            "code": "tier1_unpushed",
            "reason": "Backups are not fully replicated to GitHub — the last run succeeded "
            "locally but commits are not pushed to the remote. Check the Backup tab.",
        }

    # 8. Off-site (Tier-2) copy configured but incomplete. Gate on the RESOLVED
    #    backend (CURRENT config), not the last status record's `tier2_backend`: a
    #    Tier-2 backend disabled after a partial run must not keep warning off a
    #    stale record. Require an explicit incompleteness signal so an info-less
    #    legacy record stays quiet.
    if resolved_backend and resolved_backend != "none":
        t2 = (destinations or {}).get("tier2") or {}
        # 8a. The last CONFIRMED run was against a DIFFERENT backend than the one
        #     currently resolved (e.g. switched smb → local via /config). Reusing
        #     that stale confirmed/status would call the new, never-yet-run backend
        #     healthy. A recorded backend that matches (or an old record with none
        #     recorded at all) falls through to the normal incompleteness check.
        recorded_backend = lb.get("tier2_backend")
        if recorded_backend and recorded_backend != resolved_backend:
            return {
                "state": "warn",
                "code": "tier2_incomplete",
                "reason": "The off-site backend was changed to "
                f"'{resolved_backend}' and has not been confirmed yet — the last "
                "confirmed off-site copy was to a different backend. Check the "
                "Backup tab.",
            }
        status = t2.get("status")
        if (status is not None and status != "ok") or t2.get("confirmed") is False:
            return {
                "state": "warn",
                "code": "tier2_incomplete",
                "reason": "The off-site backup copy is incomplete — your local backup "
                "succeeded but the off-site copy did not finish. Check the Backup tab.",
            }

    # 9. The schedule probe itself failed (systemctl/D-Bus unreachable) and
    #    nothing more specific above already explained the banner — report the
    #    probe failure itself as a LAST RESORT, rather than silently reading
    #    healthy. Fail-closed: an unknown dependency state must never look
    #    "healthy" — but a probe outage must also not MASK an unrelated,
    #    already-known tier1/tier2 problem, hence this runs only after those.
    if sch.get("probe_ok") is False:
        return {
            "state": "warn",
            "code": "schedule_probe_failed",
            "reason": "Could not verify the backup schedule (systemctl probe failed) — "
            "check the Backup tab.",
        }

    return {"state": "ok", "code": "healthy", "reason": ""}


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
                last_backup = {k: v for k, v in raw.items() if k in _STATUS_SAFE_FIELDS}
                # failure_reason can embed a home path (which carries a username) or
                # raw gpg output; scrub it HERE so the sanitized value is the only one
                # that ever reaches this unauthenticated response — the banner reason
                # and this raw field must not disagree.
                if last_backup.get("failure_reason"):
                    last_backup["failure_reason"] = _scrub_reason(last_backup["failure_reason"])
        except (json.JSONDecodeError, OSError):
            last_backup = None
    result["last_backup"] = last_backup

    result["repo"] = _backup_repo_url()
    result["schedule"] = _timer_state()
    result["destinations"] = _destinations(last_backup, result["repo"])

    # Server-authoritative health verdict for the page-top banner. `configured`
    # uses CURRENT signals only — a saved repo setting, a real clone (`.git`, not
    # just the directory), or a resolvable origin — never a lingering prior run.
    from genesis.dashboard.routes.secrets import _key_value

    repo_setting = (_key_value("GENESIS_BACKUP_REPO") or "").strip()
    configured = bool(repo_setting or (_BACKUP_DIR / ".git").is_dir() or result["repo"])
    result["backup_health"] = _backup_health(
        last_backup,
        result["schedule"],
        result["destinations"],
        configured=configured,
        resolved_backend=_resolved_backend(),
    )

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
        "tier2_backend": _resolved_backend(),
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
    if backend == "local" and not (local_path or _key_value("GENESIS_BACKUP_LOCAL_PATH")):
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
                errors.append(f"schedule_interval must be one of {sorted(_INTERVAL_TO_CALENDAR)}")
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
                return jsonify({"error": "Failed to write backup schedule (drop-in)"}), 500
            if not _set_timer_enabled(True):
                return jsonify(
                    {
                        "error": "Schedule written but failed to enable the backup "
                        "timer — retry, or check `systemctl --user status "
                        "genesis-backup.timer`"
                    }
                ), 500
            schedule_result = "enabled"
        else:
            if not _set_timer_enabled(False):
                return jsonify({"error": "Failed to disable the backup timer"}), 500
            schedule_result = "disabled"

    logger.info(
        "Backup config updated: keys=%s schedule=%s",
        sorted(env_updates.keys()),
        schedule_action[0] if schedule_action else None,
    )
    return jsonify(
        {
            "status": "ok",
            "updated": sorted(env_updates.keys()),
            "schedule": schedule_result,
            "warnings": warnings,
            "needs_restart": False,
        }
    )


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
        err = r.stderr.strip() if r is not None else "systemctl unavailable"
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
