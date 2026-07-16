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

import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

# ask(proposal_text) -> the user's reply text, or None on timeout.
AskFn = Callable[[str], Awaitable[str | None]]

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


async def coordinate_grow_disk(
    remote, ask: AskFn, *, disk: str, add_gib: int,
) -> dict:
    """Capacity → ask the user → on APPROVE run the host disk-grow execute verb."""
    status = await remote.provision_status()
    if not status.get("ok"):
        return {"ok": False, "stage": "no_capacity", "detail": status}
    reply = await ask(_disk_proposal(status.get("capacity", {}), disk, add_gib))
    if not _approved(reply):
        return {"ok": False, "stage": "denied", "reply": reply}
    return await remote.request_grow_disk(disk, add_gib)


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
