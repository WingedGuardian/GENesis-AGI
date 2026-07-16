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

1. **Persistent**: ``incus config get limits.memory.swap`` != ``true`` →
   ``incus config set`` (covers unset AND false; applies at every future
   container start).
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
    except (ValueError, OSError):
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
        rc, stdout, _stderr = await _run_subprocess(
            "incus",
            "config",
            "get",
            container,
            "limits.memory.swap",
            timeout=_INCUS_TIMEOUT,
        )
    except Exception:
        logger.warning("swap_watch: incus config get failed", exc_info=True)
        rc, stdout = 1, ""
    if rc == 0:
        value = stdout.strip().lower()
        if value != "true":
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
            healed.append("memory.swap.max: 0 → max (live, no restart needed)")
        else:
            problems.append(
                "live cgroup write failed — swap stays off until the next "
                "container start (config is set, so a restart will apply it)",
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
