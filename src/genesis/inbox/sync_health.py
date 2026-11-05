"""Staleness alert for the inbox sync (``scripts/inbox_sync.sh``).

The sync runs from a systemd timer, outside the server, and its failure modes
are silent: a scheduler that stops firing, a run that keeps failing, a
deletion the trash keeps refusing. Every configured run writes a heartbeat
(``genesis.inbox.sync_config.heartbeat_path()``); the awareness loop calls
``check_inbox_sync_health`` on each tick and raises an ``infrastructure_alert``
when the heartbeat says the sync is not working, resolving it once it is.

Silent when the sync is not configured (``inbox_monitor.sync_remote`` empty):
most installs do not have a vault.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

SOURCE = "inbox_sync_monitor"
_TYPE = "infrastructure_alert"
# Six missed 5-minute cycles. Long enough that one slow or failed cycle does
# not page; short enough that a dead scheduler is noticed within the hour.
STALE_AFTER = timedelta(minutes=30)
# infrastructure_alert rows expire after 3 days, and the dedup only sees open rows,
# so a long outage would otherwise raise a new alert every 3 days. Each stays open
# until the check itself resolves it: one alert per outage.
_OPEN_UNTIL_RESOLVED = timedelta(days=3650)


@dataclass(frozen=True)
class Problem:
    key: str  # stable per condition: dedup and targeted resolve
    priority: str
    message: str


def _hash(key: str) -> str:
    return hashlib.sha256(f"inbox_sync:{key}".encode()).hexdigest()


def _caller_without_a_remote(heartbeat_text: str | None, now: datetime) -> list[Problem]:
    """An alert when something other than the timer (an old crontab line) ran the
    sync recently with no remote set: someone expects a sync that is not happening."""
    try:
        stamp = datetime.fromisoformat(json.loads(heartbeat_text)["unconfigured_caller"])
        recent = timedelta(0) <= now - stamp <= STALE_AFTER
    except (TypeError, ValueError, KeyError, RecursionError):
        return []
    if not recent:
        return []
    return [
        Problem(
            "unconfigured",
            "high",
            "Inbox sync: something other than genesis-inbox-sync.timer (a crontab line?) "
            "is running the sync, but inbox_monitor.sync_remote is empty, so nothing is "
            "syncing. Set sync_remote in ~/.genesis/config/inbox_monitor.local.yaml, then "
            "remove the crontab line.",
        )
    ]


def assess(remote: str, heartbeat_text: str | None, now: datetime) -> list[Problem]:
    """Problems the heartbeat shows. Pure: no I/O.

    ``heartbeat_text`` is the heartbeat file's content, or None when missing.
    """
    if not remote:
        return _caller_without_a_remote(heartbeat_text, now)
    if heartbeat_text is None:
        return [
            Problem(
                "stale",
                "high",
                "Inbox sync: no heartbeat at all although inbox_monitor.sync_remote is set. "
                "The sync has never completed a run on this install: check that "
                "genesis-inbox-sync.timer is enabled (`systemctl --user list-timers`) "
                "and read logs/inbox_sync.log. New vault notes are not reaching Genesis, "
                "and responses are not reaching the vault.",
            )
        ]
    try:
        data = json.loads(heartbeat_text)
        last_ok_raw = str(data.get("last_ok") or "")
        held = data.get("held")
        held = 0 if held is None else held
        # The script writes a non-negative int: anything else must not read as healthy.
        if type(held) is not int or held < 0:
            raise ValueError("held")
    except (ValueError, TypeError, AttributeError, RecursionError):
        return [
            Problem(
                "unreadable",
                "high",
                "Inbox sync: the heartbeat file is unreadable, so whether the sync works "
                "is unknown. Read logs/inbox_sync.log.",
            )
        ]
    problems: list[Problem] = []
    last_ok: datetime | None = None
    if last_ok_raw:
        try:
            last_ok = datetime.fromisoformat(last_ok_raw)
        except ValueError:
            last_ok = None
        if last_ok is not None and last_ok.utcoffset() is None:
            last_ok = None  # cannot be compared to an aware now, and the sync never writes one
    # A future stamp would stay silent until it passed: not evidence of a recent cycle.
    if last_ok is None or not timedelta(0) <= now - last_ok <= STALE_AFTER:
        since = f"since {last_ok_raw}" if last_ok_raw else "ever"
        problems.append(
            Problem(
                "stale",
                "high",
                f"Inbox sync: no fully successful cycle {since} (expected every 5 minutes). "
                "Either the timer stopped (`systemctl --user list-timers | grep inbox-sync`) "
                "or every run fails (read the `Heartbeat: cycle not ok` line in "
                "logs/inbox_sync.log). New vault notes may not be reaching Genesis, and "
                "responses may not be reaching the vault. Before "
                "restarting a long-stopped sync, check that the version running keeps a "
                "vault record (.genesis-seen-in-vault): an older script deletes every "
                "response written while it was stopped.",
            )
        )
    if held > 0:
        problems.append(
            Problem(
                "held",
                "medium",
                f"Inbox sync: {held} response(s) deleted in the vault cannot be removed "
                "locally because the Genesis trash refuses them (see the HELD lines in "
                "logs/inbox_sync.log for the reason). They are kept and never re-uploaded, "
                "but the deletion will not apply until the cause is fixed.",
            )
        )
    return problems


_HEARTBEAT_MAX_BYTES = 64 * 1024  # the real file is ~90 bytes


def _read_heartbeat(path) -> str | None:
    """The heartbeat's text, or None when it is missing, a symlink or not a
    regular file. Opened without following a symlink, and read bounded: a
    planted file can neither redirect the read nor exhaust memory."""
    import os

    try:
        # O_NONBLOCK: a FIFO with no writer would otherwise block this open on the
        # event loop, before the regular-file check below can refuse it.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        import errno

        # A symlink is not a heartbeat the sync wrote, so it reads as none. Any
        # other failure is present but unreadable, which assess reports as such.
        return None if exc.errno == errno.ELOOP else ""
    try:
        import stat

        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return ""
        data = os.read(fd, _HEARTBEAT_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > _HEARTBEAT_MAX_BYTES:
        return "{oversized}"  # reads as unreadable, so it is reported
    return data.decode("utf-8", errors="replace")


async def check_inbox_sync_health(db) -> None:
    """Raise or resolve the inbox-sync alerts. Never raises into the tick."""
    if db is None:
        return
    try:
        from genesis.db.crud import observations
        from genesis.inbox.sync_config import (
            heartbeat_path,
            load_sync_settings,
            mirror_target_problem,
            remote_is_valid,
        )

        now = datetime.now(UTC)
        config_problem = None
        try:
            remote, watch_path = load_sync_settings()
        except Exception as exc:
            # The sync refuses to run on this too, so it must not be silent:
            # the remote usually lives only in the overlay, and a broken
            # overlay would otherwise read as "sync off" on both sides. Only
            # the exception TYPE goes in the alert: a YAML error quotes lines
            # of the overlay, which the dashboard and digests would then show.
            logger.warning("inbox sync config unreadable", exc_info=True)
            config_problem = (
                f"the inbox config (or its local overlay "
                f"~/.genesis/config/inbox_monitor.local.yaml) cannot be read "
                f"({type(exc).__name__}; the server log has the detail)"
            )
        else:
            if remote and not remote_is_valid(remote):
                config_problem = "inbox_monitor.sync_remote is not a valid rclone remote"
            elif remote and (why := mirror_target_problem(watch_path)):
                config_problem = f"inbox_monitor.watch_path cannot be the sync target: {why}"
        if config_problem:
            problems = [
                Problem(
                    "config",
                    "high",
                    f"Inbox sync: {config_problem}, so the sync refuses to run. "
                    "Fix the setting; the alert clears on the next tick.",
                )
            ]
        else:
            problems = assess(remote, _read_heartbeat(heartbeat_path()), now)
        active = {p.key for p in problems}
        for p in problems:
            await observations.create(
                db,
                id=str(uuid.uuid4()),
                source=SOURCE,
                type=_TYPE,
                content=p.message,
                priority=p.priority,
                created_at=now.isoformat(),
                expires_at=(now + _OPEN_UNTIL_RESOLVED).isoformat(),
                content_hash=_hash(p.key),
                skip_if_duplicate=True,
            )
        # A config problem means the heartbeat was not read, so it proves nothing
        # about the other alerts: they stay open rather than closing and re-opening.
        checked = () if config_problem else ("config", "stale", "unreadable", "held", "unconfigured")
        cleared = [_hash(k) for k in checked if k not in active]
        if cleared:
            await observations.resolve_by_source_and_type(
                db,
                source=SOURCE,
                type=_TYPE,
                resolved_at=now.isoformat(),
                resolution_notes="auto-resolved: the inbox sync heartbeat is healthy again",
                content_hashes=cleared,
            )
    except Exception:
        logger.warning("inbox sync health check failed", exc_info=True)
