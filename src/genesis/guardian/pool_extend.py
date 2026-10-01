"""LVM partial extend: grow a thin pool into VG space autoextend cannot use.

The one mutation relief may make besides deleting its own snapshots. Measured
on a live host: dmeventd's autoextend (the ``genesis-thinpool`` profile,
threshold 80%, percent 20%) refuses to extend at all when the VG cannot supply
a whole 20% step, so 3.9 GB of VG free sat unused beside a pool that filled to
100%. This grows the pool by what the VG CAN supply, in exactly that case.

The trigger is autoextend's own, not relief's growth estimate: every guard is
a reason autoextend, which the install opted into, should have acted and
structurally could not. A thin pool cannot shrink, so this permanent step
never hangs on a rate read from noisy samples (review).

* LVM-thin with a named pool, carrying the ``genesis-thinpool`` profile (the
  install's own opt-in; without it the VG space may be meant for something
  else);
* data% at or above the profile's threshold;
* VG free SMALLER than one autoextend step (at or above it, dmeventd can act,
  and this stays out of its way);
* after keeping ``extend_keep_free_mib`` (or twice the metadata LV, if larger)
  unallocated, so the metadata LV can still grow, at least 1 GiB is left.

After an extend, VG free is down to that keep, so the plan returns None until
the operator adds space: no separate cooldown is needed. The caller re-measures
immediately before the mutation and returns the FRESH plan
(``before_mutation``); the extend never exceeds it. Assumes the profile holds
the values below and dmeventd monitoring is on (host provisioning sets both).
"""

from __future__ import annotations

from genesis.guardian.pool import StoragePoolStatus
from genesis.guardian.provisioning.expand import (
    AUTOEXTEND_PERCENT,
    AUTOEXTEND_PROFILE_NAME,
    AUTOEXTEND_THRESHOLD_PCT,
    _assert_no_full_extend,
)

_GIB = 1024**3
GUARD_STOP = "stopped before the mutation (pool changed, no longer wanted, or state unwritable)"
# The client gave up; `sudo` was killed but `lvextend` may still have finished.
TIMED_OUT = "lvextend did not answer in time; whether the pool grew is unknown"


def plan_extend(status: StoragePoolStatus, keep_free_mib: int) -> int | None:
    """Bytes to grow the thin pool by, or None when extending is not ours to do."""
    if not status.vg_name or not status.thinpool_lv:
        return None
    if status.thinpool_profile != AUTOEXTEND_PROFILE_NAME:
        return None
    if status.data_pct is None or status.data_pct < AUTOEXTEND_THRESHOLD_PCT:
        return None
    # Relief skips the extend while metadata is short, which it can only judge
    # from a known metadata %: unknown is not "not short" (review).
    if status.metadata_pct is None:
        return None
    if not status.vg_free_bytes or not status.pool_size_bytes:
        return None
    # The keep-free space is sized from the metadata LV; an unknown size would
    # read as 0 and spend the headroom the metadata LV needs to grow (review).
    if not status.metadata_size_bytes:
        return None
    step = status.pool_size_bytes * AUTOEXTEND_PERCENT // 100
    if status.vg_free_bytes >= step:
        return None
    keep = max(keep_free_mib * 1024**2, 2 * status.metadata_size_bytes)
    grow = status.vg_free_bytes - keep
    return grow if grow >= _GIB else None


def autoextend_reason(status: StoragePoolStatus) -> str:
    free = (status.vg_free_bytes or 0) / _GIB
    return (
        f"data is at {status.data_pct:.1f}%, at or past LVM's {AUTOEXTEND_THRESHOLD_PCT}% "
        f"autoextend threshold, and the VG's {free:.1f}G free is less than one "
        f"{AUTOEXTEND_PERCENT}% autoextend step, so LVM will not extend"
    )


async def extend_thinpool(
    status: StoragePoolStatus,
    grow_bytes: int,
    run,
    before_mutation,
) -> tuple[bool, bool, str]:
    """``lvextend -L +<bytes>b vg/thinpool``, rounded DOWN to whole extents.

    ``before_mutation()`` re-measures and returns the fresh plan in bytes, or
    None to stop; the extend is the smaller of the two plans. Returns
    ``(ok, attempted, detail)``: ``attempted`` is False when it stopped before
    issuing the mutation (unreadable extent size, nothing to grow, or the
    guard said no). A timeout is attempted and not ok, with ``TIMED_OUT``.
    """
    vg, lv = status.vg_name, status.thinpool_lv
    rc, out, err = await run(
        "sudo",
        "-n",
        "vgs",
        "--noheadings",
        "--nosuffix",
        "--units",
        "b",
        "-o",
        "vg_extent_size",
        vg,
        timeout=15.0,
    )
    try:
        extent = int(float(out.strip().split()[0])) if rc == 0 and out.strip() else 0
    except (ValueError, IndexError, OverflowError):
        extent = 0
    if extent <= 0:
        return False, False, f"could not read the extent size of VG {vg}: {(err or out)[:160]}"
    fresh = await before_mutation()
    if fresh is None:
        return False, False, GUARD_STOP
    grow = min(grow_bytes, fresh) // extent * extent
    if grow <= 0:
        return False, False, "the grow rounds down to zero extents"
    argv = ("sudo", "-n", "lvextend", "-L", f"+{grow}b", f"{vg}/{lv}")
    _assert_no_full_extend(argv)
    # lvextend on a thin pool is a metadata operation that takes seconds; the
    # 60s bound keeps a wedged LVM from holding the tick, and a timeout is
    # reported as unknown, never as a failure.
    rc, out, err = await run(*argv, timeout=60.0)
    if rc != 0 and err == "timeout":
        return False, True, TIMED_OUT
    if rc != 0:
        return False, True, f"lvextend failed: {(err or out)[:200]}"
    return True, True, f"grew {vg}/{lv} by {grow / _GIB:.1f}G"
