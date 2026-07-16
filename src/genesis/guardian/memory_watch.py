"""Guardian-side RAM tiered alerting — HOST-SIDE.

Backstops a gap the disk-pool monitor already closed for storage: the ONLY
whole-RAM Telegram alert today is container-side (`infra:container_memory_high`
in the health snapshot), and that path dies exactly when the container
thrashes/OOMs — the moment it matters most. The guardian runs OUTSIDE the
container blast radius, so it can page out-of-band.

Two axes, worst-of (mirrors the disk pool's data%/metadata%):

- **Container RAM** — anon+kernel vs the container's cgroup ``memory.max``, read
  via ``incus exec``. Best-effort: that exec can itself stall under severe
  memory pressure, so this axis may go blind precisely when it matters.
- **Host-VM RAM** — ``100 − MemAvailable/MemTotal`` from the guardian's OWN
  ``/proc/meminfo``. Needs NO incus-exec → the RELIABLE signal in a thrash. The
  worst-of means the host axis still fires the page when the container read
  stalls.

Design mirrors :mod:`genesis.guardian.pool`: the tier + hysteresis logic is the
same pure, unit-tested code (``_tier_for``/``decide_alert``); the measurement
glue is kept thin around it. Any measurement failure yields ``None`` (no
signal), never a false ``0%``/healthy.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from genesis.guardian.alert.base import Alert, AlertSeverity
from genesis.guardian.alert.dispatcher import AlertDispatcher
from genesis.guardian.config import GuardianConfig, MemoryTiersConfig
from genesis.guardian.health_signals import measure_container_mem_pct
from genesis.guardian.host_profile import _read_meminfo
from genesis.guardian.pool import (
    _TIER_RANK,
    TIER_CRIT,
    TIER_HIGH,
    TIER_OK,
    TIER_WARN,
    _tier_for,
    decide_alert,
)

logger = logging.getLogger(__name__)

# WARN/HIGH are degradation (WARNING); CRIT is genuine pressure (CRITICAL).
# Mirrors _POOL_TIER_SEVERITY so RAM and disk alerts read consistently.
_MEM_TIER_SEVERITY = {
    TIER_WARN: AlertSeverity.WARNING,
    TIER_HIGH: AlertSeverity.WARNING,
    TIER_CRIT: AlertSeverity.CRITICAL,
}


def measure_host_mem_pct() -> tuple[float | None, str]:
    """Host-VM RAM used% (``100 − MemAvailable/MemTotal``) from /proc/meminfo.

    The RELIABLE axis — read on the guardian's own host, no incus-exec — so it
    still fires when the container is thrashing so hard that exec stalls.
    Returns ``(pct, detail)``; ``pct`` is None if meminfo is unreadable (treat
    as no signal, never 0/healthy). Reuses host_profile's canonical reader.
    """
    mem = _read_meminfo()
    total = mem.get("MemTotal")
    avail = mem.get("MemAvailable")
    if not total or avail is None:
        return None, "host meminfo unreadable"
    used_pct = 100.0 * (1.0 - avail / total)
    used_mb = (total - avail) // 1024
    total_mb = total // 1024
    return used_pct, f"{used_pct:.1f}% used ({used_mb}M / {total_mb}M)"


def memory_worst_tier(
    container_pct: float | None,
    host_pct: float | None,
    cfg: MemoryTiersConfig,
) -> str:
    """Highest tier across the container and host-VM RAM axes.

    ``_tier_for`` maps a None pct to TIER_OK, so a blind axis never contributes;
    the caller separately skips alerting when BOTH axes are None (no signal).
    """
    c = _tier_for(
        container_pct,
        cfg.container_warn_pct,
        cfg.container_high_pct,
        cfg.container_crit_pct,
    )
    h = _tier_for(
        host_pct,
        cfg.host_warn_pct,
        cfg.host_high_pct,
        cfg.host_crit_pct,
    )
    return c if _TIER_RANK[c] >= _TIER_RANK[h] else h


async def _measure_axes(config: GuardianConfig) -> tuple[float | None, str, float | None, str]:
    """Measure both RAM axes defensively. Never raises."""
    container_pct: float | None = None
    c_detail = "not measured"
    host_pct: float | None = None
    h_detail = "not measured"
    try:
        container_pct, c_detail = await measure_container_mem_pct(config)
    except Exception:
        logger.warning("container memory measure failed", exc_info=True)
        c_detail = "container measure error"
    try:
        host_pct, h_detail = measure_host_mem_pct()
    except Exception:
        logger.warning("host memory measure failed", exc_info=True)
        h_detail = "host measure error"
    return container_pct, c_detail, host_pct, h_detail


async def memory_status_snapshot(config: GuardianConfig) -> dict:
    """Read-only RAM view for the `ram-status` gateway verb (both axes + tier)."""
    cfg = config.memory_tiers
    container_pct, c_detail, host_pct, h_detail = await _measure_axes(config)
    tier = memory_worst_tier(container_pct, host_pct, cfg)
    return {
        "ok": True,
        "action": "ram-status",
        "enabled": cfg.enabled,
        "tier": tier,
        "container": {"used_pct": container_pct, "detail": c_detail},
        "host": {"used_pct": host_pct, "detail": h_detail},
    }


async def check_memory_and_alert(
    config: GuardianConfig,
    dispatcher: AlertDispatcher,
) -> None:
    """Measure container + host-VM RAM and emit tiered alerts with hysteresis.

    State (last-alerted tier + timestamp) persists in the guardian state dir so
    hysteresis survives across the stateless per-tick invocations. Alerts go
    through the guardian's own dispatcher (host Telegram channel), which
    survives a thrashing/dead container — exactly when this matters most.
    Near-verbatim clone of check._check_storage_pool_and_alert.
    """
    cfg = config.memory_tiers
    if not cfg.enabled:
        return

    container_pct, c_detail, host_pct, h_detail = await _measure_axes(config)
    if container_pct is None and host_pct is None:
        # No signal on EITHER axis — do not alert and do not record OK (which
        # would falsely clear a prior alert). Mirrors pool's detected=False.
        logger.debug("RAM not measurable (both axes): c=%s h=%s", c_detail, h_detail)
        return

    tier = memory_worst_tier(container_pct, host_pct, cfg)

    state_file = config.state_path / "memory_alert_state.json"
    last_tier = TIER_OK
    last_alert_at: datetime | None = None
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text())
            last_tier = data.get("tier", TIER_OK)
            raw_at = data.get("last_alert_at")
            last_alert_at = datetime.fromisoformat(raw_at) if raw_at else None
        except (ValueError, OSError):
            pass

    now = datetime.now(UTC)
    decision = decide_alert(tier, last_tier, last_alert_at, now, cfg.realert_hours)

    if decision.should_alert:
        if decision.is_resolution:
            severity = AlertSeverity.INFO
            title = "RAM pressure recovered"
        else:
            severity = _MEM_TIER_SEVERITY.get(tier, AlertSeverity.WARNING)
            title = f"RAM {tier.upper()}"
        parts = []
        if container_pct is not None:
            parts.append(f"container {container_pct:.0f}%")
        if host_pct is not None:
            parts.append(f"host {host_pct:.0f}%")
        body = f"{decision.reason}. " + ", ".join(parts)
        if tier == TIER_CRIT:
            body += (
                "\nMemory near exhaustion — the container's own alert path may be "
                "unreliable under this pressure; free memory or grow limits."
            )
        try:
            await dispatcher.send(Alert(severity=severity, title=title, body=body))
        except Exception:
            logger.warning("failed to send RAM alert", exc_info=True)

    # Persist tier every cycle (even without an alert) so a later rise from a
    # silently-decreased tier re-alerts correctly. Only advance the alert
    # timestamp when we actually alerted.
    new_alert_at = now if decision.should_alert else last_alert_at
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            json.dumps(
                {
                    "tier": tier,
                    "last_alert_at": new_alert_at.isoformat() if new_alert_at else None,
                }
            )
        )
    except OSError:
        logger.warning("failed to persist RAM alert state", exc_info=True)
