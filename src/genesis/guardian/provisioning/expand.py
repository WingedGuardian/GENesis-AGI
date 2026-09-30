"""Host-side storage-expand: absorb a grown virtual disk into the storage pool.

Runs AFTER the hypervisor grew the VM's disk (``grow_vm_disk``). Strictly
additive operations, stop at the first failure, JSON step list out. Two
supported substrates, forked on the incus pool driver:

**LVM-thin** (pool driver ``lvm``):
    rescan the block device → pvresize (this is what creates VG free extents) →
    set the thin-pool autoextend profile (threshold 80 / percent 20) + ensure
    dmeventd monitoring → verify vg_free>0. The data LV is NEVER extended here —
    the whole point is leaving vg_free ABOVE the pool as autoextend headroom.

**btrfs-on-LVM** (pool driver ``btrfs``, backing device a regular LVM LV —
the three-layer topology: PVE disk → guest VG → linear LV → btrfs):
    rescan → pvresize → ``lvextend`` the backing LV by an EXPLICIT byte count →
    ``btrfs filesystem resize max <mount>`` (online; btrfs reads the new device
    size live) → verify the filesystem actually grew. A linear LV in a VG with
    no thin pool has no autoextend-headroom requirement, so consuming the freed
    extents is correct here — but always via an explicit computed size, never a
    whole-VG token.

⚠ HARD RULE — this module MUST NEVER issue ``+100%FREE`` (``-l +100%FREE`` /
``-L +100%FREE``). On the thin-pool substrate that exact shape caused the
2026-07 outage: a data LV sized to the whole VG leaves zero headroom, so
autoextend can never fire. ``_assert_no_full_extend`` guards every command at
runtime (both substrates — the btrfs path computes explicit byte sizes and so
never needs the token), and ``test_never_issues_100pct_free`` asserts the thin
path never extends any data LV at all.
"""

from __future__ import annotations

import logging

from genesis.guardian._subprocess import run_subprocess
from genesis.guardian.config import GuardianConfig
from genesis.guardian.pool import (
    _detect_pool_driver,
    _detect_pool_name,
    _lvm_source,
    pool_mount_path,
)

logger = logging.getLogger(__name__)

_AUTOEXTEND_PROFILE_NAME = "genesis-thinpool"
_AUTOEXTEND_PROFILE = (
    "activation {\n"
    "\tthin_pool_autoextend_threshold=80\n"
    "\tthin_pool_autoextend_percent=20\n"
    "}\n"
)

_GIB = 1024 ** 3


def _assert_no_full_extend(argv: tuple[str, ...]) -> None:
    """Runtime backstop: refuse to ever issue a whole-VG extend token."""
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


async def _rescan_and_pvresize(vg: str, run, _record) -> tuple[bool, str]:
    """Shared absorb front half: discover the VG's PVs, rescan each backing
    disk (advisory), pvresize each PV (fatal). Returns (ok, error)."""
    rc, out, err = await _run(
        run, "sudo", "-n", "pvs", "--noheadings", "-o", "pv_name",
        "-S", f"vg_name={vg}", timeout=20.0,
    )
    if not _record("pvs", rc, out, err):
        return False, "pvs failed"
    pvs = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not pvs:
        return False, f"no PVs found in VG {vg}"

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
            return False, f"pvresize {pv} failed"
    return True, ""


async def _btrfs_backing(mount: str, run) -> tuple[str | None, str | None]:
    """Resolve the (vg, lv) backing a mounted btrfs from its mountpath.

    ``findmnt`` yields the backing device, but it can be a ``/dev/disk/by-uuid``
    symlink or a bare ``/dev/dm-N`` (with a possible ``[/subvol]`` suffix) —
    ``lvs`` accepts neither (a by-uuid path is "Invalid path for Logical Volume";
    a ``/dev/dm-N`` is misread as a VG name). Canonicalize to the kernel device
    (``readlink -f``), resolve its device-mapper name (``lsblk``), then
    ``lvs /dev/mapper/<name>`` — which only succeeds for a real LVM LV, so a
    non-LVM btrfs (e.g. on a bare partition) correctly returns (None, None).
    """
    rc, out, _ = await run("findmnt", "-no", "SOURCE", mount, timeout=10.0)
    if rc != 0 or not out.strip():
        return None, None
    dev = out.strip().splitlines()[0].split("[", 1)[0].strip()
    # by-uuid symlink / dm path → canonical kernel device (/dev/dm-N).
    rc, out, _ = await run("readlink", "-f", dev, timeout=10.0)
    real = out.strip().splitlines()[0].strip() if rc == 0 and out.strip() else dev
    # Kernel device → device-mapper name (e.g. ubuntu--vg-genesis--btrfs--lv).
    rc, out, _ = await run("lsblk", "-ndo", "NAME", real, timeout=10.0)
    if rc != 0 or not out.strip():
        return None, None
    dmname = out.strip().splitlines()[0].strip()
    rc, out, _ = await run(
        "sudo", "-n", "lvs", "--noheadings", "-o", "vg_name,lv_name",
        f"/dev/mapper/{dmname}", timeout=15.0,
    )
    if rc != 0 or not out.strip():
        return None, None
    parts = out.strip().split()
    if len(parts) < 2:
        return None, None
    return parts[0], parts[1]


async def _fs_size_bytes(mount: str, run) -> int | None:
    rc, out, _ = await run(
        "df", "--output=size", "--block-size=1", mount, timeout=10.0,
    )
    if rc != 0:
        return None
    lines = out.strip().splitlines()
    if len(lines) < 2:
        return None
    try:
        return int(lines[-1].strip())
    except ValueError:
        return None


async def _expand_btrfs_on_lvm(
    pool_name: str, run, steps: list[dict], _record, add_gib: int | None,
) -> dict:
    """btrfs-on-LVM absorb: pvresize → lvextend (explicit size) → btrfs resize.

    Only ever touches the ONE LV resolved live from the pool's own mountpath —
    never any sibling LV in the VG.
    """
    mount = pool_mount_path(pool_name)
    result: dict = {
        "ok": False, "steps": steps, "driver": "btrfs",
        "mount": mount, "vg": None, "lv": None, "fs_size_bytes": None,
        "error": "",
    }

    vg, lv = await _btrfs_backing(mount, run)
    if not vg or not lv:
        result["error"] = f"could not resolve the LVM LV backing btrfs pool {pool_name}"
        return result
    result["vg"], result["lv"] = vg, lv

    size_before = await _fs_size_bytes(mount, run)

    ok, err = await _rescan_and_pvresize(vg, run, _record)
    if not ok:
        result["error"] = err
        return result

    # Extend by an EXPLICIT byte count. Clamp to vg_free: a +N GiB hypervisor
    # grow yields slightly under N GiB of usable extents (PV metadata + extent
    # rounding), so a literal `-L +NG` can fail with "insufficient free space".
    # vg_free is always extent-aligned, so `-L +<vg_free>b` is exact. This VG
    # carries no thin pool on the btrfs substrate, so there is no autoextend-
    # headroom requirement — but we still never issue the whole-VG token.
    vg_free = await _vg_free_bytes(vg, run)
    if not vg_free or vg_free < _GIB:
        result["error"] = (
            f"VG {vg} has no free space to absorb "
            f"({vg_free or 0} B) — grow the VM disk first"
        )
        return result
    extend_bytes = min(add_gib * _GIB, vg_free) if add_gib else vg_free
    rc, out, err = await _run(
        run, "sudo", "-n", "lvextend", "-L", f"+{extend_bytes}b", f"{vg}/{lv}",
        timeout=60.0,
    )
    if not _record(f"lvextend {vg}/{lv}", rc, out, err):
        result["error"] = f"lvextend {vg}/{lv} failed"
        return result

    # Online filesystem grow — btrfs reads the new device size live; the
    # mountpoint (not the device) is the correct argument.
    rc, out, err = await _run(
        run, "sudo", "-n", "btrfs", "filesystem", "resize", "max", mount,
        timeout=120.0,
    )
    if not _record("btrfs resize max", rc, out, err):
        result["error"] = "btrfs filesystem resize failed"
        return result

    # Verify by re-read: the filesystem must actually be bigger now.
    size_after = await _fs_size_bytes(mount, run)
    result["fs_size_bytes"] = size_after
    grew = size_before is not None and size_after is not None and size_after > size_before
    if grew:
        result["ok"] = True
    else:
        result["error"] = (
            "resize commands succeeded but the filesystem did not grow "
            f"(before={size_before}, after={size_after})"
        )
    return result


async def expand_storage(
    config: GuardianConfig, *, run=None, add_gib: int | None = None,
) -> dict:
    """Absorb a grown disk into the storage pool. Returns a JSON-able result dict.

    ``add_gib`` (btrfs substrate only) bounds the LV extend to the approved grow
    amount; ``None`` (the standalone ``storage-expand`` retry verb) absorbs the
    VG's free space. The LVM-thin substrate never extends a data LV, so the
    amount is irrelevant there.
    """
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
        driver = await _detect_pool_driver(pool_name)
        if driver == "btrfs":
            return await _expand_btrfs_on_lvm(pool_name, run, steps, _record, add_gib)
        return {
            "ok": False, "steps": steps,
            "error": (
                f"pool {pool_name} (driver {driver or 'unknown'}) is neither "
                "LVM-thin nor btrfs-on-LVM — nothing to expand"
            ),
        }
    thinpool = await _resolve_thinpool_lv(pool_name, vg, run)

    # 1-2. Rescan + pvresize each PV — pvresize creates the free extents the
    #      whole operation exists to produce.
    ok, err_msg = await _rescan_and_pvresize(vg, run, _record)
    if not ok:
        return {"ok": False, "steps": steps, "error": err_msg}

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
        "driver": "lvm-thin",
        "vg": vg,
        "thinpool": thinpool,
        "vg_free_bytes": vg_free,
        "monitored": monitored,
        "error": "" if ok else "pvresize completed but VG still reports 0 free extents",
    }
