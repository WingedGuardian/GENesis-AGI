"""Host storage-pool monitoring — HOST-SIDE.

Backstops the thin-pool-exhaustion incident: `df` on the incus storage-pool
mountpath is blind to LVM-thin *allocation*, so a pool can fill to 100%
(forcing the container rootfs read-only) with no warning. This module measures
true pool allocation — LVM-thin data% and metadata% via `lvs`, plus VG free
headroom — and drives tiered guardian alerts with hysteresis.

Design:
- Measurement (:func:`measure_storage_pool`) is defensive: any failure yields
  ``detected=False`` so we never raise a false alarm on a probe error.
- Tier + alert-decision logic is pure and fully unit-tested (:func:`worst_tier`,
  :func:`decide_alert`) — the subprocess glue is kept thin around it.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime

from genesis.guardian._subprocess import run_subprocess as _run_subprocess
from genesis.guardian.config import GuardianConfig, StoragePoolConfig

logger = logging.getLogger(__name__)

TIER_OK = "ok"
TIER_WARN = "warn"
TIER_HIGH = "high"
TIER_CRIT = "crit"
_TIER_RANK = {TIER_OK: 0, TIER_WARN: 1, TIER_HIGH: 2, TIER_CRIT: 3}


@dataclass(frozen=True)
class StoragePoolStatus:
    """A point-in-time measurement of the host storage pool.

    ``detected=False`` means measurement failed (pool not LVM, incus/lvs
    unavailable, parse error) — callers must treat it as "no signal", never as
    "healthy", and must not alert on it.
    """

    detected: bool
    data_pct: float | None = None
    metadata_pct: float | None = None
    vg_free_bytes: int | None = None
    pool_used_pct: float | None = None
    detail: str = ""
    # Byte size of the thing the percentages are OF (the thin-pool data LV, or
    # the non-LVM pool filesystem). Turns a percentage into bytes for the
    # delete-first divergence evidence (snapshots.mark_healthy); None when
    # unreadable, and then there is no such evidence.
    pool_size_bytes: int | None = None
    # The incus storage pool behind the container's root disk.
    pool_name: str | None = None
    # LVM-thin only: the VG and the backing thin-pool LV. Together with
    # pool_name they are the pool's identity; a VG with several thin pools
    # leaves thinpool_lv None, and relief never acts on a pool it cannot name.
    vg_name: str | None = None
    thinpool_lv: str | None = None


@dataclass(frozen=True)
class AlertDecision:
    """Outcome of the hysteresis evaluation for one measurement."""

    should_alert: bool
    tier: str
    reason: str
    is_resolution: bool = False


def _tier_for(pct: float | None, warn: float, high: float, crit: float) -> str:
    if pct is None:
        return TIER_OK
    if pct >= crit:
        return TIER_CRIT
    if pct >= high:
        return TIER_HIGH
    if pct >= warn:
        return TIER_WARN
    return TIER_OK


def worst_tier(status: StoragePoolStatus, cfg: StoragePoolConfig) -> str:
    """Highest tier across data%, metadata% and backend-agnostic pool-used%.

    ``pool_used_pct`` is a FALLBACK signal: it only tiers when the LVM percents
    are absent (non-LVM backend, e.g. btrfs). Without it, a btrfs pool could
    fill to 100% while this function forever reported OK (data/metadata are None
    there), so neither alerting nor the autonomous pool-crit propose path could
    ever fire. On LVM-thin pools data%/metadata% remain the sole authority —
    incus's space.used/total is ALSO populated there and tiering it would change
    long-standing alert behavior on healthy LVM installs.
    """
    if status.data_pct is None and status.metadata_pct is None:
        return _tier_for(
            status.pool_used_pct,
            cfg.pool_used_warn_pct, cfg.pool_used_high_pct, cfg.pool_used_crit_pct,
        )
    data = _tier_for(
        status.data_pct, cfg.data_warn_pct, cfg.data_high_pct, cfg.data_crit_pct,
    )
    meta = _tier_for(
        status.metadata_pct,
        cfg.metadata_warn_pct, cfg.metadata_high_pct, cfg.metadata_crit_pct,
    )
    return data if _TIER_RANK[data] >= _TIER_RANK[meta] else meta


@dataclass(frozen=True)
class ThinPoolReport:
    """The thin-pool row of ``lvs``, bound by FIELD NAME (see parse_lvs_report)."""

    data_pct: float | None = None
    metadata_pct: float | None = None
    size_bytes: int | None = None
    lv_name: str | None = None


LVS_FIELDS = ("data_percent", "metadata_percent", "lv_size", "lv_name")


def parse_lvs_report(stdout: str) -> ThinPoolReport:
    """Parse ``lvs --reportformat json -o data_percent,metadata_percent,lv_size,
    lv_name --units b --nosuffix`` for the thin-pool LVs of one VG.

    Fields are bound by NAME, never by column position: lvs prints a blank
    percent for an inactive LV (measured: ``"data_percent":""``), and a
    whitespace-split positional parse then shifts the byte size into the data%
    column — a healthy pool reading as a CRITICAL one. Blank or unparseable
    values are None; output that is not the expected JSON shape yields an
    all-None report (no signal, never a guess).

    Percentages come from the first row, as before. Identity (size + LV name)
    only when there is EXACTLY one thin pool: with several, the percentages may
    belong to a different pool than the container's, and relief must never act
    on a pool it cannot name.
    """
    try:
        rows = json.loads(stdout)["report"][0]["lv"]
    except (ValueError, KeyError, IndexError, TypeError):
        return ThinPoolReport()
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
        return ThinPoolReport()

    def _num(row: dict, key: str) -> float | None:
        raw = row.get(key)
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            val = float(raw)
        except ValueError:
            return None
        return val if math.isfinite(val) else None

    first = rows[0]
    data, meta = _num(first, "data_percent"), _num(first, "metadata_percent")
    if len(rows) != 1:
        return ThinPoolReport(data_pct=data, metadata_pct=meta)
    size = _num(first, "lv_size")
    name = first.get("lv_name")
    return ThinPoolReport(
        data_pct=data,
        metadata_pct=meta,
        size_bytes=int(size) if size is not None and size > 0 else None,
        lv_name=name.strip() if isinstance(name, str) and name.strip() else None,
    )


def decide_alert(
    current_tier: str,
    last_tier: str,
    last_alert_at: datetime | None,
    now: datetime,
    realert_hours: float,
) -> AlertDecision:
    """Hysteresis policy for pool alerts.

    - Tier **increase** → alert immediately.
    - Return to ``ok`` from a raised tier → one resolution notice.
    - **Sustained** non-ok tier → re-alert once every ``realert_hours``.
    - Tier decrease that is still non-ok → no alert (avoid noise), but the
      caller still records the new (lower) tier so a later rise re-alerts.
    """
    cur = _TIER_RANK[current_tier]
    last = _TIER_RANK.get(last_tier, 0)

    if cur > last:
        return AlertDecision(True, current_tier, f"tier rose {last_tier}→{current_tier}")
    if cur == 0 and last > 0:
        return AlertDecision(
            True, current_tier, f"pool recovered ({last_tier}→ok)", is_resolution=True,
        )
    if cur > 0 and cur == last:
        if last_alert_at is None:
            return AlertDecision(True, current_tier, "sustained (no prior alert time)")
        hours = (now - last_alert_at).total_seconds() / 3600.0
        if hours >= realert_hours:
            return AlertDecision(
                True, current_tier, f"sustained {current_tier} for {hours:.1f}h",
            )
    return AlertDecision(False, current_tier, "no change")


def pool_mount_path(pool_name: str) -> str:
    """Filesystem mountpath of an incus storage pool on the host."""
    return f"/var/lib/incus/storage-pools/{pool_name}"


async def _detect_pool_name(config: GuardianConfig) -> str | None:
    rc, out, _ = await _run_subprocess(
        "incus", "config", "device", "get", config.container_name, "root", "pool",
        timeout=10.0,
    )
    return out.strip() if rc == 0 and out.strip() else None


async def _pool_driver_and_source(pool_name: str) -> tuple[str | None, str | None]:
    """Parse ``incus storage show``: (driver, source), (None, None) on failure."""
    rc, out, _ = await _run_subprocess(
        "incus", "storage", "show", pool_name, timeout=10.0,
    )
    if rc != 0:
        return None, None
    # incus storage show emits YAML; driver + source identify the backend.
    driver = None
    source = None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("driver:"):
            driver = s.split(":", 1)[1].strip()
        elif s.startswith("source:"):
            source = s.split(":", 1)[1].strip()
    return driver, source


async def _detect_pool_driver(pool_name: str) -> str | None:
    """Backend driver of an incus pool ("lvm", "btrfs", "dir", …), None on failure."""
    driver, _ = await _pool_driver_and_source(pool_name)
    return driver


async def _lvm_source(pool_name: str) -> str | None:
    """Return the LVM VG backing an incus pool, or None if not LVM."""
    driver, source = await _pool_driver_and_source(pool_name)
    if driver != "lvm" or not source:
        return None
    # ``source`` is the VG name. NOTE: the backing thin-pool LV name is NOT
    # necessarily the incus pool name (default layout: pool "default" / LV
    # "IncusThinPool") — resolve it via lvm.thinpool_name, not by assumption.
    return source


async def _pool_used_via_df(mount: str) -> tuple[float, int] | None:
    """Filesystem (used%, size bytes) of a pool mount via ``df`` (statvfs).

    The tiering signal for non-LVM pools, where there is no thin-pool
    data%/metadata%. Returns None on any error (missing mount, unparsable
    output, zero size) so a probe failure is never a false alarm.
    """
    rc, out, _ = await _run_subprocess(
        "df", "-B1", "--output=used,size", mount, timeout=10.0,
    )
    if rc != 0:
        return None
    lines = out.strip().splitlines()
    if len(lines) < 2:  # header + at least one data row
        return None
    try:
        used, size = (int(x) for x in lines[-1].split()[:2])
    except (ValueError, IndexError):
        return None
    if size <= 0:
        return None
    return 100.0 * used / size, size


async def _pool_used_pct_via_df(mount: str) -> float | None:
    """Used% only — see :func:`_pool_used_via_df`."""
    got = await _pool_used_via_df(mount)
    return got[0] if got else None


async def measure_storage_pool(config: GuardianConfig) -> StoragePoolStatus:
    """Measure the host storage pool. Defensive: failure → detected=False."""
    pool_name = await _detect_pool_name(config)
    if not pool_name:
        return StoragePoolStatus(detected=False, detail="pool name undetected")

    pool_used_pct: float | None = None

    # Identify the backend ONCE, and treat "could not tell" as undetected. An
    # `incus storage show` failure used to read as "not LVM" and fall through to
    # df, which on an LVM pool measures the HOST filesystem, not thin-pool
    # allocation — a number automatic relief must never act on.
    driver, source = await _pool_driver_and_source(pool_name)
    if driver is None:
        return StoragePoolStatus(detected=False, detail=f"backend of pool {pool_name} undetected")
    if driver == "lvm" and not source:
        return StoragePoolStatus(detected=False, detail=f"lvm pool {pool_name} has no source VG")
    vg = source if driver == "lvm" else None
    if not vg:
        # Non-LVM backend (btrfs/dir): the pool mount's filesystem used% is the
        # only tiering signal (there is no thin-pool data%/metadata%). incus
        # storage info exposes no machine-readable space for an uncapped
        # btrfs-on-LV pool, so read the mount directly — the same source
        # snapshots.py uses for this pool.
        df_used = await _pool_used_via_df(pool_mount_path(pool_name))
        return StoragePoolStatus(
            detected=df_used is not None,
            pool_used_pct=df_used[0] if df_used else None,
            pool_size_bytes=df_used[1] if df_used else None,
            pool_name=pool_name,
            detail=f"non-lvm pool {pool_name}",
        )

    # LVM-thin: data% + metadata% via lvs (needs passwordless sudo on host).
    # Select ONLY thin-pool LVs — a bare `lvs <vg>` lists every LV in the VG
    # (regular LVs report blank percents), so we must filter to segtype
    # thin-pool to read the pool's own data%/metadata%.
    vg_name = vg.split("/")[0]
    # The same row also carries the pool's byte size and LV name (folded into
    # this call rather than a new subprocess: the measurement's subprocess
    # count is budgeted, see host_profile._POOL_TIMEOUT). `--units b` changes
    # only lv_size; the percent columns are unit-free.
    rc, out, err = await _run_subprocess(
        "sudo", "-n", "lvs", "--reportformat", "json", "--nosuffix", "--units", "b",
        "-S", "segtype=thin-pool",
        "-o", ",".join(LVS_FIELDS),
        vg_name,
        timeout=10.0,
    )
    if rc != 0:
        logger.warning("lvs failed for %s: %s", vg_name, err)
        return StoragePoolStatus(
            detected=pool_used_pct is not None,
            pool_used_pct=pool_used_pct,
            detail=f"lvs failed: {err[:120]}",
        )
    report = parse_lvs_report(out)
    data_pct, metadata_pct = report.data_pct, report.metadata_pct

    # VG free bytes (headroom for autoextend — 0 free = autoextend can't fire).
    vg_free_bytes: int | None = None
    rc, out, _ = await _run_subprocess(
        "sudo", "-n", "vgs", "--noheadings", "--nosuffix", "--units", "b",
        "-o", "vg_free", vg_name,
        timeout=10.0,
    )
    if rc == 0 and out.strip():
        try:
            vg_free_bytes = int(float(out.strip().split()[0]))
        except (ValueError, IndexError):
            vg_free_bytes = None

    return StoragePoolStatus(
        detected=True,
        data_pct=data_pct,
        metadata_pct=metadata_pct,
        vg_free_bytes=vg_free_bytes,
        pool_used_pct=pool_used_pct,
        detail=f"lvm {vg} data={data_pct} meta={metadata_pct}",
        pool_size_bytes=report.size_bytes,
        pool_name=pool_name,
        vg_name=vg_name,
        thinpool_lv=report.lv_name,
    )
