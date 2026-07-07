"""Host-side storage-expand: absorb a grown virtual disk into the LVM-thin pool.

Runs AFTER the hypervisor grew the VM's disk (``grow_vm_disk``). Strictly
additive LVM operations, stop at the first failure, JSON step list out:

    rescan the block device → pvresize (this is what creates VG free extents) →
    set the thin-pool autoextend profile (threshold 80 / percent 20) + ensure
    dmeventd monitoring → optionally grow tiny pool metadata → verify vg_free>0.

⚠ HARD RULE — this module MUST NEVER extend the DATA LV to consume all free
space (``lvextend ... +100%FREE`` / ``-l +100%FREE``). That is precisely the
configuration that caused the 2026-07 thin-pool outage: a data LV sized to the
whole VG leaves zero headroom, so autoextend can never fire. The point of a
disk grow is to leave vg_free ABOVE the pool. A regression test greps this
file to guarantee that command shape never appears, and ``_assert_no_full_extend``
guards at runtime.
"""

from __future__ import annotations

import logging

from genesis.guardian._subprocess import run_subprocess
from genesis.guardian.config import GuardianConfig
from genesis.guardian.pool import _detect_pool_name, _lvm_source

logger = logging.getLogger(__name__)

_AUTOEXTEND_PROFILE_NAME = "genesis-thinpool"
_AUTOEXTEND_PROFILE = (
    "activation {\n"
    "\tthin_pool_autoextend_threshold=80\n"
    "\tthin_pool_autoextend_percent=20\n"
    "}\n"
)


def _assert_no_full_extend(argv: tuple[str, ...]) -> None:
    """Runtime backstop: refuse to ever issue a whole-VG data-LV extend."""
    joined = " ".join(argv)
    if "100%FREE" in joined:
        raise RuntimeError(
            "refusing lvextend +100%FREE — that re-creates the outage "
            "(data LV consuming all free extents leaves no autoextend headroom)",
        )


async def _run(run, *argv: str, timeout: float = 60.0, stdin_data=None):
    _assert_no_full_extend(argv)
    return await run(*argv, timeout=timeout, stdin_data=stdin_data)


async def _block_disk_for(pv: str, run) -> str | None:
    """Map a PV device (/dev/sdb or /dev/sdb1) to its parent block disk name."""
    rc, out, _ = await run("lsblk", "-no", "PKNAME", pv, timeout=10.0)
    if rc == 0 and out.strip():
        return out.strip().splitlines()[0].strip()
    # No parent → the PV is the whole disk; use its basename.
    name = pv.rsplit("/", 1)[-1]
    return name or None


async def _vg_free_bytes(vg: str, run) -> int | None:
    rc, out, _ = await run(
        "sudo", "-n", "vgs", "--noheadings", "--nosuffix", "--units", "b",
        "-o", "vg_free", vg, timeout=15.0,
    )
    if rc == 0 and out.strip():
        try:
            return int(float(out.strip().split()[0]))
        except (ValueError, IndexError):
            return None
    return None


async def _seg_monitored(vg: str, thinpool: str, run) -> bool:
    rc, out, _ = await run(
        "sudo", "-n", "lvs", "--noheadings", "-o", "seg_monitor",
        f"{vg}/{thinpool}", timeout=15.0,
    )
    return rc == 0 and "monitored" in out.lower() and "not monitored" not in out.lower()


async def _resolve_thinpool_lv(pool_name: str, vg: str, run) -> str:
    """Resolve the thin-pool LV name backing an incus LVM pool.

    The incus pool name and its backing thin-pool LV are NOT the same in the
    default incus LVM layout: the pool is ``default`` but its LV is
    ``IncusThinPool``. Assuming ``thinpool == pool_name`` made the autoextend
    profile + monitoring target a nonexistent ``vg/<pool_name>`` LV. Resolve it
    honestly, most-authoritative first:

    1. incus's own ``lvm.thinpool_name`` config (exactly which LV incus uses),
    2. the single thin-pool LV present in the VG (``lvs -S segtype=thin-pool``),
    3. last resort: the pool name (correct only where the naming happens to
       coincide) — never worse than the old behaviour.
    """
    rc, out, _ = await run(
        "incus", "storage", "get", pool_name, "lvm.thinpool_name", timeout=10.0,
    )
    name = out.strip() if rc == 0 else ""
    if name:
        return name
    rc, out, _ = await run(
        "sudo", "-n", "lvs", "--noheadings", "-o", "lv_name",
        "-S", "segtype=thin-pool", vg, timeout=15.0,
    )
    if rc == 0:
        names = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if len(names) == 1:  # unambiguous → use it
            return names[0]
    return pool_name


async def expand_storage(config: GuardianConfig, *, run=None) -> dict:
    """Absorb a grown disk into the thin pool. Returns a JSON-able result dict."""
    run = run or run_subprocess
    steps: list[dict] = []

    def _record(name: str, rc: int, out: str, err: str) -> bool:
        ok = rc == 0
        steps.append({"step": name, "ok": ok, "detail": (err or out or "")[:200]})
        return ok

    pool_name = await _detect_pool_name(config)
    if not pool_name:
        return {"ok": False, "steps": steps, "error": "incus storage pool undetected"}
    vg = await _lvm_source(pool_name)
    if not vg:
        return {
            "ok": False, "steps": steps,
            "error": f"pool {pool_name} is not LVM-thin — nothing to expand",
        }
    thinpool = await _resolve_thinpool_lv(pool_name, vg, run)

    # 1. Discover PVs in the VG.
    rc, out, err = await _run(
        run, "sudo", "-n", "pvs", "--noheadings", "-o", "pv_name",
        "-S", f"vg_name={vg}", timeout=20.0,
    )
    if not _record("pvs", rc, out, err):
        return {"ok": False, "steps": steps, "error": "pvs failed"}
    pvs = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not pvs:
        return {"ok": False, "steps": steps, "error": f"no PVs found in VG {vg}"}

    # 2. Rescan (non-fatal) + pvresize (fatal) each PV — pvresize creates the
    #    free extents the whole operation exists to produce.
    for pv in pvs:
        disk = await _block_disk_for(pv, run)
        if disk:
            rc, out, err = await _run(
                run, "sudo", "-n", "tee", f"/sys/block/{disk}/device/rescan",
                stdin_data="1\n", timeout=20.0,
            )
            _record(f"rescan {disk}", rc, out, err)  # advisory only
        rc, out, err = await _run(run, "sudo", "-n", "pvresize", pv, timeout=60.0)
        if not _record(f"pvresize {pv}", rc, out, err):
            return {"ok": False, "steps": steps, "error": f"pvresize {pv} failed"}

    # 3. Autoextend profile + dmeventd monitoring (so future fills auto-grow,
    #    now that vg_free > 0). All idempotent.
    rc, out, err = await _run(
        run, "sudo", "-n", "tee",
        f"/etc/lvm/profile/{_AUTOEXTEND_PROFILE_NAME}.profile",
        stdin_data=_AUTOEXTEND_PROFILE, timeout=15.0,
    )
    _record("write autoextend profile", rc, out, err)
    rc, out, err = await _run(
        run, "sudo", "-n", "lvchange", "--metadataprofile",
        _AUTOEXTEND_PROFILE_NAME, f"{vg}/{thinpool}", timeout=20.0,
    )
    _record("apply metadataprofile", rc, out, err)
    rc, out, err = await _run(
        run, "sudo", "-n", "lvchange", "--monitor", "y", f"{vg}/{thinpool}",
        timeout=20.0,
    )
    _record("enable monitoring", rc, out, err)

    # 4. Verify: vg_free must now be > 0 (the whole point), monitoring on.
    vg_free = await _vg_free_bytes(vg, run)
    monitored = await _seg_monitored(vg, thinpool, run)
    ok = vg_free is not None and vg_free > 0
    return {
        "ok": ok,
        "steps": steps,
        "vg": vg,
        "thinpool": thinpool,
        "vg_free_bytes": vg_free,
        "monitored": monitored,
        "error": "" if ok else "pvresize completed but VG still reports 0 free extents",
    }
