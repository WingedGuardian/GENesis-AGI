"""Owner alerts for the root network watchdog's Tailscale events.

The watchdog (``scripts/systemd/genesis-network-watchdog.sh``) runs as root and
writes only its own telemetry, ``/run/genesis-network-watchdog.json`` (0644).
When it heals a stuck tunnel, observes one, or fails to restart tailscaled, it
records ``tailscale.last_event`` there. This module runs inside the Genesis
runtime, as the owning user, once per awareness tick: it turns a NEW event into
an ordinary entry in the user's durable alert queue, which the same tick then
drains to the owner.

Why this side raises the alert: the watchdog used to write queue files into the
user's home directory as root, and every hardening round on that write found
another way a user-owned directory could redirect a root write. Reading a
root-owned file as the user crosses no privilege boundary, and the latency is
the same, because the queue is only ever drained on this tick.

An event's identity is its ``at`` timestamp. The last one alerted is kept in a
small seen-file, so each event alerts once. On the first run with no seen-file,
an event older than a day is recorded as seen without alerting: after an
upgrade, a heal from last week is not news.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Where the watchdog writes its telemetry (its STATE_FILE default).
DEFAULT_STATE_FILE = Path("/run/genesis-network-watchdog.json")
#: The telemetry is a few KB; anything far larger is not the watchdog's file.
_MAX_STATE_BYTES = 1_000_000
#: With no seen-file (first run), older events are recorded, not alerted.
_FIRST_RUN_WINDOW_S = 24 * 3600
_ACTIONS = ("healed", "observed", "restart-failed")


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


def _read_seen(seen_file: Path) -> int | None:
    try:
        value = json.loads(seen_file.read_text()).get("tailscale_event_at")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _write_seen(seen_file: Path, at: int) -> None:
    try:
        seen_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = seen_file.with_name(f".{seen_file.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"tailscale_event_at": at}))
        os.replace(tmp, seen_file)
    except OSError:
        logger.warning("could not record the network watchdog seen-file", exc_info=True)


def _message(event: dict[str, Any]) -> tuple[str, str, str]:
    """(severity, title, body) for one event."""
    peer = str(event.get("peer") or "a peer")
    age = event.get("handshake_age_s")
    stuck = (
        f"No WireGuard handshake with {peer}"
        + (f" for {age}s" if isinstance(age, int) else "")
        + " while traffic was wanted; the tunnel ping got no reply but the peer"
        " answered discovery pings, so the path was fine and the tunnel was stuck."
        " Evidence: 'tailscale' in /run/genesis-network-watchdog.json and the"
        " root-only status snapshot beside it."
    )
    action = event["action"]
    if action == "healed":
        rate = event.get("rate_limit_s")
        after = ""
        if isinstance(rate, int):
            wait = f"{rate // 60} min" if rate >= 60 else f"{rate}s"
            after = f" Next automatic restart is allowed after {wait}."
        return (
            "warning",
            f"Tailscale tunnel to {peer} was stuck — restarted tailscaled",
            f"{stuck} Restarting tailscaled dropped the Tailscale SSH sessions on this"
            f" machine; tmux sessions survive, so reconnect.{after}",
        )
    if action == "observed":
        return (
            "warning",
            f"Tailscale tunnel to {peer} is stuck (observe mode — not restarted)",
            f"{stuck} Restart it with: sudo systemctl restart tailscaled",
        )
    rc = event.get("rc")
    return (
        "critical",
        "Tailscale restart FAILED — tailscaled may be down",
        f"{stuck} The watchdog tried to restart tailscaled and the restart failed"
        + (f" (rc={rc})" if isinstance(rc, int) else "")
        + ", so Tailscale may now be down entirely. It will not retry."
        " Check: sudo systemctl status tailscaled; fix with: sudo systemctl restart tailscaled",
    )


def enqueue_new_events(
    *,
    queue_root: Path,
    seen_file: Path,
    state_file: Path | None = None,
    now: float | None = None,
) -> bool:
    """Queue an owner alert for a new watchdog event. Returns True if one was queued.

    ``state_file`` defaults to ``$GENESIS_NETWD_STATE_FILE`` (the test suite
    points it at a temp path), else the watchdog's own default.
    Never raises: a broken telemetry read must not stop the tick's drain.
    """
    try:
        if state_file is None:
            override = os.environ.get("GENESIS_NETWD_STATE_FILE")
            state_file = Path(override) if override else DEFAULT_STATE_FILE
        event = _read_event(state_file)
        if event is None:
            return False
        at = event["at"]
        seen = _read_seen(seen_file)
        if seen is not None and at <= seen:
            return False
        if seen is None and (now if now is not None else time.time()) - at > _FIRST_RUN_WINDOW_S:
            _write_seen(seen_file, at)
            return False

        from genesis.guardian.alert import queue as alert_queue

        severity, title, body = _message(event)
        if event["action"] == "observed":
            # Observe mode never restarts, so only the /run stamp holds its hour;
            # if that write fails, a new observation lands every run. One key per
            # peer lets the queue and the outreach dedup keep that to one page.
            key = f"network-watchdog:tailscale:observed:{event.get('peer')}"
        else:
            key = f"network-watchdog:tailscale:{event['action']}:{at}"
        queued = alert_queue.enqueue_alert(
            queue_root,
            severity=severity,
            source="network-watchdog",
            title=title,
            body=body,
            dedupe_key=key,
        )
        # enqueue_alert returns False both for a write failure and for "already
        # queued". Only a failure leaves the event unrecorded, so the next tick
        # retries it; an entry already in the queue is as good as queued.
        if queued or any(
            e.get("dedupe_key") == key for _, e in alert_queue.list_queued(queue_root)
        ):
            _write_seen(seen_file, at)
        return queued
    except Exception:
        logger.warning("network watchdog event check failed", exc_info=True)
        return False
