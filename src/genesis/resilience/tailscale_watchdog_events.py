"""Turn the root Tailscale watchdog's per-run evidence into Genesis observations.

The watchdog (``scripts/systemd/genesis-tailscale-watchdog.py``) runs as root
and writes only its own file, ``/run/genesis-tailscale-watchdog.json`` (0644):
what its latest run observed, and nothing it has to remember. The awareness tick
calls :func:`record_new_events` as the owning user every ~5 minutes.

**The condition lives here, in one alert per peer.** The file's ``evidence``
gives a verdict per peer the run could judge:

* ``stuck`` raises the peer's ``critical`` alert (paged once by the
  critical-observations job), unless one is already open.
* ``ok`` (a fresh handshake, or the tunnel answered) or ``offline`` (the peer
  itself is gone, so there is no stuck tunnel to report) resolves it.
* A peer missing from a COMPLETE ``present`` list has left the tailnet, and its
  alert resolves.
* Anything else is unknown and changes nothing. A skipped, idle or unreadable
  peer never clears an alert, and nothing depends on state the watchdog could
  lose.

Evidence is acted on only while the file is fresh (this boot, rewritten within
:data:`_SILENT_AFTER_S`).

Two refinements keep this from paging too much:

* A tunnel stuck again within :data:`_REOPEN_S` of its alert being resolved,
  by this module (a flap) or by anyone else while it was still true (the owner,
  a reflection prune), REOPENS that alert instead of raising a new one. A
  reopened row keeps its ``surfaced_at``, so it does not page again.
* A stuck episode more than :data:`_REOPEN_S` after the last one is a new alert
  and pages, and so is one after the alert expired or was withdrawn (below).

**Expiry.** Every run that still finds the tunnel stuck moves the alert's
expiry to :data:`_STUCK_TTL` ahead, so it lives while the tunnel is seen stuck
and ends on the store's expiry sweep a day after it was last seen. A peer
nobody connects to again produces no evidence at all, and without this its
alert would stay open forever.

**The off switch.** When the watchdog is turned off (``off`` mode in a fresh
file, the durable switch; or, once the file has gone stale, a timer that is
masked, disabled or not installed, or a masked service) every open alert from
this source is WITHDRAWN: resolved with a note saying nothing is watching any
more, which is not a claim that the tunnel recovered. An unreadable timer state
changes nothing.

**Restart outcomes** are one-off events, one row each (keyed on the event id).
``restart-failed``, ``unverified`` and ``pending`` are ``critical`` (the daemon
itself may be down or hung), and resolve once a later run finds tailscaled
running and its status readable. ``healed`` is ``high``: the owner felt the SSH
drop, and the tunnel is back, so there is nothing left to do (when one run both
finds and heals a tunnel, no stuck-peer alert is ever raised).
``restart-no-effect``, ``restart-unconfirmed`` (the tunnel could not be
checked afterwards) and ``not-restarted`` are ``high`` because the stuck-peer
alert, which stays open, carries the page; these say what the restart did.

**The watchdog's own health**, both ``high``, both raised again by a new episode
after one resolves:

* silent: the timer is enabled but the file is missing or stale, and the
  oneshot is not mid-run;
* blind: it reports but judged no tunnel for :data:`_BLIND_AFTER_RUNS` runs in
  a row.

That is the only way its failures reach anyone: this user cannot read the
system journal.

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
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = Path("/run/genesis-tailscale-watchdog.json")
TIMER_UNIT = "genesis-tailscale-watchdog.timer"
SERVICE_UNIT = "genesis-tailscale-watchdog.service"
SOURCE = "tailscale_watchdog_monitor"
TYPE = "infrastructure_alert"
CATEGORY_DOWN = "tailscale_tunnel_down"
CATEGORY_RESTART = "tailscale_restart"
CATEGORY_SILENT = "tailscale_watchdog_silent"
CATEGORY_BLIND = "tailscale_watchdog_blind"
_MAX_STATE_BYTES = 1_000_000
#: The timer fires every ~2 minutes; three missed runs plus slack is silence.
_SILENT_AFTER_S = 600
_BLIND_AFTER_RUNS = 3
_REOPEN_S = 3600
#: How long a stuck-tunnel alert outlives the last run that saw it stuck.
_STUCK_TTL = timedelta(hours=24)
#: Marks the resolutions this module makes, for whoever reads the row.
_NOTE = "[tailscale-watchdog] "
_WITHDRAWN = _NOTE + "withdrawn: "
#: The store's expiry sweep (``observations.resolve_expired``) writes this.
_EXPIRED_NOTE = "auto-expired (TTL)"
#: Timer unit-file states that mean the watchdog was turned off or removed
#: ("" is what systemd reports for a unit that is not installed).
_TIMER_OFF_STATES = frozenset({"masked", "masked-runtime", "disabled", ""})
_MAX_PEERS = 1000
_MAX_TARGETS = 20
_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
_EVIDENCE_TEXT = (
    " Evidence: /run/genesis-tailscale-watchdog.json and the root-only status snapshot beside it."
)
_CRITICAL_ACTIONS = frozenset({"restart-failed", "unverified", "pending"})
#: The same contract as the watchdog's EVENT_ACTIONS.
_ACTIONS = _CRITICAL_ACTIONS | {
    "healed",
    "restart-no-effect",
    "restart-unconfirmed",
    "not-restarted",
}
#: Runs after which tailscaled is known to be running with a readable status.
_DAEMON_OK_ACTIONS = frozenset(
    {
        "none",
        "suspect-unreachable",
        "incomplete",
        "stuck",
        "ratelimited",
        "healed",
        "restart-no-effect",
        "restart-unconfirmed",
    }
)

TimerState = Callable[[], Awaitable[dict[str, str] | None]]


def _valid_ip(value: Any) -> bool:
    try:
        return isinstance(value, str) and str(ipaddress.IPv4Address(value)) == value
    except ValueError:
        return False


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _valid_event(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    try:
        return (
            isinstance(event["id"], str)
            and len(event["id"]) <= 128
            and event["action"] in _ACTIONS
            and isinstance(event["peers"], list)
            and 0 < len(event["peers"]) <= _MAX_TARGETS
            and all(_valid_ip(ip) for ip in event["peers"])
            and isinstance(event["cleared"], list)
            and all(ip in event["peers"] for ip in event["cleared"])
            and isinstance(event.get("unconfirmed", []), list)
            and all(ip in event["peers"] for ip in event.get("unconfirmed", []))
            and (event["rc"] is None or type(event["rc"]) is int)
            and type(event["rate_limit_s"]) is int
        )
    except (KeyError, TypeError):
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


def _minutes(seconds: int) -> str:
    return f"{max(1, seconds // 60)} min"


def _stuck_content(ip: str, age: Any, mode: Any, capped: bool = False) -> str:
    if mode == "observe":
        action = "The watchdog is in observe mode, so it will not restart tailscaled."
    elif capped:
        action = (
            "The watchdog has STOPPED restarting tailscaled for it: restarts this boot did"
            " not clear it, so the fault is probably not on this node. Check the peer, or"
            " restart tailscaled by hand; the watchdog tries again once the tunnel has been"
            " seen working."
        )
    else:
        action = (
            "The watchdog restarts tailscaled for it at most once per rate-limit window;"
            " restart outcomes are reported separately."
        )
    return (
        f"The Tailscale tunnel to {ip} is stuck: no WireGuard handshake"
        + (f" for {age}s" if type(age) is int else "")
        + " while traffic is wanted, and the tunnel ping gets no reply though the peer"
        f" answers discovery pings. Fix: sudo systemctl restart tailscaled. {action}"
        " This alert resolves itself when the tunnel answers again." + _EVIDENCE_TEXT
    )


def _event_content(event: dict[str, Any]) -> tuple[str, str]:
    """(priority, content). The first sentence says what happened or what to
    do: the critical page shows only the start of the content."""
    peers = ", ".join(event["peers"])
    unconfirmed = event.get("unconfirmed", [])
    left = [ip for ip in event["peers"] if ip not in event["cleared"] and ip not in unconfirmed]
    wait = f"the next automatic restart is allowed after {_minutes(event['rate_limit_s'])}"
    check = " Check: sudo systemctl status tailscaled."
    text = {
        "healed": f"The Tailscale watchdog restarted tailscaled and the stuck tunnel(s) to"
        f" {peers} answer again. Tailscale SSH sessions on this machine dropped (tmux"
        f" sessions survive); {wait}.",
        "restart-no-effect": f"The Tailscale watchdog restarted tailscaled, which came back,"
        f" but the tunnel(s) to {', '.join(left)} still get no reply; {wait}. Check the peer"
        " and this node's relay (tailscale netcheck).",
        "restart-unconfirmed": "The Tailscale watchdog restarted tailscaled, which came back,"
        f" but could not check the tunnel(s) to {', '.join(unconfirmed)} afterwards (the"
        " tailscale CLI did not give an answer); the stuck-tunnel alert stays open until a"
        f" later run sees the tunnel answer; {wait}.",
        "not-restarted": f"The Tailscale watchdog's restart of tailscaled failed"
        f" (rc={event['rc']}) and nothing restarted; the tunnel(s) to {peers} are still"
        f" stuck; {wait}. Fix: sudo systemctl restart tailscaled.",
        "restart-failed": "Tailscale may be DOWN: the watchdog restarted tailscaled and it did"
        " not come back." + check + " Fix: sudo systemctl restart tailscaled.",
        "unverified": "The Tailscale watchdog restarted tailscaled but could not read whether"
        " it came back." + check,
        "pending": "The Tailscale watchdog's restart of tailscaled had not finished when it"
        " stopped waiting; tailscaled may be hung." + check,
    }[event["action"]]
    priority = "critical" if event["action"] in _CRITICAL_ACTIONS else "high"
    return priority, text + _EVIDENCE_TEXT


def _hash(key: str) -> str:
    return hashlib.sha256(f"tailscale_watchdog:{key}".encode()).hexdigest()


def _boot_id() -> str:
    try:
        return _BOOT_ID.read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def _now() -> datetime:
    return datetime.now(UTC)


async def _systemctl_show(unit: str, *props: str) -> dict[str, str] | None:
    systemctl = os.environ.get("GENESIS_TSWD_SYSTEMCTL") or "systemctl"
    argv = [systemctl, "show", unit]
    for prop in props:
        argv += ["-p", prop]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
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


async def _systemd_timer_state() -> dict[str, str] | None:
    """The timer's enablement and activation, plus whether its oneshot is
    mid-run (a long run legitimately leaves the file untouched), or None."""
    timer = await _systemctl_show(
        TIMER_UNIT, "UnitFileState", "ActiveState", "ActiveEnterTimestampMonotonic"
    )
    if timer is None:
        return None
    service = await _systemctl_show(SERVICE_UNIT, "ActiveState", "UnitFileState") or {}
    timer["ServiceActiveState"] = service.get("ActiveState", "")
    timer["ServiceUnitFileState"] = service.get("UnitFileState", "")
    return timer


async def _create(
    db,
    *,
    key: str,
    priority: str,
    category: str,
    content: str,
    once: bool,
    expires_at: str | None = None,
) -> bool:
    """Create the row for ``key`` unless one exists. With ``once``, ANY row
    (resolved too) counts, so it is never raised again; without it, only an
    unresolved one, so a new episode after recovery is raised. The awareness
    tick is the only writer for this source."""
    from genesis.db.crud import observations

    content_hash = _hash(key)
    if once and await observations.exists_by_hash(db, source=SOURCE, content_hash=content_hash):
        return False
    created = await observations.create(
        db,
        id=str(uuid.uuid4()),
        source=SOURCE,
        type=TYPE,
        category=category,
        content=content,
        priority=priority,
        created_at=_now().isoformat(),
        content_hash=content_hash,
        skip_if_duplicate=True,
        expires_at=expires_at,
    )
    return bool(created)


async def _resolve(
    db,
    category: str | None,
    note: str,
    *,
    hashes: set[str] | None = None,
    keep: set[str] | None = None,
    priority: str | None = None,
) -> int:
    """Resolve this source's open rows in ``category`` (every category when
    None): only ``hashes`` when given, all but ``keep`` otherwise, optionally
    only one ``priority``. ``note`` is the full resolution note."""
    from genesis.db.crud import observations

    return await observations.resolve_by_source_and_type(
        db,
        source=SOURCE,
        type=TYPE,
        category=category,
        content_hashes=hashes,
        exclude_content_hashes=keep,
        priority=priority,
        resolved_at=_now().isoformat(),
        resolution_notes=note,
    )


async def _raise_stuck(db, ip: str, age: Any, mode: Any, capped: bool = False) -> bool:
    """Raise, reopen, or keep alive the peer's alert. True if a new row was
    made. Every call moves the open alert's expiry to :data:`_STUCK_TTL` ahead
    and rewrites its text to the current age and mode (an operator may have
    switched between live and observe), without paging again."""
    from genesis.db.crud import observations

    expires_at = (_now() + _STUCK_TTL).isoformat()
    content = _stuck_content(ip, age, mode, capped)
    row = await observations.latest_by_hash(db, source=SOURCE, content_hash=_hash(f"stuck:{ip}"))
    if row is not None:
        if not row["resolved"]:
            await observations.set_expires_at(db, row["id"], expires_at)
            await observations.update_content(db, row["id"], content)
            return False
        notes = row.get("resolution_notes") or ""
        # Expired (unseen for a day) or withdrawn (the watchdog was off): a new
        # sighting is a new alert and pages.
        ended = notes == _EXPIRED_NOTE or notes.startswith(_WITHDRAWN)
        try:
            since = (_now() - datetime.fromisoformat(row["resolved_at"])).total_seconds()
        except (TypeError, ValueError):
            since = float("inf")
        if not ended and since <= _REOPEN_S:
            # Still or again stuck within the hour: a flap, or someone (the
            # owner, a reflection prune) resolved it while it was still true.
            # Bring the same alert back; it keeps its surfaced_at, so it does
            # not page again.
            await observations.reopen(db, row["id"], expires_at=expires_at)
            await observations.update_content(db, row["id"], content)
            return False
    created = await _create(
        db,
        key=f"stuck:{ip}",
        priority="critical",
        category=CATEGORY_DOWN,
        once=False,
        expires_at=expires_at,
        content=content,
    )
    if created:
        logger.warning("tailscale tunnel to %s is stuck", ip)
    return created


async def _watchdog_health(db, state, fresh: bool, boot: str, now: float, timer) -> int:
    if fresh:
        await _resolve(db, CATEGORY_SILENT, _NOTE + "the Tailscale watchdog is reporting again")
        blind_runs = state.get("blind_runs")
        if type(blind_runs) is int and blind_runs >= _BLIND_AFTER_RUNS:
            return int(
                await _create(
                    db,
                    key=f"blind:{boot}",
                    priority="high",
                    category=CATEGORY_BLIND,
                    once=False,
                    content=(
                        "The Tailscale tunnel watchdog is running but has checked no"
                        f" tunnel for {blind_runs} runs in a row: tailscaled is not"
                        " running, the tailscale CLI is failing, its status output could"
                        " not be read, or its probe limits are set to zero. A stuck tunnel"
                        " will be neither healed nor reported. Check: sudo systemctl"
                        " status tailscaled; tailscale status --json."
                    ),
                )
            )
        await _resolve(
            db,
            CATEGORY_BLIND,
            _NOTE
            + (
                "tailscaled is turned off, so there is nothing to watch"
                if state.get("last_action") == "tailscaled-off"
                else "the Tailscale watchdog can check tunnels again"
            ),
        )
        return 0
    if not timer or timer.get("UnitFileState") != "enabled":
        return 0  # turned off (the caller withdrew its alerts) or unknown
    if timer.get("ServiceActiveState") in ("activating", "active"):
        return 0  # a long run is still in progress
    try:
        since = int(timer.get("ActiveEnterTimestampMonotonic", "0")) / 1e6
    except ValueError:
        since = 0.0
    running = timer.get("ActiveState") == "active"
    if running and not (since > 0 and now - since > _SILENT_AFTER_S):
        return 0  # the timer (re)started recently; give it time to report
    return int(
        await _create(
            db,
            key=f"silent:{boot}",
            priority="high",
            category=CATEGORY_SILENT,
            once=False,
            content=(
                f"The Tailscale tunnel watchdog has not reported for over"
                f" {_SILENT_AFTER_S // 60} minutes (timer"
                f" {'active' if running else 'NOT active'}), so a stuck tunnel will be"
                " neither healed nor reported. Check: sudo systemctl status"
                " genesis-tailscale-watchdog.service genesis-tailscale-watchdog.timer."
            ),
        )
    )


async def _apply_evidence(db, state: dict[str, Any]) -> int:
    created = 0
    evidence = state.get("evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    ages = state.get("handshake_age_s")
    ages = ages if isinstance(ages, dict) else {}
    capped_list = state.get("capped")
    capped = (
        {ip for ip in capped_list[:_MAX_PEERS] if _valid_ip(ip)}
        if isinstance(capped_list, list)
        else set()
    )
    cleared: set[str] = set()
    for ip, verdict in list(evidence.items())[:_MAX_PEERS]:
        if not _valid_ip(ip):
            continue
        if verdict == "stuck":
            created += await _raise_stuck(db, ip, ages.get(ip), state.get("mode"), ip in capped)
        elif verdict in ("ok", "offline"):
            cleared.add(_hash(f"stuck:{ip}"))
    await _resolve(
        db,
        CATEGORY_DOWN,
        _NOTE + "the Tailscale watchdog found this tunnel answering again, or the peer offline",
        hashes=cleared,
    )
    present = state.get("present")
    if state.get("present_complete") is True and isinstance(present, list):
        keep = {_hash(f"stuck:{ip}") for ip in present[:_MAX_PEERS] if _valid_ip(ip)}
        await _resolve(
            db, CATEGORY_DOWN, _NOTE + "this peer is no longer in the tailnet", keep=keep
        )
    return created


async def record_new_events(
    db,
    *,
    state_file: Path | None = None,
    now_mono: float | None = None,
    boot_id: str | None = None,
    timer_state: TimerState | None = None,
) -> int:
    """Mirror the watchdog's file into observations; returns the rows created.

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
        # CLOCK_MONOTONIC is shared with the root watchdog (same kernel), so a
        # run's age needs no wall clock.
        last = state.get("last_check_mono") if state else None
        fresh = (
            state is not None
            and state.get("boot_id") == boot
            and _number(last)
            and now - last <= _SILENT_AFTER_S
        )
        timer = None
        if fresh:
            # Running and reporting, whatever its enablement: off only if its
            # own mode says so.
            off = state.get("mode") == "off"
        else:
            timer = await (timer_state or _systemd_timer_state)()
            off = timer is not None and (
                timer.get("UnitFileState") in _TIMER_OFF_STATES
                # A masked service cannot run, whatever the timer says.
                or timer.get("ServiceUnitFileState", "").startswith("masked")
            )
        if off:
            await _resolve(
                db,
                None,
                _WITHDRAWN + "the Tailscale watchdog was turned off, so nothing is watching"
                " this any more; this is not a recovery",
            )
            return 0
        created = await _watchdog_health(db, state, fresh, boot, now, timer)
        if not fresh:
            return created  # old evidence is not evidence of now

        created += await _apply_evidence(db, state)
        # Restart outcomes: one row per event, ever.
        events = state.get("events")
        for event in events if isinstance(events, list) else []:
            if not _valid_event(event):
                continue
            priority, content = _event_content(event)
            if await _create(
                db,
                key=f"event:{event['id']}",
                priority=priority,
                category=CATEGORY_RESTART,
                content=content,
                once=True,
            ):
                created += 1
                logger.warning("tailscale watchdog: %s (%s)", event["action"], priority)
        # After the events, so a failed restart that has already recovered is
        # recorded but never paged.
        if state.get("last_action") in _DAEMON_OK_ACTIONS:
            await _resolve(
                db,
                CATEGORY_RESTART,
                _NOTE + "tailscaled is running and its status is readable",
                priority="critical",
            )

        return created
    except Exception:
        logger.warning("tailscale watchdog event check failed", exc_info=True)
        return 0
