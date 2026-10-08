"""Container-swap reconciler — HOST-SIDE (guardian).

``limits.memory.swap true`` is set by host-setup.sh at container creation and
on manual re-runs, and #1079 activates it live there too — but an install that
advances via bare ``git pull`` (observed on a sibling install) never re-runs
host-setup, so the knob can sit unset (and the live cgroup at
``memory.swap.max=0``) indefinitely. Every memory spike then becomes the
load-100 D-state OOM-thrash wedge instead of degrading into swap pressure —
the exact failure the setting exists to prevent, silent until it fires.

The guardian is the only Genesis component that runs host-side with incus +
sudo access *continuously*, so it reconciles on OBSERVED state each tick:

1. **Persistent**: ``incus config get limits.memory.swap`` is not already
   swap-on (``_is_swap_on``: any Incus TRUE spelling, or a parseable byte-size
   ceiling) → ``incus config set ... true`` (covers unset, every FALSE
   spelling, and unparseable garbage; applies at every future container
   start).
2. **Live**: cgroup ``memory.swap.max == "0"`` → write ``max`` now — what
   incus would have written at start (``cgroup_ops.activate_swap_max``, the
   guardian-side twin of scripts/lib/container_swap.sh).

Healthy path = two cheap reads, no writes, no alerts. A heal emits one INFO
alert (guardian self-actions must be visible); a failed heal emits a WARNING
throttled by a state file (memory_watch idiom) so a persistent fault pages
daily, not per-tick. Never raises into the tick.

Deliberate override note: an operator who explicitly set
``limits.memory.swap=false`` will be reconciled back to ``true`` — swap-on is
a Genesis install invariant (docs/reference/memory-resilience.md). Disable the
reconciler itself (``swap_reconcile_enabled: false`` in guardian config) to
opt a host out.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from genesis.guardian._subprocess import run_subprocess as _run_subprocess
from genesis.guardian.alert.base import Alert, AlertSeverity
from genesis.guardian.cgroup_ops import activate_swap_max, read_swap_max

logger = logging.getLogger(__name__)

# A persistent failure re-pages at most this often (state-file throttle).
_REALERT_HOURS = 24.0

_INCUS_TIMEOUT = 10.0

# Suffixes Incus's own ``units.ParseByteSizeString`` recognizes for
# ``limits.memory.swap`` (shared/units/units.go, v6.0.0 and main, read
# 2026-10-07). A bare integer (suffix "") is bytes. This mirrors Incus's
# parser closely enough to tell a byte-valued ceiling apart from garbage; it
# does not need to be a full re-implementation, since Incus itself is the
# authority that actually applies the value.
_INCUS_BYTE_SUFFIXES = frozenset(
    {
        "",
        "B",
        " bytes",
        "kB",
        "MB",
        "GB",
        "TB",
        "PB",
        "EB",
        "KiB",
        "MiB",
        "GiB",
        "TiB",
        "PiB",
        "EiB",
    }
)

# Incus's own boolean-word lists (shared/util/boolean.go, v6.0.0 and main,
# read 2026-10-07: `IsTrue` = {"true","1","yes","on"}, `IsFalse` =
# {"false","0","no","off"}, case-insensitive). driver_lxc.go checks these
# BEFORE trying to parse a byte size — `if IsTrueOrEmpty(v) || IsFalse(v) {
# SetMemorySwapLimit(0) } else { parse as byte size }` — so the bare digit
# strings "0" and "1" are claimed by the boolean check and NEVER reach the
# byte-size parser, even though they also look like valid byte sizes (0
# bytes, 1 byte). Checking these word lists first is required, not cosmetic:
# without it, an operator's explicit "0" (deliberately off) would be
# misread as "a 0-byte ceiling" and left unreconciled, silently defeating
# the guardian's own "false is always reconciled back to true" invariant.
_INCUS_TRUE_WORDS = frozenset({"true", "1", "yes", "on"})
_INCUS_FALSE_WORDS = frozenset({"false", "0", "no", "off"})


_ASCII_DIGITS = frozenset("0123456789")


def _is_parseable_incus_size(raw: str) -> bool:
    """Whether Incus would accept ``raw`` as a ``limits.memory.swap`` byte
    size (a leading run of digits plus one of its known suffixes). Callers
    must rule out Incus's own boolean words first (see ``_is_swap_on``) —
    this function alone cannot tell "0" the byte size from "0" the bool.

    Matched on ASCII digits only, deliberately narrower than ``str.isdigit()``
    (which also accepts Unicode digit forms Go's byte-wise
    ``strconv.Atoi`` — the actual parser this mirrors — would reject): Incus
    validates this value server-side with the identical grammar at write time
    (``internal/instance/config.go``'s ``validate.IsSize``, which itself calls
    ``units.ParseByteSizeString``), so a value read back from ``incus config
    get`` can never contain one anyway. Matching the narrower grammar removes
    the gap rather than relying on that unreachability."""
    if not raw:
        return False
    i = 0
    while i < len(raw) and raw[i] in _ASCII_DIGITS:
        i += 1
    if i == 0:
        return False
    return raw[i:] in _INCUS_BYTE_SUFFIXES


def _is_swap_on(raw: str) -> bool:
    """Whether this ``limits.memory.swap`` value already explicitly encodes
    swap-on, so the reconciler should leave it alone: any Incus TRUE
    spelling, or a parseable byte-size ceiling (the native swap-ceiling
    form — Incus parses anything that is not an ``IsTrueOrEmpty``/``IsFalse``
    boolean word as a byte size; see driver_lxc.go). Empty/unset and any
    Incus FALSE spelling are NOT swap-on — both still need the reconciling
    'set true' call below, unchanged from today; this is the guardian's
    existing "deliberate override: false is always reconciled back to true"
    policy, now applied to every spelling of false, not just the literal
    word "false"."""
    stripped = raw.strip()
    lowered = stripped.lower()
    if not stripped or lowered in _INCUS_FALSE_WORDS:
        return False
    if lowered in _INCUS_TRUE_WORDS:
        return True
    return _is_parseable_incus_size(stripped)


async def _send(dispatcher, severity: AlertSeverity, title: str, body: str) -> None:
    try:
        await dispatcher.send(Alert(severity=severity, title=title, body=body))
    except Exception:
        logger.warning("swap_watch alert dispatch failed", exc_info=True)


def _failure_alert_due(state_file, now: datetime) -> bool:
    """True when the WARNING throttle window has elapsed (or no state yet)."""
    if not state_file.exists():
        return True
    try:
        data = json.loads(state_file.read_text())
        raw_at = data.get("last_failure_alert_at")
        if not raw_at:
            return True
        last = datetime.fromisoformat(raw_at)
        return (now - last).total_seconds() >= _REALERT_HOURS * 3600
    except (ValueError, OSError, TypeError, AttributeError):
        # TypeError: aware-vs-naive timestamp; AttributeError: valid JSON that
        # isn't a dict. Both mean "state unusable" -> alert is due, and neither
        # may escape (module contract: never raises into the tick).
        return True


def _record_failure_alert(state_file, now: datetime) -> None:
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"last_failure_alert_at": now.isoformat()}))
    except OSError:
        logger.warning("could not persist swap_watch alert state", exc_info=True)


async def check_container_swap_and_alert(config, dispatcher) -> None:
    """Reconcile the container-swap invariant; alert on heal or failure.

    Never raises into the tick. An unreadable signal (incus down, container
    stopped, cgroup absent) is "no signal", not a defect — container-down is
    the state machine's job, not this watch's.
    """
    if not getattr(config, "swap_reconcile_enabled", True):
        return

    container = config.container_name
    healed: list[str] = []
    problems: list[str] = []

    # 1. Persistent knob. `incus config get` on an unset key returns rc=0 with
    # empty output, so unset and explicit-false both land in the set branch.
    try:
        # --expanded so a profile-inherited true reads as true (a plain get is
        # local-only and would trigger a redundant local override + a false
        # "was off" heal alert). Verified supported on live incus.
        rc, stdout, _stderr = await _run_subprocess(
            "incus",
            "config",
            "get",
            "--expanded",
            container,
            "limits.memory.swap",
            timeout=_INCUS_TIMEOUT,
        )
    except Exception:
        logger.warning("swap_watch: incus config get failed", exc_info=True)
        rc, stdout = 1, ""
    config_verified = rc == 0
    if rc == 0:
        raw_value = stdout.strip()
        value = raw_value.lower()
        if not _is_swap_on(raw_value):
            try:
                rc_set, _out, err_set = await _run_subprocess(
                    "incus",
                    "config",
                    "set",
                    container,
                    "limits.memory.swap",
                    "true",
                    timeout=_INCUS_TIMEOUT,
                )
            except Exception as exc:
                rc_set, err_set = 1, str(exc)
            if rc_set == 0:
                healed.append(
                    f"limits.memory.swap: {value or 'unset'} → true (persists across restarts)",
                )
            else:
                problems.append(
                    f"incus config set limits.memory.swap=true failed: {err_set.strip()}",
                )
    else:
        logger.debug("swap_watch: no incus config signal (rc=%s)", rc)

    # 2. Live cgroup. Only "0" is the defect; None = no signal (stopped
    # container / cgroup v1), any other value already permits swap.
    current = await read_swap_max(container)
    if current == "0":
        if await activate_swap_max(container):
            if config_verified:
                healed.append("memory.swap.max: 0 → max (live, no restart needed)")
            else:
                # Live-healed, but the config read failed so the PERSISTENT knob
                # was never verified/set — a restart can revert memory.swap.max
                # to 0. Don't declare a clean "reconciled"; surface it so the
                # persistent half gets fixed. (Config set failing outright
                # already lands in problems above; this covers the read failing
                # while the cgroup was still writable — e.g. an incus socket
                # hiccup with the container running.)
                problems.append(
                    "activated swap live (memory.swap.max 0 → max) but could NOT "
                    "verify the persistent limits.memory.swap knob (incus config "
                    "read failed) — a container restart may revert swap to off",
                )
        else:
            problems.append(
                "live cgroup write failed — swap stays off until the next "
                "container start (set the persistent knob and restart to apply)",
            )

    if healed:
        logger.info("swap_watch healed: %s", "; ".join(healed))
        await _send(
            dispatcher,
            AlertSeverity.INFO,
            "Guardian enabled container swap",
            "Container-swap invariant reconciled on "
            f"'{container}':\n- "
            + "\n- ".join(healed)
            + "\n\nWithout this, a memory spike wedges the box into D-state "
            "thrash instead of degrading into swap. If swap-off was "
            "intentional, set swap_reconcile_enabled: false in guardian "
            "config.",
        )

    if problems:
        logger.warning("swap_watch problems: %s", "; ".join(problems))
        now = datetime.now(UTC)
        state_file = config.state_path / "swap_watch_state.json"
        if _failure_alert_due(state_file, now):
            await _send(
                dispatcher,
                AlertSeverity.WARNING,
                "Container swap reconcile FAILED",
                f"Guardian could not enforce the swap invariant on '{container}':\n- "
                + "\n- ".join(problems),
            )
            _record_failure_alert(state_file, now)
