"""Host-side container capacity grows — grow-root (+ set-container-limits).

LOCAL incus operations (NOT Proxmox — those live in ``provisioning/``). These
grow the container's OWN resources after the VM underneath has room:

- **grow-root**: raise the container root device ``size=`` — incus grows the
  thin LV AND resizes the filesystem ONLINE (spike-proven 2026-07-15 on incus
  6.0, LVM+ext4: df 1.7G→2.7G with no restart, container stayed RUNNING).
- **set-container-limits**: raise the cgroup ``limits.memory``/``limits.cpu``
  so a grown VM's RAM/cores actually reach the container (both apply LIVE —
  cgroup ``memory.max`` rewrites without a restart, spike-proven).

Both are **grow-only** and **verified** (re-read after the mutation). Strictly
additive, JSON step-list out, stop at the first failure — mirrors
``provisioning/expand.py``. incus commands run as the guardian user (in the
``incus`` group); no sudo. Approval is the CALLER's job (the container obtains
it before invoking the gateway verb), same two-owner model as the Proxmox
provisioning verbs.
"""

from __future__ import annotations

import logging
import os

from genesis.guardian._subprocess import run_subprocess
from genesis.guardian.config import GuardianConfig
from genesis.guardian.host_profile import _read_meminfo
from genesis.guardian.pool import (
    TIER_HIGH,
    measure_storage_pool,
    worst_tier,
)

logger = logging.getLogger(__name__)

_GB = 1000**3  # incus expresses device sizes in decimal GB (30GB → 27.94 GiB LV)
_GIB = 1024**3
_MIB = 1024**2

# Host RAM reserve kept OUT of the container cap: never let limits.memory climb
# so high the host itself can't breathe. max(4 GiB, 20% of MemTotal) — mirrors
# host-setup.sh's install-time reserve, config-overridable via the formula, never
# a machine constant.
_HOST_RESERVE_MIN_GIB = 4
_HOST_RESERVE_FRACTION = 0.20


def _parse_incus_size(s: str) -> int | None:
    """Parse an incus size string ("30GB", "16GiB", "512MB", a bare byte int) to
    bytes. Returns None if unparseable. incus uses decimal GB/MB and binary
    GiB/MiB (IEC)."""
    s = (s or "").strip()
    if not s:
        return None
    units = [
        ("GiB", _GIB),
        ("MiB", _MIB),
        ("KiB", 1024),
        ("GB", _GB),
        ("MB", 1000**2),
        ("kB", 1000),
        ("B", 1),
    ]
    for suf, mult in units:
        if s.endswith(suf):
            num = s[: -len(suf)].strip()
            try:
                return int(float(num) * mult)
            except ValueError:
                return None
    try:
        return int(s)  # bare byte count
    except ValueError:
        return None


async def grow_root(
    config: GuardianConfig,
    new_gb: int,
    *,
    run=run_subprocess,
) -> dict:
    """Grow the container root device to ``new_gb`` GB total (grow-only).

    ``new_gb`` is the new TOTAL size in incus GB (decimal), matching how the
    device is expressed ("30GB"). Refuses a shrink, refuses when the thin pool is
    already at/above its HIGH tier (don't grow virtual capacity into a nearly-full
    pool), then ``incus config device set <c> root size=<new_gb>GB`` and verifies
    the re-read. Never raises — returns a JSON step-list dict.
    """
    action = "grow-root"
    steps: list[str] = []
    c = config.container_name
    try:
        # 1. Current root size (grow-only anchor).
        rc, out, err = await run(
            "incus",
            "config",
            "device",
            "get",
            c,
            "root",
            "size",
            timeout=15.0,
        )
        if rc != 0:
            return {
                "ok": False,
                "action": action,
                "error": f"cannot read current root size: {err[:200]}",
            }
        cur_bytes = _parse_incus_size(out)
        new_bytes = new_gb * _GB
        steps.append(f"current root size {out.strip() or '?'} ({cur_bytes} B)")
        if cur_bytes is not None and new_bytes <= cur_bytes:
            return {
                "ok": False,
                "action": action,
                "steps": steps,
                "error": f"grow-only: requested {new_gb}GB "
                f"({new_bytes} B) not greater than current {cur_bytes} B",
            }

        # 2. Pool-headroom guard — never grow virtual capacity into a stressed pool.
        status = await measure_storage_pool(config)
        if status.detected:
            tier = worst_tier(status, config.storage_pool)
            steps.append(f"pool tier={tier} (data={status.data_pct} meta={status.metadata_pct})")
            if tier in (TIER_HIGH, "crit"):
                return {
                    "ok": False,
                    "action": action,
                    "steps": steps,
                    "error": f"pool at {tier} tier — refusing to grow root into a "
                    "nearly-full thin pool (free pool space first)",
                }
        else:
            steps.append("pool headroom unmeasurable — proceeding (grow is virtual)")

        # 3. Grow (incus resizes the thin LV + filesystem online).
        rc, out, err = await run(
            "incus",
            "config",
            "device",
            "set",
            c,
            "root",
            f"size={new_gb}GB",
            timeout=300.0,
        )
        if rc != 0:
            return {
                "ok": False,
                "action": action,
                "steps": steps,
                "error": f"incus device set failed: {err[:300]}",
            }
        steps.append(f"set root size={new_gb}GB")

        # 4. Verify the re-read reflects the new size.
        rc, out, err = await run(
            "incus",
            "config",
            "device",
            "get",
            c,
            "root",
            "size",
            timeout=15.0,
        )
        verified = rc == 0 and _parse_incus_size(out) == new_bytes
        steps.append(f"verify root size now {out.strip()!r} (verified={verified})")
        return {
            "ok": verified,
            "action": action,
            "steps": steps,
            "new_size_gb": new_gb,
            "verified": verified,
        }
    except Exception as exc:  # noqa: BLE001 — the verb contract is JSON-always
        logger.warning("grow-root failed", exc_info=True)
        return {"ok": False, "action": action, "steps": steps, "error": repr(exc)}


def _host_reserve_bytes(mem_total_bytes: int) -> int:
    """RAM to keep out of the container cap: max(4 GiB, 20% of host MemTotal)."""
    return max(_HOST_RESERVE_MIN_GIB * _GIB, int(mem_total_bytes * _HOST_RESERVE_FRACTION))


async def set_container_limits(
    config: GuardianConfig,
    new_mem_mib: int | None,
    new_cpu: int | None,
    *,
    run=run_subprocess,
) -> dict:
    """Raise the container's cgroup limits (grow-only), the VM↔container coupling.

    After a Proxmox VM memory/cpu grow, nothing else raises the container's caps;
    this does. ``limits.memory`` is capped hard below ``MemTotal − reserve`` so the
    host always keeps headroom; ``limits.cpu`` is capped at the host core count.
    Both apply LIVE (spike-proven). Grow-only: never lowers an existing cap.
    Pass None to leave an axis unchanged. Never raises — JSON step-list dict.
    """
    action = "set-container-limits"
    steps: list[str] = []
    c = config.container_name
    try:
        mem = _read_meminfo()
        mem_total_b = (mem.get("MemTotal") or 0) * 1024  # kB → B
        if not mem_total_b:
            return {
                "ok": False,
                "action": action,
                "error": "cannot read host MemTotal from /proc/meminfo",
            }

        # ── VALIDATE BOTH AXES UP FRONT (no mutation until every check passes,
        # so a valid-memory + invalid-cpu request never leaves a partial set) ──
        host_cores = os.cpu_count() or 1
        new_mem_b: int | None = None
        if new_mem_mib is not None:
            new_mem_b = new_mem_mib * _MIB
            reserve_b = _host_reserve_bytes(mem_total_b)
            cap_b = mem_total_b - reserve_b
            rc, out, _ = await run("incus", "config", "get", c, "limits.memory", timeout=15.0)
            cur_b = _parse_incus_size(out) if rc == 0 else None
            steps.append(
                f"mem current={out.strip()!r} ({cur_b} B), "
                f"host MemTotal={mem_total_b} B, cap={cap_b} B"
            )
            if cur_b is not None and new_mem_b <= cur_b:
                return {
                    "ok": False,
                    "action": action,
                    "steps": steps,
                    "error": f"grow-only: memory {new_mem_mib}MiB not greater than "
                    f"current {cur_b} B",
                }
            if new_mem_b >= cap_b:
                return {
                    "ok": False,
                    "action": action,
                    "steps": steps,
                    "error": f"memory {new_mem_mib}MiB ({new_mem_b} B) exceeds host "
                    f"cap MemTotal−reserve ({cap_b} B) — would starve the host",
                }

        if new_cpu is not None:
            rc, out, _ = await run("incus", "config", "get", c, "limits.cpu", timeout=15.0)
            cur_cpu_raw = out.strip() if rc == 0 else ""
            # An unset/empty limits.cpu means ALL host cores (unlimited); a numeric
            # cap below that would REDUCE the container — so treat unset as
            # host_cores for the grow-only comparison (Codex P2: cpu was not
            # grow-only, a cpu=4 request could lower an existing cpu=8 cap).
            cur_cpu = int(cur_cpu_raw) if cur_cpu_raw.isdigit() else host_cores
            steps.append(f"cpu current={cur_cpu_raw!r} (eff {cur_cpu}), host cores={host_cores}")
            if new_cpu < 1 or new_cpu > host_cores:
                return {
                    "ok": False,
                    "action": action,
                    "steps": steps,
                    "error": f"cpu {new_cpu} out of range 1..{host_cores} (host cores)",
                }
            if new_cpu <= cur_cpu:
                return {
                    "ok": False,
                    "action": action,
                    "steps": steps,
                    "error": f"grow-only: cpu {new_cpu} not greater than current {cur_cpu}",
                }

        # ── APPLY (all validations passed; both are live cgroup writes) ──
        if new_mem_mib is not None:
            rc, out, err = await run(
                "incus",
                "config",
                "set",
                c,
                f"limits.memory={new_mem_mib}MiB",
                timeout=30.0,
            )
            if rc != 0:
                return {
                    "ok": False,
                    "action": action,
                    "steps": steps,
                    "error": f"set limits.memory failed: {err[:200]}",
                }
            steps.append(f"set limits.memory={new_mem_mib}MiB")

        if new_cpu is not None:
            rc, out, err = await run(
                "incus",
                "config",
                "set",
                c,
                f"limits.cpu={new_cpu}",
                timeout=30.0,
            )
            if rc != 0:
                return {
                    "ok": False,
                    "action": action,
                    "steps": steps,
                    "error": f"set limits.cpu failed: {err[:200]}",
                }
            steps.append(f"set limits.cpu={new_cpu}")

        # Verify by re-read.
        rc, out, _ = await run("incus", "config", "get", c, "limits.memory", timeout=15.0)
        mem_now = out.strip() if rc == 0 else "?"
        rc, out, _ = await run("incus", "config", "get", c, "limits.cpu", timeout=15.0)
        cpu_now = out.strip() if rc == 0 else "?"
        steps.append(f"verify limits.memory={mem_now!r} limits.cpu={cpu_now!r}")
        return {
            "ok": True,
            "action": action,
            "steps": steps,
            "limits_memory": mem_now,
            "limits_cpu": cpu_now,
        }
    except Exception as exc:  # noqa: BLE001 — the verb contract is JSON-always
        logger.warning("set-container-limits failed", exc_info=True)
        return {"ok": False, "action": action, "steps": steps, "error": repr(exc)}
