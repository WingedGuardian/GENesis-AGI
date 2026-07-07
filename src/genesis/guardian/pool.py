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
    """Highest tier across data% and metadata% (metadata alerts earlier)."""
    data = _tier_for(
        status.data_pct, cfg.data_warn_pct, cfg.data_high_pct, cfg.data_crit_pct,
    )
    meta = _tier_for(
        status.metadata_pct,
        cfg.metadata_warn_pct, cfg.metadata_high_pct, cfg.metadata_crit_pct,
    )
    return data if _TIER_RANK[data] >= _TIER_RANK[meta] else meta


def parse_lvs_data_metadata(stdout: str) -> tuple[float | None, float | None]:
    """Parse ``lvs --noheadings -o data_percent,metadata_percent``.

    Output is one line like ``  75.00  12.34`` (percentages). Returns
    ``(data_pct, metadata_pct)``; either element is None if absent/unparseable.
    """
    lines = [ln for ln in stdout.strip().splitlines() if ln.strip()]
    if not lines:
        return None, None
    parts = lines[0].split()

    def _f(idx: int) -> float | None:
        if idx >= len(parts):
            return None
        try:
            return float(parts[idx])
        except ValueError:
            return None

    return _f(0), _f(1)


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


async def _detect_pool_name(config: GuardianConfig) -> str | None:
    rc, out, _ = await _run_subprocess(
        "incus", "config", "device", "get", config.container_name, "root", "pool",
        timeout=10.0,
    )
    return out.strip() if rc == 0 and out.strip() else None


async def _lvm_source(pool_name: str) -> str | None:
    """Return the LVM ``vg/thinpool`` backing an incus pool, or None if not LVM."""
    rc, out, _ = await _run_subprocess(
        "incus", "storage", "show", pool_name, timeout=10.0,
    )
    if rc != 0:
        return None
    # incus storage show emits YAML; the driver + source lines identify LVM.
    driver = None
    source = None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("driver:"):
            driver = s.split(":", 1)[1].strip()
        elif s.startswith("source:"):
            source = s.split(":", 1)[1].strip()
    if driver != "lvm" or not source:
        return None
    # ``source`` is the VG name. NOTE: the backing thin-pool LV name is NOT
    # necessarily the incus pool name (default layout: pool "default" / LV
    # "IncusThinPool") — resolve it via lvm.thinpool_name, not by assumption.
    return source


async def measure_storage_pool(config: GuardianConfig) -> StoragePoolStatus:
    """Measure the host storage pool. Defensive: failure → detected=False."""
    pool_name = await _detect_pool_name(config)
    if not pool_name:
        return StoragePoolStatus(detected=False, detail="pool name undetected")

    # Backend-agnostic used% via incus storage info (best-effort).
    pool_used_pct: float | None = None
    rc, out, _ = await _run_subprocess(
        "incus", "storage", "info", pool_name, "--format", "json", timeout=10.0,
    )
    if rc == 0:
        try:
            info = json.loads(out)
            space = info.get("space") or info.get("resources", {}).get("space", {})
            used, total = space.get("used"), space.get("total")
            if isinstance(used, (int, float)) and total:
                pool_used_pct = 100.0 * used / total
        except (json.JSONDecodeError, TypeError, AttributeError, ZeroDivisionError):
            pass

    vg = await _lvm_source(pool_name)
    if not vg:
        # Non-LVM backend — pool_used_pct (if any) is the only signal.
        return StoragePoolStatus(
            detected=pool_used_pct is not None,
            pool_used_pct=pool_used_pct,
            detail=f"non-lvm pool {pool_name}",
        )

    # LVM-thin: data% + metadata% via lvs (needs passwordless sudo on host).
    # Select ONLY thin-pool LVs — a bare `lvs <vg>` lists every LV in the VG
    # (regular LVs report blank percents), so we must filter to segtype
    # thin-pool to read the pool's own data%/metadata%.
    vg_name = vg.split("/")[0]
    rc, out, err = await _run_subprocess(
        "sudo", "-n", "lvs", "--noheadings", "--nosuffix",
        "-S", "segtype=thin-pool",
        "-o", "data_percent,metadata_percent", vg_name,
        timeout=10.0,
    )
    if rc != 0:
        logger.warning("lvs failed for %s: %s", vg_name, err)
        return StoragePoolStatus(
            detected=pool_used_pct is not None,
            pool_used_pct=pool_used_pct,
            detail=f"lvs failed: {err[:120]}",
        )
    data_pct, metadata_pct = parse_lvs_data_metadata(out)

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
    )
