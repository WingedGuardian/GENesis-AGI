"""Container-side provisioning coordinator (the Genesis-UP approval owner).

When Genesis is up it owns approval: it reads the user's reply on its OWN bot
with zero getUpdates contention (the main bot is the only poller). This module
fetches host capacity via the read-only gateway verb, asks the user to
APPROVE/DENY on their own channel, and only on APPROVE invokes the host gateway
EXECUTE verb — which re-checks the due-diligence gate and executes host-side.

No mutation code lives here. All hypervisor/LVM mutation is host-side in the
guardian; the container only orchestrates approval + dispatches the verb. The
functions take injected ``remote`` (a GuardianRemote) and ``ask`` (an async
proposal→reply callable) so they are testable without the live outreach stack.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from genesis.util.tasks import tracked_task

logger = logging.getLogger(__name__)

# ask(proposal_text) -> the user's reply text, or None on timeout.
AskFn = Callable[[str], Awaitable[str | None]]
# notify(text) -> fire-and-forget message to the owner (poller terminal states).
NotifyFn = Callable[[str], Awaitable[None]]

_GIB = 1024**3


def _fmt_gib(byts: object) -> str:
    try:
        return f"{int(byts) / _GIB:.0f}G"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"


def _disk_proposal(cap: dict, disk: str, add_gib: int) -> str:
    disks = cap.get("disks") or {}
    cur = disks.get(disk)
    cur_s = _fmt_gib(cur) if cur is not None else "unknown"
    new_s = _fmt_gib(cur + add_gib * _GIB) if isinstance(cur, (int, float)) else "?"
    return (
        f"🔧 Grow VM disk {disk} by {add_gib}G ({cur_s} → {new_s}).\n"
        f"Host storage free: {_fmt_gib(cap.get('storage_free_bytes'))}. "
        "Irreversible — one attempt, no auto-retry.\n"
        "Reply APPROVE to execute or DENY to cancel."
    )


def _approved(reply: str | None) -> bool:
    return (reply or "").strip().upper() == "APPROVE"


def _backup_is_stale(backup: dict) -> bool:
    """Would the host grow-gate's backup check refuse right now?

    Mirrors gate._backup_check: required + (age unknown OR over limit).
    Keyed on the STRUCTURED provision-status backup block, never on parsing
    display strings.
    """
    if not backup.get("require_recent_backup"):
        return False
    age = backup.get("age_days")
    try:
        limit = float(backup.get("backup_max_age_days", 14))
    except (TypeError, ValueError):
        limit = 14.0
    return age is None or float(age) > limit


def _chain_proposal(cap: dict, backup: dict, disk: str, add_gib: int,
                    wall_s: float) -> str:
    """One approval covers the WHOLE chain — so the text must name every leg
    and the time gap (the grow executes tens of minutes+ after this APPROVE,
    behind a fresh host-side due-diligence re-check)."""
    age = backup.get("age_days")
    age_s = f"{age:.1f}d old" if isinstance(age, (int, float)) else "none exists"
    disks = cap.get("disks") or {}
    cur = disks.get(disk)
    cur_s = _fmt_gib(cur) if cur is not None else "unknown"
    new_s = _fmt_gib(cur + add_gib * _GIB) if isinstance(cur, (int, float)) else "?"
    return (
        f"🔧 Grow VM disk {disk} by {add_gib}G ({cur_s} → {new_s}) — but the "
        f"hypervisor backup is stale ({age_s}; the safety gate requires "
        f"≤{backup.get('backup_max_age_days', 14)}d), so this runs as a CHAIN "
        "under this one approval:\n"
        "1. START a vzdump backup now (reads the whole live VM disk — expect "
        "I/O load; crash-consistent without a guest agent).\n"
        f"2. When it VERIFIES (typically minutes-to-an-hour; abandoned as "
        f"UNVERIFIED after {wall_s / 3600:.0f}h), old backups rotate and the "
        "grow executes AUTOMATICALLY after a fresh due-diligence re-check.\n"
        "3. If the backup fails or the re-check refuses, the grow does NOT "
        "run and you are notified — nothing retries on its own.\n"
        "Reply APPROVE to run the whole chain or DENY to cancel."
    )


async def _poll_vzdump(
    remote,
    notify: NotifyFn | None,
    *,
    upid: str,
    poll_interval_s: float,
    wall_s: float,
    on_verified: Callable[[], Awaitable[dict]] | None = None,
) -> dict:
    """Drive the host status verb to a terminal state (tracked-task body).

    Poll-outcome discipline: only state=="failed" is terminal failure;
    transport errors / "denied" / running / unknown are TRANSIENT and retried
    to the wall bound, which ends as UNVERIFIED (never "failed" — the backup
    may still land; the host ledger keeps the row unverified and honest).
    Host-side verify already alerts verified/failed via the guardian channel;
    ``notify`` carries the chain outcome + the wall-bound case.
    """
    deadline = time.monotonic() + wall_s
    while time.monotonic() < deadline:
        res = await remote.request_vzdump_status(upid)
        state = res.get("state", "")
        if state == "verified":
            if on_verified is None:
                return res
            follow = await on_verified()
            if notify is not None:
                if follow.get("ok"):
                    await notify(
                        f"✅ Chain complete: backup verified, grow executed "
                        f"({follow.get('requested', '')} → {follow.get('after', '?')}).",
                    )
                else:
                    await notify(
                        "⚠️ Backup verified but the grow leg did NOT run "
                        f"({follow.get('stage', follow.get('error', 'refused'))}). "
                        "Nothing retries on its own — re-request the grow when ready.",
                    )
            return follow
        if state == "failed":
            if notify is not None:
                await notify(
                    "❌ Backup task failed — "
                    + (f"the approved grow was NOT executed. ({res.get('error', '')})"
                       if on_verified is not None else f"({res.get('error', '')})"),
                )
            return res
        await asyncio.sleep(poll_interval_s)
    if notify is not None:
        await notify(
            f"⚠️ Backup {upid} not verified within {wall_s / 3600:.1f}h — left "
            "UNVERIFIED (it may still finish). Check `provision-vzdump-status`; "
            + ("the approved grow was NOT executed." if on_verified is not None else ""),
        )
    return {"ok": False, "stage": "wall_bound", "upid": upid}


async def coordinate_grow_disk(
    remote, ask: AskFn, *, disk: str, add_gib: int,
    notify: NotifyFn | None = None,
    poll_interval_s: float = 60.0,
    vzdump_wall_s: float = 7200.0,
) -> dict:
    """Capacity → ask the user → on APPROVE run the host disk-grow execute verb.

    JIT backup chain: when the host reports the backup gate would refuse
    (stale/absent backup) and nothing is in flight, the proposal becomes a
    backup→verify→grow CHAIN under one truthful approval; the grow leg runs
    from a tracked background task once the backup verifies (fresh host-side
    re-check included). The wall bound uses the project 2h floor — a full-VM
    dump legitimately runs long; this is not a safety timeout.
    """
    status = await remote.provision_status()
    if not status.get("ok"):
        return {"ok": False, "stage": "no_capacity", "detail": status}

    backup = status.get("backup") or {}
    if _backup_is_stale(backup):
        if backup.get("in_flight_upid"):
            return {"ok": False, "stage": "backup_in_flight",
                    "upid": backup["in_flight_upid"],
                    "detail": "a backup is already running — retry the grow "
                              "after it verifies"}
        reply = await ask(_chain_proposal(
            status.get("capacity", {}), backup, disk, add_gib, vzdump_wall_s,
        ))
        if not _approved(reply):
            return {"ok": False, "stage": "denied", "reply": reply}
        start = await remote.request_vzdump_start()
        if not start.get("ok"):
            return {"ok": False, "stage": "backup_start_failed", "detail": start}
        upid = str(start.get("upid", ""))

        async def _grow_leg() -> dict:
            return await remote.request_grow_disk(disk, add_gib)

        tracked_task(
            _poll_vzdump(
                remote, notify, upid=upid, poll_interval_s=poll_interval_s,
                wall_s=vzdump_wall_s, on_verified=_grow_leg,
            ),
            name=f"vzdump-chain-{disk}",
        )
        return {"ok": True, "stage": "chain_started", "upid": upid,
                "detail": "backup started; grow executes automatically once "
                          "it verifies (you will be notified either way)"}

    reply = await ask(_disk_proposal(status.get("capacity", {}), disk, add_gib))
    if not _approved(reply):
        return {"ok": False, "stage": "denied", "reply": reply}
    return await remote.request_grow_disk(disk, add_gib)


async def coordinate_vzdump(
    remote, ask: AskFn, *, notify: NotifyFn | None = None,
    poll_interval_s: float = 60.0, vzdump_wall_s: float = 7200.0,
) -> dict:
    """Explicit backup: ask the user → on APPROVE start a vzdump and track it.

    Two-phase: returns as soon as the backup STARTS (a full-VM dump runs for
    tens of minutes+); a tracked background task polls to the terminal state.
    The host side alerts verified/failed on the guardian channel; ``notify``
    additionally covers the wall-bound (abandoned-UNVERIFIED) case.
    """
    status = await remote.provision_status()
    if not status.get("ok"):
        return {"ok": False, "stage": "no_capacity", "detail": status}
    backup = status.get("backup") or {}
    if backup.get("in_flight_upid"):
        return {"ok": False, "stage": "backup_in_flight",
                "upid": backup["in_flight_upid"]}
    age = backup.get("age_days")
    age_s = f"{age:.1f}d old" if isinstance(age, (int, float)) else "none exists"
    proposal = (
        f"💾 Take a hypervisor backup (vzdump) of the host VM now? Newest: "
        f"{age_s}.\n"
        "Reads the whole live VM disk (expect I/O load; crash-consistent "
        "without a guest agent), lands on the backup datastore, then rotates "
        "old backups per keep-last.\n"
        "Reply APPROVE to start or DENY to cancel."
    )
    reply = await ask(proposal)
    if not _approved(reply):
        return {"ok": False, "stage": "denied", "reply": reply}
    start = await remote.request_vzdump_start()
    if not start.get("ok"):
        return {"ok": False, "stage": "backup_start_failed", "detail": start}
    upid = str(start.get("upid", ""))
    tracked_task(
        _poll_vzdump(
            remote, notify, upid=upid,
            poll_interval_s=poll_interval_s, wall_s=vzdump_wall_s,
        ),
        name="vzdump-verify",
    )
    return {"ok": True, "stage": "started", "upid": upid,
            "detail": "backup started; verification is tracked in the "
                      "background (host alerts on the outcome)"}


async def coordinate_grow_memory(remote, ask: AskFn, *, new_mib: int) -> dict:
    """Capacity → ask the user → on APPROVE run the host memory-grow execute verb."""
    status = await remote.provision_status()
    if not status.get("ok"):
        return {"ok": False, "stage": "no_capacity", "detail": status}
    cur = status.get("capacity", {}).get("vm_memory_mib")
    proposal = (
        f"🔧 Grow VM memory to {new_mib} MiB (current {cur} MiB).\n"
        "⚠️ Takes effect only after a VM reboot (~2 min downtime, scheduled).\n"
        "Reply APPROVE to execute or DENY to cancel."
    )
    reply = await ask(proposal)
    if not _approved(reply):
        return {"ok": False, "stage": "denied", "reply": reply}
    return await remote.request_grow_memory(new_mib)


async def coordinate_grow_root(remote, ask: AskFn, *, new_gb: int) -> dict:
    """Ask the user -> on APPROVE run the host container-root grow execute verb.

    Local incus op (not Proxmox): incus resizes the thin LV + filesystem online,
    no restart. The host verb enforces grow-only + a pool-headroom guard.
    """
    proposal = (
        f"\U0001f527 Grow the CONTAINER root volume to {new_gb}GB total.\n"
        "incus resizes the thin LV + filesystem online (no restart). Grow-only, "
        "refused if the thin pool is near-full.\n"
        "Reply APPROVE to execute or DENY to cancel."
    )
    reply = await ask(proposal)
    if not _approved(reply):
        return {"ok": False, "stage": "denied", "reply": reply}
    return await remote.request_grow_root(new_gb)


async def coordinate_set_container_limits(
    remote, ask: AskFn, *, mem_mib: int | None = None, cpu: int | None = None,
) -> dict:
    """Ask the user -> on APPROVE raise the container cgroup caps (grow-only, live).

    The VM<->container coupling: after a Proxmox VM grow, this makes the grown
    RAM/cores reach the container. The host verb caps memory below MemTotal-reserve.
    """
    if mem_mib is None and cpu is None:
        # Fail before asking the user to approve a no-op (empty proposal).
        return {"ok": False, "stage": "invalid",
                "error": "nothing to do (both axes None)"}
    parts = []
    if mem_mib is not None:
        parts.append(f"memory->{mem_mib}MiB")
    if cpu is not None:
        parts.append(f"cpu->{cpu}")
    proposal = (
        f"\U0001f527 Raise container cgroup limits ({', '.join(parts)}) - grow-only, "
        "applied live (no restart).\n"
        "Reply APPROVE to execute or DENY to cancel."
    )
    reply = await ask(proposal)
    if not _approved(reply):
        return {"ok": False, "stage": "denied", "reply": reply}
    return await remote.request_set_container_limits(mem_mib, cpu)
