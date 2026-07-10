"""Container-side credential self-heal — runs on the awareness tick + at startup.

Wraps the standalone validator (``cred_integrity``) with the container's policy:
detect corruption, restore from the encrypted backup, and write a status file
that the health-alert path (``mcp/health/errors.py``) surfaces to Telegram.

This is the FIRST responder in the G.1 escalation ladder — the host guardian
only steps in if the container fails to self-heal within its grace window.

Debounce policy (protects a legitimate writer caught mid-rewrite, e.g. Claude
Code rewriting ~/.claude.json):
- ``empty`` / ``nul_bytes`` / ``missing`` — the outage signature; restore now.
- ``parse_error`` / ``missing_keys`` — restore only if still corrupt next tick
  (2-tick confirmation). At startup there is no live writer, so restore now.
- ``unreadable`` — ambiguous (permission/IO), alert-only, never a destructive
  restore.

Restores are rate-capped per target (``max_restores_per_day``) so a bad backup
copy can't drive a restore loop; the cap then alerts instead.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from genesis.guardian.cred_integrity import (
    DEFAULT_TARGETS,
    RESTORABLE_STATUSES,
    allowed_restore,
    check_all,
    resolve_passphrase,
    restore_file,
)

logger = logging.getLogger(__name__)

_STATUS_VERSION = 1
_IMMEDIATE_STATUSES = frozenset({"empty", "nul_bytes", "missing"})
_RESTORED_TTL_S = 86400  # keep a "restored" status visible for 24h (alert dedup window)
_MAX_EVENTS = 20


def _now() -> datetime:
    return datetime.now(UTC)


def _load_status(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_status(path: Path, payload: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cred_status.")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError:
        logger.warning("failed to write cred integrity status", exc_info=True)


def check_and_selfheal(
    *,
    home: Path | None = None,
    backup_dir: Path | None = None,
    status_path: Path | None = None,
    max_restores_per_day: int = 3,
    startup: bool = False,
    targets=None,
) -> dict:
    """Validate all credential targets; restore corrupt ones from backup.

    Returns the status dict (also persisted to ``status_path``). Never raises —
    the awareness loop invokes this zero-arg and must not be broken by it.
    """
    home = home or Path.home()
    if backup_dir is None:
        candidate = home / "backups" / "genesis-backups"
        backup_dir = candidate if candidate.exists() else None
    status_path = status_path or (home / ".genesis" / "cred_integrity_status.json")
    tgts = targets if targets is not None else DEFAULT_TARGETS
    by_name = {t.name: t for t in tgts}

    prior = _load_status(status_path).get("targets", {})
    now = _now()
    now_iso = now.isoformat()
    out_targets: dict[str, dict] = {}
    events: list[str] = []

    try:
        results = check_all(tgts, home, backup_dir)
    except Exception:  # noqa: BLE001 — must never break the tick
        logger.warning("cred integrity check failed", exc_info=True)
        return _load_status(status_path)

    for name, vr in results.items():
        target = by_name[name]
        prev = prior.get(name, {})
        attempts: list[str] = list(prev.get("restore_attempts", []))

        # Healthy (ok / absent): keep a recent "restored" visible for its TTL so
        # the CRITICAL creds:restored alert fires, then collapse to ok.
        if vr.ok:
            if prev.get("status") == "restored":
                last = prev.get("last_event_at")
                if last and (now - _isoparse(last)).total_seconds() < _RESTORED_TTL_S:
                    out_targets[name] = {**prev}  # preserve the restored record
                    continue
            out_targets[name] = {"status": "ok", "detail": vr.detail}
            continue

        # Corrupt. first_seen persists across ticks until it resolves.
        first_seen = prev.get("first_seen", now_iso)

        # Unreadable → alert-only, never restore.
        if vr.status not in RESTORABLE_STATUSES:
            out_targets[name] = {
                "status": "corrupt", "detail": f"{vr.status}: {vr.detail}",
                "first_seen": first_seen, "last_event_at": now_iso,
                "restore_attempts": attempts,
            }
            events.append(f"{name}: {vr.status} (alert-only)")
            continue

        immediate = startup or vr.status in _IMMEDIATE_STATUSES
        confirmed = prev.get("status") == "corrupt_pending"

        if not immediate and not confirmed:
            # First sighting of an ambiguous corruption — wait one tick.
            out_targets[name] = {
                "status": "corrupt_pending", "detail": f"{vr.status}: {vr.detail}",
                "first_seen": first_seen, "last_event_at": now_iso,
                "restore_attempts": attempts,
            }
            events.append(f"{name}: {vr.status} (pending 2-tick confirm)")
            continue

        # Restore. Rate-cap first.
        if not allowed_restore(attempts, now, max_restores_per_day):
            out_targets[name] = {
                "status": "restore_failed",
                "detail": f"{vr.status}; rate cap reached ({max_restores_per_day}/day)",
                "first_seen": first_seen, "last_event_at": now_iso,
                "restore_attempts": attempts,
            }
            events.append(f"{name}: restore rate-capped")
            continue

        if backup_dir is None:
            out_targets[name] = {
                "status": "corrupt", "detail": f"{vr.status}; no backup configured",
                "first_seen": first_seen, "last_event_at": now_iso,
                "restore_attempts": attempts,
            }
            events.append(f"{name}: {vr.status} (no backup to restore from)")
            continue

        passphrase = resolve_passphrase(home)
        if not passphrase:
            out_targets[name] = {
                "status": "restore_failed",
                "detail": f"{vr.status}; no passphrase (env/secrets/escrow)",
                "first_seen": first_seen, "last_event_at": now_iso,
                "restore_attempts": attempts,
            }
            events.append(f"{name}: no passphrase to decrypt backup")
            continue

        attempts.append(now_iso)
        try:
            rr = restore_file(
                target, home=home, backup_dir=backup_dir, passphrase=passphrase
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("restore_file raised for %s", name, exc_info=True)
            out_targets[name] = {
                "status": "restore_failed", "detail": f"restore error: {exc}",
                "first_seen": first_seen, "last_event_at": now_iso,
                "restore_attempts": attempts,
            }
            events.append(f"{name}: restore raised {exc}")
            continue

        if rr.ok:
            logger.warning(
                "cred self-heal: RESTORED %s from backup (%s)", name, rr.backup_mtime
            )
            out_targets[name] = {
                "status": "restored", "detail": rr.detail,
                "first_seen": first_seen, "last_event_at": now_iso,
                "restore_attempts": attempts,
                "aside_path": rr.aside_path, "backup_mtime": rr.backup_mtime,
            }
            events.append(f"{name}: restored from backup dated {rr.backup_mtime}")
        else:
            logger.error("cred self-heal: restore FAILED for %s (%s)", name, rr.action)
            out_targets[name] = {
                "status": "restore_failed", "detail": f"{rr.action}: {rr.detail}",
                "first_seen": first_seen, "last_event_at": now_iso,
                "restore_attempts": attempts,
            }
            events.append(f"{name}: restore failed ({rr.action})")

    prior_events = _load_status(status_path).get("recent_events", [])
    stamped = [f"{now_iso} {e}" for e in events]
    payload = {
        "version": _STATUS_VERSION,
        "checked_at": now_iso,
        "targets": out_targets,
        "recent_events": (stamped + prior_events)[:_MAX_EVENTS],
    }
    _write_status(status_path, payload)
    return payload


def _isoparse(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=UTC)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
