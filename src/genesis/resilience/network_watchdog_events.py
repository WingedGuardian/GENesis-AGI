"""Turn the root network watchdog's Tailscale events into Genesis observations.

The watchdog (``scripts/systemd/genesis-network-watchdog.sh``) runs as root and
writes only its own telemetry, ``/run/genesis-network-watchdog.json`` (0644).
When it heals a stuck tunnel, observes one, or fails to restart tailscaled, it
records ``tailscale.last_event`` there. The awareness tick calls
:func:`record_new_events` as the owning user; a recent event becomes an
``infrastructure_alert`` observation, which the existing machinery delivers:

* ``restart-failed`` and ``observed`` are ``critical`` — the critical-
  observations job pages the owner once (Tailscale may be down; or observe mode
  wants a human to act).
* ``healed`` is ``high`` — the dashboard and morning report. The owner already
  felt the SSH drop; a page five minutes later adds nothing.

Dedup is the observation store's own: one atomic ``INSERT … WHERE NOT EXISTS``
on a deterministic content hash (``skip_if_duplicate``). No seen-file, no
reliance on outreach dedup, and a database outage fails the write so the next
tick retries it rather than paging again. An event older than
:data:`_MAX_EVENT_AGE_S` is ignored, which bounds a re-fire after the row is
resolved or expires. An observation is keyed per peer, not per event time:
observe mode records a new event every run if the watchdog's hourly stamp
cannot be written, and that must stay one row, not one per run.

The observation names the peer by its validated IPv4 address only. The peer's
hostname is chosen by another tailnet member, and first-party observations must
carry no text from outside Genesis (``memory/provenance.py``); it stays in the
telemetry file.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Where the watchdog writes its telemetry (its STATE_FILE default).
DEFAULT_STATE_FILE = Path("/run/genesis-network-watchdog.json")
#: The telemetry is a few KB; anything far larger is not the watchdog's file.
_MAX_STATE_BYTES = 1_000_000
#: Older events are not raised: after a reboot or restore the file is gone, and
#: within a boot this bounds a re-fire once the row is resolved or expires.
_MAX_EVENT_AGE_S = 6 * 3600
_ACTIONS = ("healed", "observed", "restart-failed")
SOURCE = "network_watchdog_monitor"
_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")


def _read_event(state_file: Path) -> dict[str, Any] | None:
    """The watchdog's ``tailscale.last_event``, or None when absent or malformed."""
    try:
        with open(state_file, "rb") as fh:
            raw = fh.read(_MAX_STATE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_STATE_BYTES:
        logger.warning("network watchdog telemetry %s is oversized — ignored", state_file)
        return None
    try:
        state = json.loads(raw)
    except ValueError:
        return None
    ts = state.get("tailscale") if isinstance(state, dict) else None
    event = ts.get("last_event") if isinstance(ts, dict) else None
    if not isinstance(event, dict) or event.get("action") not in _ACTIONS:
        return None
    at = event.get("at")
    if not isinstance(at, int) or isinstance(at, bool) or at <= 0:
        return None
    return event


def _peer_ip(event: dict[str, Any]) -> str:
    """The peer's IPv4 address from ``"name (ip)"``, or ``"a peer"``."""
    peer = str(event.get("peer") or "")
    if peer.endswith(")") and "(" in peer:
        candidate = peer[peer.rindex("(") + 1 : -1]
        try:
            return str(ipaddress.IPv4Address(candidate))
        except ValueError:
            pass
    return "a peer"


def _boot_id() -> str:
    try:
        return _BOOT_ID.read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def _observation(event: dict[str, Any]) -> tuple[str, str]:
    """(priority, content) for one event. The first sentence says what to do:
    the critical-observations page shows only the start of the content."""
    peer = _peer_ip(event)
    age = event.get("handshake_age_s")
    stuck = (
        f"No WireGuard handshake with {peer}"
        + (f" for {age}s" if isinstance(age, int) else "")
        + " while traffic was wanted; the tunnel ping got no reply but the peer"
        " answered discovery pings. Evidence: 'tailscale' in"
        " /run/genesis-network-watchdog.json and the root-only status snapshot beside it."
    )
    action = event["action"]
    if action == "restart-failed":
        rc = event.get("rc")
        return (
            "critical",
            "Tailscale may be DOWN: the network watchdog restarted tailscaled to clear a"
            " stuck tunnel and it did not come back"
            + (f" (rc={rc})" if isinstance(rc, int) else "")
            + ". It will not retry. Check: sudo systemctl status tailscaled; fix:"
            f" sudo systemctl restart tailscaled. {stuck}",
        )
    if action == "observed":
        return (
            "critical",
            f"Tailscale tunnel to {peer} is stuck (watchdog in observe mode, so it did"
            f" not restart it). Restart it with: sudo systemctl restart tailscaled. {stuck}",
        )
    rate = event.get("rate_limit_s")
    after = ""
    if isinstance(rate, int):
        after = (
            f" Next automatic restart is allowed after {rate // 60} min."
            if rate >= 60
            else (f" Next automatic restart is allowed after {rate}s.")
        )
    return (
        "high",
        f"The network watchdog restarted tailscaled to clear a stuck tunnel to {peer};"
        f" Tailscale SSH sessions on this machine dropped (tmux sessions survive).{after}"
        f" {stuck}",
    )


def _content_hash(event: dict[str, Any], boot_id: str) -> str:
    # One row per stuck peer for an observation; per event otherwise.
    action = event["action"]
    identity = f"observed:{_peer_ip(event)}" if action == "observed" else f"{action}:{event['at']}"
    return hashlib.sha256(f"network_watchdog:tailscale:{boot_id}:{identity}".encode()).hexdigest()


async def record_new_events(
    db,
    *,
    state_file: Path | None = None,
    now: float | None = None,
    boot_id: str | None = None,
) -> bool:
    """Record a recent watchdog event as an observation. True if a row was created.

    ``state_file`` defaults to ``$GENESIS_NETWD_STATE_FILE`` (the test suite
    points it at a temp path), else the watchdog's own default.
    Never raises: a broken read must not break the awareness tick.
    """
    if db is None:
        return False
    try:
        if state_file is None:
            override = os.environ.get("GENESIS_NETWD_STATE_FILE")
            state_file = Path(override) if override else DEFAULT_STATE_FILE
        event = _read_event(state_file)
        if event is None:
            return False
        # A negative age (the clock stepped back) is still recent: the hash, not
        # the ordering, is what stops a repeat.
        if (now if now is not None else time.time()) - event["at"] > _MAX_EVENT_AGE_S:
            return False

        from genesis.db.crud import observations

        priority, content = _observation(event)
        content_hash = _content_hash(event, boot_id or _boot_id())
        if event["action"] != "observed":
            # One event, one row, EVER. The store's own dedup matches unresolved
            # rows only, so resolving a "Tailscale may be DOWN" page would let the
            # next tick page it again. The awareness tick is the only writer.
            cursor = await db.execute(
                "SELECT 1 FROM observations WHERE source = ? AND content_hash = ? LIMIT 1",
                (SOURCE, content_hash),
            )
            if await cursor.fetchone():
                return False
        created = await observations.create(
            db,
            id=str(uuid.uuid4()),
            source=SOURCE,
            type="infrastructure_alert",
            content=content,
            priority=priority,
            created_at=datetime.now(UTC).isoformat(),
            content_hash=content_hash,
            skip_if_duplicate=True,
        )
        if created:
            logger.warning("network watchdog event recorded: %s (%s)", event["action"], priority)
        return bool(created)
    except Exception:
        logger.warning("network watchdog event check failed", exc_info=True)
        return False
