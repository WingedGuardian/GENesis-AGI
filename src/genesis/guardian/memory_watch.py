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
    AlertDecision,
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


def _worse(a: str, b: str) -> str:
    return a if _TIER_RANK[a] >= _TIER_RANK[b] else b


def _aware(ts: datetime) -> datetime:
    """A naive timestamp (a state file this code did not write) reads as UTC."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


def _hours_since(iso: str, now: datetime) -> float:
    """Hours since an ISO timestamp; 0 when it does not parse (treated as new)."""
    try:
        return max(0.0, (now - _aware(datetime.fromisoformat(iso))).total_seconds() / 3600.0)
    except (TypeError, ValueError):
        return 0.0


def _held_tier(
    pct: float | None, lines: tuple[float, float, float], prior: str, margin: float
) -> str:
    """One axis's tier with an exit margin.

    A rise takes effect at the line. A fall leaves a tier only once the reading
    is ``margin`` points below that tier's line, so a reading hovering at a
    line holds one tier instead of crossing it every tick. A blind axis (None)
    holds its prior tier: a failed read is no evidence that pressure eased.
    """
    if pct is None:
        return prior
    raw = _tier_for(pct, *lines)
    with_margin = _tier_for(pct + margin, *lines)
    held = prior if _TIER_RANK[prior] <= _TIER_RANK[with_margin] else with_margin
    return _worse(raw, held)


def _exit_margin(cfg: MemoryTiersConfig) -> float:
    """The configured exit margin in points, or 5 when it is not a number.
    A bad value must not take the RAM watch down every tick."""
    try:
        return max(0.0, float(cfg.exit_margin_pct))
    except (TypeError, ValueError):
        logger.warning(
            "memory_tiers.exit_margin_pct=%r is not a number; using 5", cfg.exit_margin_pct
        )
        return 5.0


def memory_axis_tiers(
    container_pct: float | None,
    host_pct: float | None,
    cfg: MemoryTiersConfig,
    prior: dict[str, str] | None = None,
) -> dict[str, str]:
    """Per-axis tiers, each held by the exit margin against its prior tier."""
    prior = prior or {}
    margin = _exit_margin(cfg)
    return {
        "container": _held_tier(
            container_pct,
            (cfg.container_warn_pct, cfg.container_high_pct, cfg.container_crit_pct),
            prior.get("container", TIER_OK),
            margin,
        ),
        "host": _held_tier(
            host_pct,
            (cfg.host_warn_pct, cfg.host_high_pct, cfg.host_crit_pct),
            prior.get("host", TIER_OK),
            margin,
        ),
    }


def memory_worst_tier(
    container_pct: float | None,
    host_pct: float | None,
    cfg: MemoryTiersConfig,
) -> str:
    """Highest tier across the container and host-VM RAM axes, from the readings
    alone (no prior, so no margin): the `ram-status` view.

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
    return _worse(c, h)


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

    State (the paged peak tier, each axis's held tier, the last page time)
    persists in the guardian state dir so hysteresis survives across the
    stateless per-tick invocations. Alerts go through the guardian's own
    dispatcher (host Telegram channel), which survives a thrashing/dead
    container — exactly when this matters most. Shaped like
    check._check_storage_pool_and_alert, plus the flap damping below.
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

    # State: ``tier`` is the highest tier PAGED since the last recovery (the
    # alert baseline) and ``axes`` the tier each axis currently holds. Paging
    # against the peak, not the last reading, is what stops a reading that
    # dips HIGH→WARN→HIGH from re-paging "tier rose" each time. A state file
    # from before ``axes`` existed seeds both axes from its ``tier``.
    state_file = config.state_path / "memory_alert_state.json"
    peak = TIER_OK
    paged: str | None = None
    axes_prior: dict[str, str] = {}
    blind_prior: dict[str, str] = {}
    last_alert_at: datetime | None = None
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text())
            raw_peak = data.get("tier", TIER_OK)
            peak = raw_peak if isinstance(raw_peak, str) and raw_peak in _TIER_RANK else TIER_OK
            raw_paged = data.get("paged")
            if isinstance(raw_paged, str) and raw_paged in _TIER_RANK:
                paged = raw_paged
            raw_axes = data.get("axes")
            if isinstance(raw_axes, dict):
                axes_prior = {
                    k: v for k, v in raw_axes.items() if isinstance(v, str) and v in _TIER_RANK
                }
            else:
                axes_prior = {"container": peak, "host": peak}
            raw_blind = data.get("blind_since")
            if isinstance(raw_blind, dict):
                blind_prior = {k: v for k, v in raw_blind.items() if isinstance(v, str)}
            raw_at = data.get("last_alert_at")
            last_alert_at = _aware(datetime.fromisoformat(raw_at)) if raw_at else None
        except (ValueError, OSError, AttributeError, TypeError):
            # A state file this code did not write must never disable the watch:
            # whatever was read stands, the rest defaults, and the write below
            # replaces the file.
            pass

    axes = memory_axis_tiers(container_pct, host_pct, cfg, axes_prior)
    tier = _worse(axes["container"], axes["host"])

    now = datetime.now(UTC)
    # When each blind axis went blind (kept while it stays blind, dropped once
    # it is read again), so a hold that has lasted a whole re-alert window can
    # be reported as stale instead of as live pressure.
    blind_since: dict[str, str] = {}
    for pct, axis in ((container_pct, "container"), (host_pct, "host")):
        if pct is None:
            blind_since[axis] = blind_prior.get(axis) or now.isoformat()
    # The peak's memory lasts one re-alert window. Within it, a reading back
    # at a tier already paged is not news; after it, a sustained lower tier
    # gets its reminder and a rise from there pages again, as before (the
    # decide_alert contract is that the caller records falls; memory_watch
    # records them late, on purpose).
    baseline = peak
    if 0 < _TIER_RANK[tier] < _TIER_RANK[peak] and (
        last_alert_at is None or (now - last_alert_at).total_seconds() >= cfg.realert_hours * 3600
    ):
        baseline = tier
    decision = decide_alert(tier, baseline, last_alert_at, now, cfg.realert_hours)

    # Severity and the exhaustion warning follow what was MEASURED (plus what
    # a blind axis holds), not a tier the exit margin is holding up: 87% held
    # at CRIT by the margin is not "near exhaustion". A blind axis's hold
    # counts for one re-alert window; after that it is stale and no longer
    # sets the severity (the page still names it).
    measured = memory_worst_tier(container_pct, host_pct, cfg)
    blind_hours: dict[str, float] = {}
    for pct, axis in ((container_pct, "container"), (host_pct, "host")):
        if pct is None:
            blind_hours[axis] = _hours_since(blind_since[axis], now)
            if blind_hours[axis] < cfg.realert_hours:
                measured = _worse(measured, axes[axis])
    # ``live`` is ``tier`` without a stale blind hold: what the readings (with
    # their exit margin) and a fresh blind hold support. ``paged`` is the live
    # tier the last page went out at. A stale hold can keep ``tier`` level
    # while the pressure behind it rises, e.g. a container blind since a CRIT
    # page while host RAM climbs to CRIT: that rise is news, so it pages. The
    # margin stays in ``live``, so a reading hovering at a line still pages
    # once. A state file from before ``paged`` existed seeds it from the peak.
    live = TIER_OK
    for pct, axis in ((container_pct, "container"), (host_pct, "host")):
        if pct is not None or blind_hours[axis] < cfg.realert_hours:
            live = _worse(live, axes[axis])
    if paged is None:
        paged = peak
    if not decision.should_alert and _TIER_RANK[live] > _TIER_RANK[paged]:
        decision = AlertDecision(True, tier, f"pressure rose {paged}→{live}")

    if decision.should_alert:
        if decision.is_resolution:
            severity = AlertSeverity.INFO
            title = "RAM pressure recovered"
        else:
            severity = _MEM_TIER_SEVERITY.get(measured, AlertSeverity.WARNING)
            title = f"RAM {tier.upper()}"
        parts = []
        for pct, axis in ((container_pct, "container"), (host_pct, "host")):
            if pct is not None:
                parts.append(f"{axis} {pct:.0f}%")
            elif axes[axis] != TIER_OK:
                # Say why a page fires when the axis behind it cannot be read.
                parts.append(
                    f"{axis} unmeasured for {blind_hours[axis]:.1f}h (holding {axes[axis]})"
                )
        body = f"{decision.reason}. " + ", ".join(parts)
        if measured != tier and not decision.is_resolution:
            body += (
                f" (measured {measured}; held at {tier} until the reading falls "
                f"{_exit_margin(cfg):g} points below the {tier} line)"
            )
        if measured == TIER_CRIT:
            body += (
                "\nMemory near exhaustion — the container's own alert path may be "
                "unreliable under this pressure; free memory or grow limits."
            )
        try:
            await dispatcher.send(Alert(severity=severity, title=title, body=body))
        except Exception:
            logger.warning("failed to send RAM alert", exc_info=True)

    # The peak moves only when a page goes out: up on a rise, to ok on the
    # recovery. A silent fall keeps it, so the next rise to the same tier is
    # not news. The axes are persisted every cycle so the exit margin and the
    # blind-axis hold see the tier each axis held last time.
    if decision.should_alert:
        peak = TIER_OK if decision.is_resolution else tier
        paged = TIER_OK if decision.is_resolution else live
    new_alert_at = now if decision.should_alert else last_alert_at
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            json.dumps(
                {
                    "tier": peak,
                    "paged": paged,
                    "axes": axes,
                    "blind_since": blind_since,
                    "last_alert_at": new_alert_at.isoformat() if new_alert_at else None,
                }
            )
        )
    except OSError:
        logger.warning("failed to persist RAM alert state", exc_info=True)
