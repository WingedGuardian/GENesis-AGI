"""Storage-pool pressure RELIEF — HOST-SIDE.

``pool.py`` measures the pool and alerts in tiers; this module decides when
the guardian must FREE space it owns, and does it.

Why it exists: a thin pool once filled to 100% (container rootfs forced
read-only) while CRITICAL alerts fired every six hours for four days. The
space was held by the guardian's own healthy snapshot, which nothing was
allowed to delete: rotation is create-then-delete, and both the guardian's
pool gate and LVM's own autoextend threshold refused the create, so the old
snapshot stayed and kept diverging. Alerts are not a mechanism. This is.

Decisions come from MEASURED growth, not a fixed percentage — a percentage
says nothing about whether the pool fills in an hour or a month:

* a bounded history (``pool_history.jsonl`` in the guardian state dir, one
  sample per ``history_sample_interval_s``, at most ``history_max_samples``);
* the growth RATE — the worse of the last six hours and the last day, in used
  bytes, so a pool extend (which lowers data% without freeing anything) never
  reads as shrinkage;
* the worst BURST — the largest rise over any ten-minute window — which sizes
  the reserve kept free for writes faster than the tick can react to;
* the RUNWAY — hours until the data or metadata space is full at that rate.

Two stages (``assess_pressure``):

* EARLY — runway under ``early_horizon_hours``: free what is expendable. An
  opt-in LVM extend into VG free space that dmeventd's own autoextend cannot
  use, pre-recovery snapshots, and a healthy lifeline older than
  ``snapshots.lifeline_max_age_hours``.
* URGENT — runway under ``urgent_horizon_hours`` or free space below the burst
  reserve: any guardian snapshot, the rollback lifeline last.

A pool NOT under pressure never loses its rollback lifeline: the lifeline is
only refreshed while the container is healthy, so an unconditional age cap
would delete the only rollback target during exactly the outage that needs it.

At most one action per tick, then a settle of one history interval before
the next, so each effect is re-measured first — btrfs frees a deleted
subvolume asynchronously, and an immediate re-measure would read "nothing
changed" and walk on toward the lifeline. At most one extend a day. (Ticks
are 30s apart when the container is fine; the tick is a oneshot, so during an
outage's diagnosis they can be an hour apart — ample against day-scale
horizons.) Only guardian-owned things are ever touched — anything else filling
the pool is reported, never deleted.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from genesis.guardian.config import StoragePoolConfig
from genesis.guardian.pool import StoragePoolStatus
from genesis.guardian.snapshots import HEALTHY_SUFFIX
from genesis.util.atomic import atomic_write_text

logger = logging.getLogger(__name__)

HISTORY_FILE = "pool_history.jsonl"

# Rate windows. The short one catches a change of regime within hours, the
# long one a steady climb. Not one hour: a routine backup writes ~2 GB in well
# under an hour, and a 1h slope reads that as ~48 GB/day — relief would then
# delete the rollback snapshot on every backup. Sub-hour spikes are what the
# burst RESERVE is for, not the rate.
_SHORT_WINDOW = timedelta(hours=6)
_LONG_WINDOW = timedelta(hours=24)
# A slope over less than this is not a rate. Two hours, not less: taking a
# thin snapshot steps METADATA up at once (measured on a live pool: 32.7% →
# 37.1% within ~30 minutes of a fresh snapshot, then ~0.3%/h). A 30-minute
# slope reads that step as "metadata full in ~7h" and would delete the brand
# new lifeline on a freshly deployed guardian with no older history. Faster
# fills than the rate can see in 2h are what the burst RESERVE covers.
_MIN_RATE_SPAN = timedelta(hours=2)
# Burst window: the fastest rise the tick-sampled history can show.
_BURST_WINDOW = timedelta(minutes=10)
# Bursts older than this no longer size the reserve (see min_reserve_pct).
_BURST_LOOKBACK = timedelta(hours=24)

RELIEF_MODES = ("live", "alert_only", "off")


@dataclass(frozen=True)
class PoolSample:
    """One point of pool history. Fractions are 0..1 of the pool's own size."""

    ts: datetime
    data_frac: float | None
    meta_frac: float | None
    size_bytes: int | None

    @property
    def used_bytes(self) -> float | None:
        if self.data_frac is None or self.size_bytes is None:
            return None
        return self.data_frac * self.size_bytes


@dataclass(frozen=True)
class Runway:
    """Derived pressure numbers for the current sample. None = unknown."""

    free_bytes: int | None
    rate_bytes_per_h: float | None
    burst_bytes: float
    reserve_bytes: int | None
    data_hours_to_full: float | None
    meta_hours_to_full: float | None

    @property
    def hours_to_full(self) -> float | None:
        known = [h for h in (self.data_hours_to_full, self.meta_hours_to_full) if h is not None]
        return min(known) if known else None

    def describe(self) -> str:
        """Numbers for an alert body — every figure the decision used."""
        gib = 1024**3
        parts = []
        if self.free_bytes is not None:
            parts.append(f"free {self.free_bytes / gib:.1f}G")
        if self.reserve_bytes is not None:
            parts.append(f"reserve {self.reserve_bytes / gib:.1f}G")
        if self.rate_bytes_per_h is not None:
            parts.append(f"growth {self.rate_bytes_per_h * 24 / gib:.1f}G/day")
        else:
            parts.append("growth unknown (history too short)")
        if self.data_hours_to_full is not None:
            parts.append(f"data full in {self.data_hours_to_full:.0f}h")
        if self.meta_hours_to_full is not None:
            parts.append(f"metadata full in {self.meta_hours_to_full:.0f}h")
        return ", ".join(parts)


def effective_relief_mode(cfg: StoragePoolConfig) -> str:
    """The relief mode after the env kill switch; invalid → alert_only.

    Invalid degrades toward LESS authority (alert, never act), not toward off:
    a typo must not silently remove the alerts too.
    """
    raw = cfg.relief_mode
    # YAML 1.1 reads a bare `off` as boolean False (and `on` as True), so the
    # documented `relief_mode: off` arrives here as False, not "off".
    if raw is False:
        return "off"
    mode = raw if raw in RELIEF_MODES else "alert_only"
    if mode == "live" and os.environ.get("GUARDIAN_POOL_RELIEF_DISABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return "alert_only"
    return mode


def sample_from_status(status: StoragePoolStatus, now: datetime) -> PoolSample | None:
    """History sample for a measurement, or None when there is nothing to record."""
    if not status.detected:
        return None
    if status.data_pct is not None:
        data = status.data_pct / 100.0
    elif status.pool_used_pct is not None:
        data = status.pool_used_pct / 100.0
    else:
        return None
    meta = status.metadata_pct / 100.0 if status.metadata_pct is not None else None
    return PoolSample(now, data, meta, status.pool_size_bytes)


def _parse_line(line: str) -> PoolSample | None:
    try:
        raw = json.loads(line)
        ts = datetime.fromisoformat(raw["ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        data = raw.get("data")
        meta = raw.get("meta")
        size = raw.get("size")
        return PoolSample(
            ts,
            float(data) if data is not None else None,
            float(meta) if meta is not None else None,
            int(size) if size is not None else None,
        )
    except (ValueError, TypeError, KeyError):
        return None


def load_history(path: Path) -> list[PoolSample]:
    """Read the history, oldest first. Unreadable lines are skipped, not fatal."""
    try:
        text = path.read_text()
    except OSError:
        return []
    out = [s for s in (_parse_line(ln) for ln in text.splitlines() if ln.strip()) if s]
    out.sort(key=lambda s: s.ts)
    return out


def _dump(sample: PoolSample) -> str:
    return json.dumps(
        {
            "ts": sample.ts.isoformat(),
            "data": sample.data_frac,
            "meta": sample.meta_frac,
            "size": sample.size_bytes,
        }
    )


def record_sample(
    path: Path,
    sample: PoolSample,
    *,
    min_interval_s: int,
    max_samples: int,
) -> bool:
    """Append ``sample`` if one is due. Returns True when it was written.

    Bounded: once the file holds more than ``max_samples`` plus a 10% slack,
    it is rewritten atomically with the newest ``max_samples``. The slack keeps
    the rewrite to roughly once per ``max_samples / 10`` samples instead of on
    every tick.
    """
    history = load_history(path)
    if history:
        last = history[-1].ts
        # A clock stepped backwards (last > now) must not freeze sampling.
        if last <= sample.ts and (sample.ts - last).total_seconds() < min_interval_s:
            return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if len(history) + 1 > max_samples + max(1, max_samples // 10):
            keep = [*history, sample][-max_samples:]
            # mkstemp-based: 0600, and the temp never outlives a failed write.
            atomic_write_text(path, "".join(_dump(s) + "\n" for s in keep))
        else:
            with path.open("a") as fh:
                fh.write(_dump(sample) + "\n")
        os.chmod(path, 0o600)  # its rates drive destructive relief decisions
    except OSError:
        logger.warning("could not write pool history %s", path, exc_info=True)
        return False
    return True


def _slope_per_h(points: list[tuple[datetime, float]], now: datetime, window: timedelta):
    """Rise per hour from the oldest point inside ``window`` to the newest."""
    inside = [(t, v) for t, v in points if now - t <= window]
    if len(inside) < 2:
        return None
    (t0, v0), (t1, v1) = inside[0], inside[-1]
    span = t1 - t0
    if span < _MIN_RATE_SPAN:
        return None
    return (v1 - v0) / (span.total_seconds() / 3600.0)


def _rate(points: list[tuple[datetime, float]], now: datetime) -> float | None:
    slopes = [
        s
        for s in (
            _slope_per_h(points, now, _SHORT_WINDOW),
            _slope_per_h(points, now, _LONG_WINDOW),
        )
        if s is not None
    ]
    if not slopes:
        return None
    return max(0.0, max(slopes))


def _burst(points: list[tuple[datetime, float]]) -> float:
    """Largest rise between two points at most ``_BURST_WINDOW`` apart."""
    worst = 0.0
    start = 0
    for j, (tj, vj) in enumerate(points):
        while tj - points[start][0] > _BURST_WINDOW:
            start += 1
        for i in range(start, j):
            rise = vj - points[i][1]
            if rise > worst:
                worst = rise
    return worst


def compute_runway(
    history: list[PoolSample],
    current: PoolSample,
    cfg: StoragePoolConfig,
) -> Runway:
    """Rate, burst, reserve and hours-to-full for ``current`` given ``history``."""
    samples = [s for s in history if s.ts <= current.ts]
    if not samples or samples[-1].ts != current.ts:
        samples.append(current)
    now = current.ts

    used_pts = [(s.ts, s.used_bytes) for s in samples if s.used_bytes is not None]
    rate = _rate(used_pts, now) if used_pts else None
    recent = [(t, v) for t, v in used_pts if now - t <= _BURST_LOOKBACK]
    burst = _burst(recent) if recent else 0.0

    free: int | None = None
    reserve: int | None = None
    data_h: float | None = None
    if current.used_bytes is not None and current.size_bytes is not None:
        free = max(0, int(current.size_bytes - current.used_bytes))
        floor = int(cfg.min_reserve_pct / 100.0 * current.size_bytes)
        reserve = max(int(burst * cfg.burst_multiplier), floor)
        if rate:
            data_h = free / rate

    meta_h: float | None = None
    meta_pts = [(s.ts, s.meta_frac) for s in samples if s.meta_frac is not None]
    if current.meta_frac is not None and meta_pts:
        meta_rate = _rate(meta_pts, now)
        if meta_rate:
            meta_h = max(0.0, 1.0 - current.meta_frac) / meta_rate

    return Runway(
        free_bytes=free,
        rate_bytes_per_h=rate,
        burst_bytes=burst,
        reserve_bytes=reserve,
        data_hours_to_full=data_h,
        meta_hours_to_full=meta_h,
    )


def _ambiguous_pool(status: StoragePoolStatus) -> bool:
    """More than one thin pool in the VG: the percentages may belong to a
    different pool than the container's. Neither the relief pass nor the
    delete-first decision may act on a pool it cannot name (the tier alerts
    still report it). One check, shared by both entry points."""
    return status.data_pct is not None and bool(status.vg_name) and not status.thinpool_lv


LEVEL_NONE = "none"
LEVEL_EARLY = "early"
LEVEL_URGENT = "urgent"


def assess_pressure(runway: Runway, cfg: StoragePoolConfig) -> tuple[str, str]:
    """(level, reason). The reason names the rule that fired, with its horizon."""
    if (
        runway.free_bytes is not None
        and runway.reserve_bytes is not None
        and runway.free_bytes < runway.reserve_bytes
    ):
        return LEVEL_URGENT, "free space is below the burst reserve"
    for level, horizon in (
        (LEVEL_URGENT, cfg.urgent_horizon_hours),
        (LEVEL_EARLY, cfg.early_horizon_hours),
    ):
        if runway.data_hours_to_full is not None and runway.data_hours_to_full < horizon:
            return level, f"data runway under {horizon:.0f}h at the measured growth rate"
        if runway.meta_hours_to_full is not None and runway.meta_hours_to_full < horizon:
            return level, f"metadata runway under {horizon:.0f}h at the measured growth rate"
    return LEVEL_NONE, ""


@dataclass(frozen=True)
class SnapshotInfo:
    name: str
    created: datetime | None
    healthy: bool


def data_pressed(runway: Runway, cfg: StoragePoolConfig) -> bool:
    """True when DATA space (not only metadata) is what pressure is about."""
    if (
        runway.free_bytes is not None
        and runway.reserve_bytes is not None
        and runway.free_bytes < runway.reserve_bytes
    ):
        return True
    return (
        runway.data_hours_to_full is not None
        and runway.data_hours_to_full < cfg.early_horizon_hours
    )


def plan_delete(
    snaps: list[SnapshotInfo],
    level: str,
    now: datetime,
    lifeline_max_age_hours: float,
) -> str | None:
    """The ONE guardian snapshot to delete next at ``level``, or None.

    Order: pre-recovery (non-healthy) snapshots oldest first — they are
    captures of an already-broken state (recovery.py ranks them the same way)
    — then healthy ones oldest first, so the newest healthy snapshot (the
    rollback lifeline) is always the last thing freed. Superseded healthy
    snapshots (left behind when a rotation's delete failed) go before the
    lifeline at either stage. At the EARLY stage the lifeline itself qualifies
    only once it is older than the lifeline cap; an unknown age never does.
    """
    if level == LEVEL_NONE:
        return None

    def _age_key(s: SnapshotInfo) -> datetime:
        return s.created or datetime.min.replace(tzinfo=UTC)

    others = sorted((s for s in snaps if not s.healthy), key=_age_key)
    if others:
        return others[0].name
    healthy = sorted((s for s in snaps if s.healthy), key=_age_key)
    if not healthy:
        return None
    lifeline = healthy[-1]
    superseded = healthy[:-1]  # a rotation whose old-lifeline delete failed
    if superseded:
        return superseded[0].name
    if level == LEVEL_URGENT:
        return lifeline.name
    if lifeline_max_age_hours <= 0 or lifeline.created is None:
        return None
    if now - lifeline.created > timedelta(hours=lifeline_max_age_hours):
        return lifeline.name
    return None


def plan_extend(status: StoragePoolStatus, level: str, cfg: StoragePoolConfig) -> int | None:
    """Bytes to grow the thin pool by, or None when extending is not ours to do.

    Only when ALL hold — each guard is a reason dmeventd's autoextend (the
    mechanism the install opted into) should have acted and structurally
    could not:

    * pressure is on and this is LVM-thin with a named pool;
    * the pool carries the ``genesis-thinpool`` profile — the install's own
      opt-in to growing the pool into VG free space (host provisioning sets
      it); without it the free space may be reserved for something else;
    * data% has reached the profile's autoextend threshold;
    * VG free is SMALLER than one autoextend step. LVM refuses to extend at
      all when it cannot take the whole step, so that space would otherwise
      sit unused while the pool fills (measured: a pool filled to 100% with
      3.9 GB of VG free beside it). At or above one step, dmeventd can do it
      and this stays out of its way.

    Leaves ``extend_keep_free_mib`` (or twice the metadata LV, if larger)
    unallocated so thin-pool metadata can still be grown. Never issues a
    whole-VG token (expand._assert_no_full_extend is applied at execution).
    """
    from genesis.guardian.provisioning.expand import (
        AUTOEXTEND_PERCENT,
        AUTOEXTEND_PROFILE_NAME,
        AUTOEXTEND_THRESHOLD_PCT,
    )

    if level == LEVEL_NONE:
        return None
    if not status.vg_name or not status.thinpool_lv:
        return None
    if status.thinpool_profile != AUTOEXTEND_PROFILE_NAME:
        return None
    if status.data_pct is None or status.data_pct < AUTOEXTEND_THRESHOLD_PCT:
        return None
    if not status.vg_free_bytes or not status.pool_size_bytes:
        return None
    step = status.pool_size_bytes * AUTOEXTEND_PERCENT // 100
    if status.vg_free_bytes >= step:
        return None
    keep = max(cfg.extend_keep_free_mib * 1024**2, 2 * (status.metadata_size_bytes or 0))
    grow = status.vg_free_bytes - keep
    if grow < 1024**3:
        return None
    return grow


# --- the per-tick pass ------------------------------------------------------

STATE_FILE = "pool_relief_state.json"


def _load_state(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text())
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    try:
        # mkstemp-based: 0600 (it gates destructive actions via the settle and
        # extend throttles), and the temp never outlives a failed write.
        atomic_write_text(path, json.dumps(state))
    except OSError:
        logger.warning("could not persist pool relief state %s", path, exc_info=True)


def _due(state: dict, key: str, now: datetime, hours: float) -> bool:
    raw = state.get(key)
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return True
    return now - last >= timedelta(hours=hours) or last > now


async def _send(dispatcher, severity, title: str, body: str) -> None:
    from genesis.guardian.alert.base import Alert

    try:
        await dispatcher.send(Alert(severity=severity, title=title, body=body))
    except Exception:
        logger.warning("pool relief alert dispatch failed", exc_info=True)


async def _extend_thinpool(
    status: StoragePoolStatus,
    grow_bytes: int,
    run,
) -> tuple[bool, bool, str]:
    """``lvextend -L +<bytes>b vg/thinpool``, rounded DOWN to whole extents.

    Returns (ok, attempted, detail): ``attempted`` is False when it failed
    before issuing the mutation, so the ledger records only real attempts.

    The target is re-resolved the way the provisioning path resolves it and
    must match the LV the measurement named — never extend a pool we did not
    measure.
    """
    from genesis.guardian.provisioning.expand import _assert_no_full_extend

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
    except (ValueError, IndexError):
        extent = 0
    if extent <= 0:
        return False, False, f"could not read the extent size of VG {vg}: {(err or out)[:160]}"
    grow = grow_bytes // extent * extent
    if grow <= 0:
        return False, False, "grow rounds down to zero extents"
    argv = ("sudo", "-n", "lvextend", "-L", f"+{grow}b", f"{vg}/{lv}")
    _assert_no_full_extend(argv)
    rc, out, err = await run(*argv, timeout=60.0)
    if rc != 0:
        return False, True, f"lvextend failed: {(err or out)[:200]}"
    return True, True, f"grew {vg}/{lv} by {grow / 1024**3:.1f}G"


async def _named_thinpool_matches(config, status: StoragePoolStatus, run) -> bool:
    from genesis.guardian.pool import _detect_pool_name
    from genesis.guardian.provisioning.expand import _resolve_thinpool_lv

    pool_name = await _detect_pool_name(config)
    if not pool_name or not status.vg_name:
        return False
    return await _resolve_thinpool_lv(pool_name, status.vg_name, run) == status.thinpool_lv


async def check_pool_pressure(
    config,
    dispatcher,
    snapshots,
    *,
    now: datetime | None = None,
    run=None,
) -> str:
    """One relief pass. Returns what it did (for logs/tests). Never raises."""
    from genesis.guardian._subprocess import run_subprocess
    from genesis.guardian.alert.base import AlertSeverity
    from genesis.guardian.pool import measure_storage_pool
    from genesis.guardian.provisioning.ledger import ProvisioningLedger

    run = run or run_subprocess
    now = now or datetime.now(UTC)
    cfg = config.storage_pool
    if not cfg.enabled:
        return "disabled"
    mode = effective_relief_mode(cfg)
    if mode == "off":
        return "off"
    state_path = config.state_path / STATE_FILE
    state = _load_state(state_path)

    if cfg.relief_mode not in RELIEF_MODES and _due(state, "invalid_mode", now, 24):
        await _send(
            dispatcher,
            AlertSeverity.WARNING,
            "Pool relief not live",
            f"storage_pool.relief_mode={cfg.relief_mode!r} is not one of "
            f"{', '.join(RELIEF_MODES)} — running alert_only (guardian will NOT free "
            "pool space on its own).",
        )
        state["invalid_mode"] = now.isoformat()
        _save_state(state_path, state)

    status = await measure_storage_pool(config)
    sample = sample_from_status(status, now)
    if sample is None:
        return "unmeasured"
    if _ambiguous_pool(status):
        return "ambiguous_pool"
    hist_path = config.state_path / HISTORY_FILE
    record_sample(
        hist_path,
        sample,
        min_interval_s=cfg.history_sample_interval_s,
        max_samples=cfg.history_max_samples,
    )
    runway = compute_runway(load_history(hist_path), sample, cfg)
    level, reason = assess_pressure(runway, cfg)
    if level == LEVEL_NONE:
        return "ok"

    numbers = runway.describe()
    severity = AlertSeverity.CRITICAL if level == LEVEL_URGENT else AlertSeverity.WARNING

    if mode == "alert_only":
        if _due(state, f"would_{level}", now, cfg.realert_hours):
            await _send(
                dispatcher,
                severity,
                f"Pool pressure ({level}) — relief is alert-only",
                f"{reason}. {numbers}. Relief would free guardian-owned space now, but "
                "storage_pool.relief_mode / GUARDIAN_POOL_RELIEF_DISABLED keeps it off.",
            )
            state[f"would_{level}"] = now.isoformat()
            _save_state(state_path, state)
        return f"alert_only:{level}"

    # Settle: after freeing something, wait one history interval before
    # freeing more. On LVM the space returns at once; on btrfs the cleaner
    # frees a deleted subvolume in the background, so an immediate re-measure
    # would read "nothing changed" and walk on toward the rollback lifeline.
    if not _due(state, "last_action", now, cfg.history_sample_interval_s / 3600.0):
        return f"settling:{level}"

    # 1. Opt-in LVM extend into VG space dmeventd's autoextend cannot use.
    #    Only for DATA pressure (growing the data LV does nothing for
    #    metadata), and at most once a day — each extend is permanent.
    grow = plan_extend(status, level, cfg) if data_pressed(runway, cfg) else None
    if (
        grow is not None
        and _due(state, "extend", now, 24)
        and await _named_thinpool_matches(config, status, run)
    ):
        ok, attempted, detail = await _extend_thinpool(status, grow, run)
        state["extend"] = now.isoformat()
        if attempted:
            ProvisioningLedger(config.state_dir).record_action(
                "pool_extend",
                f"+{grow}b",
                ok,
                ok,
            )
        if ok:
            state["last_action"] = now.isoformat()
            _save_state(state_path, state)
            await _send(
                dispatcher,
                AlertSeverity.WARNING,
                "Guardian extended the thin pool",
                f"{reason}. {numbers}. {detail} into VG free space that LVM's "
                "autoextend could not use (it needs a full step). A thin pool "
                "cannot shrink; grow the VM disk to restore autoextend headroom.",
            )
            return "extended"
        _save_state(state_path, state)
        await _send(
            dispatcher,
            AlertSeverity.WARNING,
            "Guardian could not extend the thin pool",
            f"{reason}. {numbers}. {detail}. Falling back to deleting guardian snapshots.",
        )

    # 2. Delete ONE guardian-owned snapshot; later ticks re-measure.
    meta = await snapshots.list_snapshot_meta_strict()
    if meta is None:
        if _due(state, "list_failed", now, cfg.realert_hours):
            await _send(
                dispatcher,
                severity,
                "Pool pressure — cannot list snapshots",
                f"{reason}. {numbers}. `incus snapshot list` failed, so the guardian "
                "cannot tell what it could free. Check incus on the host.",
            )
            state["list_failed"] = now.isoformat()
            _save_state(state_path, state)
        return "list_failed"
    infos = [SnapshotInfo(n, c, n.endswith(HEALTHY_SUFFIX)) for n, c in meta]
    target = plan_delete(infos, level, now, config.snapshots.lifeline_max_age_hours)
    if target is None:
        if level == LEVEL_URGENT and _due(state, "no_target", now, cfg.realert_hours):
            held = ", ".join(n for n, _ in meta) or "none"
            await _send(
                dispatcher,
                AlertSeverity.CRITICAL,
                "Pool filling — nothing left the guardian may free",
                f"{reason}. {numbers}. Guardian snapshots: {held}. Something other "
                "than guardian snapshots is consuming the pool — the guardian never "
                "deletes what it does not own. Grow the pool or free space by hand "
                "(docs/reference/thin-pool-recovery.md).",
            )
            state["no_target"] = now.isoformat()
            _save_state(state_path, state)
        return f"no_target:{level}"

    ok = await snapshots.delete(target)
    lifeline_note = (
        " This was the rollback lifeline: SNAPSHOT_ROLLBACK has no target until the "
        "next healthy snapshot."
        if target.endswith(HEALTHY_SUFFIX)
        else ""
    )
    if ok:
        state["last_action"] = now.isoformat()
        _save_state(state_path, state)
        await _send(
            dispatcher,
            severity,
            "Guardian freed pool space",
            f"{reason}. {numbers}. Deleted guardian snapshot {target}.{lifeline_note}",
        )
        return f"deleted:{target}"
    if _due(state, "delete_failed", now, 1):
        await _send(
            dispatcher,
            severity,
            "Guardian could not free pool space",
            f"{reason}. {numbers}. Deleting guardian snapshot {target} failed; retrying next tick.",
        )
        state["delete_failed"] = now.isoformat()
        _save_state(state_path, state)
    return f"delete_failed:{target}"


async def current_pressure(config) -> str:
    """The measured pressure level NOW, without recording or acting.

    For callers outside the relief pass (mark_healthy's delete-first decision)
    that must key a destructive choice on the SAME model relief uses, not on a
    static percentage. ``LEVEL_NONE`` unless relief is ``live`` (so the kill
    switch and ``alert_only`` stop delete-first rotation too — an emergency
    brake must cover every automatic delete), or when nothing can be measured
    or the pool is ambiguous: the conservative answer, since it forbids
    deleting first.
    """
    from genesis.guardian.pool import measure_storage_pool

    try:
        cfg = config.storage_pool
        if not cfg.enabled or effective_relief_mode(cfg) != "live":
            return LEVEL_NONE
        status = await measure_storage_pool(config)
        sample = sample_from_status(status, datetime.now(UTC))
        if sample is None or _ambiguous_pool(status):
            return LEVEL_NONE
        history = load_history(config.state_path / HISTORY_FILE)
        return assess_pressure(compute_runway(history, sample, cfg), cfg)[0]
    except Exception:
        # A bad config value (``_build_sub`` does not coerce types) or a probe
        # fault: answer NONE, which forbids delete-first. The relief pass hits
        # the same fault every tick and alerts on it daily.
        logger.warning("pressure assessment failed", exc_info=True)
        return LEVEL_NONE
