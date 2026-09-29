"""Turn the root Tailscale watchdog's events into Genesis observations.

The watchdog (``scripts/systemd/genesis-tailscale-watchdog.py``) runs as root
and writes only its own file, ``/run/genesis-tailscale-watchdog.json`` (0644):
the last run's state plus a list of at most 50 events. The awareness tick calls
:func:`record_new_events` as the owning user, and the existing machinery
delivers the rows it writes:

* ``critical`` pages the owner once (the critical-observations job):
  ``restart-failed``, ``not-restarted``, ``unverified`` and ``pending`` (a
  restart that did not demonstrably bring Tailscale back), and ``observed``
  (observe mode found a stuck tunnel and left it alone).
* ``high`` shows on the dashboard and in the morning report: ``healed`` (the
  owner already felt the SSH drop), and the watchdog having gone silent.

Identity is the INCIDENT, not the detection: the watchdog keys an event on the
boot, the peer and that peer's handshake time, which does not move while one
tunnel stays stuck. A row is keyed on ``(incident, action)`` and checked
against every row, resolved or not, so one stuck tunnel pages once, and
resolving the page never lets the same incident page again.

``restart-no-effect`` is the watchdog's own check after a restart: tailscaled
came back, but the stuck peer still did not answer through the tunnel.

When the watchdog's latest run found every tunnel healthy, open ``critical``
rows from this source are resolved: a stale "Tailscale may be DOWN" must not
outlive its recovery.
If one tick sees both a failure event and a later healthy run (the runtime was
down in between), the row is created already resolved and is never paged: the
fault cleared on its own, and the row stays as the record.

If the watchdog reports but has judged no tunnel for several runs in a row
(tailscaled down, the CLI failing, a status it cannot parse), a ``high`` row
says it is blind.

If the timer is enabled but the file is missing or has not been rewritten for
:data:`_SILENT_AFTER_S`, a ``high`` row says the watchdog has gone silent. That
is the only way its failure reaches anyone: this user cannot read the system
journal.

The observation names a peer by its IPv4 address only. The watchdog never
reads peer hostnames (another tailnet member chooses those), and first-party
observations carry no text from outside Genesis (``memory/provenance.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = Path("/run/genesis-tailscale-watchdog.json")
TIMER_UNIT = "genesis-tailscale-watchdog.timer"
SOURCE = "tailscale_watchdog_monitor"
TYPE = "infrastructure_alert"
CATEGORY_DOWN = "tailscale_tunnel_down"
CATEGORY_HEALED = "tailscale_tunnel_healed"
CATEGORY_SILENT = "tailscale_watchdog_silent"
CATEGORY_BLIND = "tailscale_watchdog_blind"
_MAX_STATE_BYTES = 1_000_000
#: The timer fires every ~2 minutes; three missed runs plus slack is silence.
_SILENT_AFTER_S = 600
_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
_EVIDENCE = (
    " Evidence: /run/genesis-tailscale-watchdog.json and the root-only status snapshot beside it."
)
#: The same contract as the watchdog's ``valid_event``.
_ACTIONS = frozenset(
    {
        "healed",
        "restart-no-effect",
        "restart-failed",
        "not-restarted",
        "unverified",
        "pending",
        "observed",
    }
)
#: The watchdog's runs that judged no tunnel (its BLIND_ACTIONS), and how many
#: in a row make it blind rather than momentarily unable (~6 minutes).
_BLIND_ACTIONS = frozenset({"unavailable", "status-unparseable"})
_BLIND_AFTER_RUNS = 3
_HEX32 = re.compile(r"^[0-9a-f]{32}$")

TimerState = Callable[[], Awaitable[dict[str, str] | None]]


def _valid_event(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    try:
        ip = event["peer_ip"]
        return (
            isinstance(event["incident"], str)
            and bool(_HEX32.match(event["incident"]))
            and event["action"] in _ACTIONS
            and isinstance(ip, str)
            and str(ipaddress.IPv4Address(ip)) == ip
            and (event["handshake_age_s"] is None or type(event["handshake_age_s"]) is int)
            and (event["rc"] is None or type(event["rc"]) is int)
            and type(event["rate_limit_s"]) is int
        )
    except (KeyError, TypeError, ValueError):
        return False


def _read_state(state_file: Path) -> dict[str, Any] | None:
    """The watchdog's file as a dict, or None when absent or unreadable."""
    try:
        with open(state_file, "rb") as fh:
            raw = fh.read(_MAX_STATE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_STATE_BYTES:
        logger.warning("tailscale watchdog file %s is oversized; ignored", state_file)
        return None
    try:
        state = json.loads(raw)
    except ValueError:
        return None
    return state if isinstance(state, dict) else None


def _content(event: dict[str, Any]) -> tuple[str, str, str]:
    """(priority, category, content). The first sentence says what to do: the
    critical page shows only the start of the content."""
    ip = event["peer_ip"]
    age = event["handshake_age_s"]
    stuck = (
        f" The tunnel to {ip} had no WireGuard handshake"
        + (f" for {age}s" if age is not None else "")
        + " while traffic was wanted; the tunnel ping got no reply but the peer"
        " answered discovery pings."
    )
    fix = " Fix: sudo systemctl restart tailscaled."
    action = event["action"]
    if action == "healed":
        rate = event["rate_limit_s"]
        return (
            "high",
            CATEGORY_HEALED,
            f"The Tailscale watchdog restarted tailscaled to clear a stuck tunnel to {ip};"
            " Tailscale SSH sessions on this machine dropped (tmux sessions survive)."
            f" The next automatic restart is allowed after {max(1, rate // 60)} min."
            + stuck
            + _EVIDENCE,
        )
    lead = {
        "restart-no-effect": "A Tailscale tunnel is still stuck: the watchdog restarted"
        " tailscaled, which came back, but the tunnel still gets no reply; it will not"
        " retry for an hour. Check the peer and this node's relay (tailscale netcheck)." + fix,
        "restart-failed": "Tailscale may be DOWN: the watchdog restarted tailscaled and it did"
        " not come back. Check: sudo systemctl status tailscaled." + fix,
        "not-restarted": "A Tailscale tunnel is stuck and the watchdog's restart of tailscaled"
        f" failed (rc={event['rc']}); nothing restarted, and it will not retry for an hour." + fix,
        "unverified": "The Tailscale watchdog restarted tailscaled but could not read whether it"
        " came back. Check: sudo systemctl status tailscaled.",
        "pending": "The Tailscale watchdog's restart of tailscaled had not finished when it"
        " stopped waiting; tailscaled may be hung. Check: sudo systemctl status tailscaled.",
        "observed": f"The Tailscale tunnel to {ip} is stuck (the watchdog is in observe mode,"
        " so it did not restart tailscaled)." + fix,
    }[action]
    return "critical", CATEGORY_DOWN, lead + stuck + _EVIDENCE


def _hash(key: str) -> str:
    return hashlib.sha256(f"tailscale_watchdog:{key}".encode()).hexdigest()


def _boot_id() -> str:
    try:
        return _BOOT_ID.read_text().strip() or "unknown"
    except OSError:
        return "unknown"


async def _systemd_timer_state() -> dict[str, str] | None:
    """The watchdog timer's enablement and activation, or None if unknown."""
    systemctl = os.environ.get("GENESIS_TSWD_SYSTEMCTL") or "systemctl"
    try:
        proc = await asyncio.create_subprocess_exec(
            systemctl,
            "show",
            TIMER_UNIT,
            "-p",
            "UnitFileState",
            "-p",
            "ActiveState",
            "-p",
            "ActiveEnterTimestampMonotonic",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    found: dict[str, str] = {}
    for line in out.decode("utf-8", "replace").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            found[key] = value.strip()
    return found


async def _create(db, *, key: str, priority: str, category: str, content: str) -> bool:
    """One row per key, EVER. The store's own dedup matches unresolved rows
    only, so resolving a page would otherwise let the next tick page it again.
    The awareness tick is the only writer for this source."""
    from genesis.db.crud import observations

    content_hash = _hash(key)
    if await observations.exists_by_hash(db, source=SOURCE, content_hash=content_hash):
        return False
    created = await observations.create(
        db,
        id=str(uuid.uuid4()),
        source=SOURCE,
        type=TYPE,
        category=category,
        content=content,
        priority=priority,
        created_at=datetime.now(UTC).isoformat(),
        content_hash=content_hash,
        skip_if_duplicate=True,
    )
    return bool(created)


async def _resolve(db, category: str, note: str) -> int:
    from genesis.db.crud import observations

    return await observations.resolve_by_source_and_type(
        db,
        source=SOURCE,
        type=TYPE,
        category=category,
        resolved_at=datetime.now(UTC).isoformat(),
        resolution_notes=note,
    )


async def record_new_events(
    db,
    *,
    state_file: Path | None = None,
    now_mono: float | None = None,
    boot_id: str | None = None,
    timer_state: TimerState | None = None,
) -> int:
    """Record new watchdog events as observations; returns the rows created.

    ``state_file`` defaults to ``$GENESIS_TSWD_STATE_FILE`` (the test suite
    points it at a temp path), else the watchdog's own default. Never raises:
    a broken read must not break the awareness tick.
    """
    if db is None:
        return 0
    try:
        if state_file is None:
            override = os.environ.get("GENESIS_TSWD_STATE_FILE")
            state_file = Path(override) if override else DEFAULT_STATE_FILE
        now = time.monotonic() if now_mono is None else now_mono
        boot = boot_id or _boot_id()
        state = _read_state(state_file)
        created = 0

        # Silence: CLOCK_MONOTONIC is shared with the root watchdog (same
        # kernel), so a run's age needs no wall clock.
        last = state.get("last_check_mono") if state else None
        fresh = (
            state is not None
            and state.get("boot_id") == boot
            and isinstance(last, (int, float))
            and not isinstance(last, bool)
            and now - last <= _SILENT_AFTER_S
        )
        if fresh:
            await _resolve(db, CATEGORY_SILENT, "the Tailscale watchdog is reporting again")
            # Reporting, but judging nothing: tailscaled down, the CLI missing or
            # failing, or a status it cannot read (a Tailscale upgrade that
            # changed the format). The journal says so every run; nobody reads
            # it, so after a few runs in a row this says it here.
            blind_runs = state.get("blind_runs")
            action = state.get("last_action")
            if (
                action in _BLIND_ACTIONS
                and type(blind_runs) is int
                and blind_runs >= _BLIND_AFTER_RUNS
            ):
                day = datetime.now(UTC).date().isoformat()
                created += await _create(
                    db,
                    key=f"blind:{boot}:{day}:{action}",
                    priority="high",
                    category=CATEGORY_BLIND,
                    content=(
                        "The Tailscale tunnel watchdog is running but cannot check any"
                        f" tunnel ({action} for {blind_runs} runs in a row): tailscaled is"
                        " not running, the tailscale CLI is failing, or its status output"
                        " could not be read. A stuck tunnel will be neither healed nor"
                        " reported. Check: sudo systemctl status tailscaled; tailscale"
                        " status --json."
                    ),
                )
            elif action not in _BLIND_ACTIONS:
                await _resolve(db, CATEGORY_BLIND, "the Tailscale watchdog can check tunnels again")
        else:
            timer = await (timer_state or _systemd_timer_state)()
            if timer and timer.get("UnitFileState") == "enabled":
                try:
                    since = int(timer.get("ActiveEnterTimestampMonotonic", "0")) / 1e6
                except ValueError:
                    since = 0.0
                running = timer.get("ActiveState") == "active"
                if not running or (since > 0 and now - since > _SILENT_AFTER_S):
                    day = datetime.now(UTC).date().isoformat()
                    created += await _create(
                        db,
                        key=f"silent:{boot}:{day}",
                        priority="high",
                        category=CATEGORY_SILENT,
                        content=(
                            "The Tailscale tunnel watchdog has not reported for over"
                            f" {_SILENT_AFTER_S // 60} minutes (timer "
                            f"{'active' if running else 'NOT active'}), so a stuck tunnel"
                            " will be neither healed nor reported. Check: sudo systemctl"
                            " status genesis-tailscale-watchdog.service"
                            " genesis-tailscale-watchdog.timer."
                        ),
                    )

        events = state.get("events") if state else None
        for event in events if isinstance(events, list) else []:
            if not _valid_event(event):
                continue
            priority, category, content = _content(event)
            if await _create(
                db,
                key=f"{event['incident']}:{event['action']}",
                priority=priority,
                category=category,
                content=content,
            ):
                created += 1
                logger.warning(
                    "tailscale watchdog event recorded: %s (%s)", event["action"], priority
                )

        if fresh and state.get("last_action") == "none":
            await _resolve(
                db,
                CATEGORY_DOWN,
                "the Tailscale watchdog's latest run found tailscaled active and every tunnel answering",
            )
        return created
    except Exception:
        logger.warning("tailscale watchdog event check failed", exc_info=True)
        return 0
