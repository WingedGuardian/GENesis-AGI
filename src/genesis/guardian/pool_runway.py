"""Measured pool growth: history, growth rate and runway — HOST-SIDE.

``pool_relief`` acts on a fixed reserve. That reserve says nothing about
whether a pool fills in an hour or a month, so relief also reads the pool's
MEASURED growth and acts early when data or metadata would fill within a
horizon. This module holds the maths; it deletes nothing.

Early relief gets less authority than the reserve (see ``pool_relief``): a
wrong rate can cost a pre-recovery snapshot, a superseded healthy one, or a
lifeline already past its age cap, never a young rollback lifeline. That
split is why the rules below can stay simple. The previous design gave a
slope from step-shaped samples the power to delete the lifeline, and every
review round found a new step shape that fooled it.

Growth is tracked in BYTES for data and metadata. LVM can grow either LV, and
a percentage then drops while usage keeps rising; bytes do not move with an
extend. Samples of different pools are never compared (the container can move
between pools).

A RATE is growth that shows in BOTH halves of a window (2h, 6h, 24h, 72h; the
worst wins), so a one-off step (a backup, a fresh snapshot's metadata jump)
lands in one half and is not a rate; the 72h window lets a step that recurs
daily read as its average. A window yields a rate once its samples span three
quarters of it. The one exception is a GAP: when the last two samples are
more than ``_GAP_RATE`` apart (ticks can be an hour or more apart while an
outage is being diagnosed), the rise across the gap is a rate by itself,
because there are no samples in between to show whether it was sustained.

Known limits: growth that started less than about an hour before the current
sample, across normally spaced samples, is not a rate yet, and for a while
after that it reads low. Two one-off steps that land in the two halves of one
window do read as a rate. The reserve covers the first; early relief's limited
authority bounds the cost of the second.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from genesis.guardian.pool import StoragePoolStatus
from genesis.util.atomic import atomic_write_text

logger = logging.getLogger(__name__)

HISTORY_FILE = "pool_history.jsonl"

_RATE_WINDOWS = (
    timedelta(hours=2),
    timedelta(hours=6),
    timedelta(hours=24),
    timedelta(hours=72),
)
# Samples are ~5 min apart, so a window's first sample is rarely exactly one
# window old: requiring the full span would make the 2h window never yield a
# rate (review).
_SPAN_FRACTION = 0.75
# Longer than several sample intervals, so an ordinary late tick is not a gap;
# shorter than the hour-plus gaps of an outage's diagnosis. Never less than
# three sample intervals (see gap_threshold): a longer configured interval
# would otherwise make every consecutive pair a "gap" and every one-off step
# a rate (review).
_GAP_RATE = timedelta(minutes=30)
_FUTURE_SLACK = timedelta(hours=1)


def gap_threshold(sample_interval_s: int) -> timedelta:
    return max(_GAP_RATE, timedelta(seconds=3 * sample_interval_s))


@dataclass(frozen=True)
class PoolSample:
    """One point of pool history: bytes used and LV sizes. None = unknown."""

    ts: datetime
    pool: str | None
    data_used: float | None
    data_size: int | None
    meta_used: float | None = None
    meta_size: int | None = None


@dataclass(frozen=True)
class Runway:
    """Growth rates (bytes/h) and hours until full. None = unknown."""

    data_rate: float | None
    data_hours: float | None
    meta_rate: float | None
    meta_hours: float | None

    def describe(self) -> str:
        gib = 1024**3
        parts = []
        if self.data_rate is not None:
            parts.append(f"data growth {self.data_rate * 24 / gib:.1f}G/day")
        if self.data_hours is not None:
            parts.append(f"data full in ~{self.data_hours:.0f}h")
        if self.meta_rate is not None:
            parts.append(f"metadata growth {self.meta_rate * 24 / 1024**2:.1f}M/day")
        if self.meta_hours is not None:
            parts.append(f"metadata full in ~{self.meta_hours:.0f}h")
        return ", ".join(parts) if parts else "growth unknown (history too short)"


def sample_from_status(
    status: StoragePoolStatus,
    now: datetime,
    pool: str | None,
) -> PoolSample | None:
    """History sample for a measurement, or None when it carries no bytes."""
    if not status.detected or pool is None:
        return None
    pct = status.data_pct if status.data_pct is not None else status.pool_used_pct
    size = status.pool_size_bytes
    data_used = pct / 100.0 * size if pct is not None and size else None
    meta_size = status.metadata_size_bytes
    meta_used = (
        status.metadata_pct / 100.0 * meta_size
        if status.metadata_pct is not None and meta_size
        else None
    )
    if data_used is None and meta_used is None:
        return None
    return PoolSample(now, pool, data_used, size, meta_used, meta_size)


def _num(raw) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    val = float(raw)
    # Byte counts and sizes: a negative or non-finite value is corruption.
    return val if val == val and abs(val) != float("inf") and val >= 0 else None


def _parse_line(line: str) -> PoolSample | None:
    try:
        raw = json.loads(line)
        ts = datetime.fromisoformat(raw["ts"])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        data_size, meta_size = _num(raw.get("data_size")), _num(raw.get("meta_size"))
        pool = raw.get("pool")
        return PoolSample(
            ts,
            str(pool) if pool is not None else None,
            _num(raw.get("data_used")),
            int(data_size) if data_size is not None else None,
            _num(raw.get("meta_used")),
            int(meta_size) if meta_size is not None else None,
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def load_history(path: Path) -> list[PoolSample]:
    """The history, oldest first. Unreadable lines are skipped, not fatal."""
    try:
        # Decoded tolerantly: one undecodable byte must cost its line, not the
        # whole history (and record_sample, which loads first, would then never
        # rewrite the file to heal it) (review).
        text = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = [s for s in (_parse_line(ln) for ln in text.splitlines() if ln.strip()) if s]
    out.sort(key=lambda s: s.ts)
    return out


def _dump(s: PoolSample) -> str:
    return json.dumps(
        {
            "ts": s.ts.isoformat(),
            "pool": s.pool,
            "data_used": s.data_used,
            "data_size": s.data_size,
            "meta_used": s.meta_used,
            "meta_size": s.meta_size,
        }
    )


def record_sample(
    path: Path,
    sample: PoolSample,
    *,
    min_interval_s: int,
    max_samples: int,
) -> list[PoolSample]:
    """Append ``sample`` if one is due; return the history it belongs to.

    Bounded: past ``max_samples`` plus a 10% slack the file is rewritten
    atomically with the newest ``max_samples``, so the rewrite happens about
    once per ``max_samples / 10`` samples rather than on every tick. A failed
    write is logged and the in-memory history still includes the sample.
    """
    loaded = load_history(path)
    # A sample from the future (a clock stepped forward, then corrected) would
    # stay history[-1] and defeat the interval gate on every pass, flooding the
    # file and pushing real samples out (review). Drop them.
    history = [s for s in loaded if s.ts <= sample.ts + _FUTURE_SLACK]
    past = [s.ts for s in history if s.ts <= sample.ts]
    if past and (sample.ts - max(past)).total_seconds() < min_interval_s:
        return history
    history.append(sample)
    history.sort(key=lambda s: s.ts)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if len(history) != len(loaded) + 1 or len(history) > max_samples + max(1, max_samples // 10):
            history = history[-max_samples:]
            atomic_write_text(path, "".join(_dump(s) + "\n" for s in history))
        else:
            with path.open("a") as fh:
                fh.write(_dump(sample) + "\n")
            os.chmod(path, 0o600)  # its rates steer automatic deletes
    except OSError:
        logger.warning("could not write pool history %s", path, exc_info=True)
    return history


def _slope(a: tuple[datetime, float], b: tuple[datetime, float]) -> float | None:
    hours = (b[0] - a[0]).total_seconds() / 3600.0
    return (b[1] - a[1]) / hours if hours > 0 else None


def _sustained(
    points: list[tuple[datetime, float]], now: datetime, window: timedelta
) -> float | None:
    """Growth per hour that shows in both halves of ``window``, or None."""
    inside = [p for p in points if timedelta(0) <= now - p[0] <= window]
    if len(inside) < 3 or inside[-1][0] - inside[0][0] < window * _SPAN_FRACTION:
        return None
    mid = inside[0][0] + (inside[-1][0] - inside[0][0]) / 2
    first = [p for p in inside if p[0] <= mid]
    second = [p for p in inside if p[0] >= mid]
    if len(first) < 2 or len(second) < 2:
        return None
    s1, s2 = _slope(first[0], first[-1]), _slope(second[0], second[-1])
    if s1 is None or s2 is None:
        return None
    return min(s1, s2)


def growth_rate(
    points: list[tuple[datetime, float]],
    now: datetime,
    gap: timedelta = _GAP_RATE,
    recorded_until: datetime | None = None,
) -> float | None:
    """Worst growth per hour (>= 0) from the windows and a trailing gap, or None.

    The gap is judged on the last two points, AND on the last two RECORDED
    points (``recorded_until``) while the current reading is within one gap
    of the last one. Otherwise the gap rate lives for one pass only: the pass
    right after the post-gap sample is recorded (seconds later, the next
    tick's pre-cycle pass) would see no gap at all (review).
    """
    rates = [r for r in (_sustained(points, now, w) for w in _RATE_WINDOWS) if r is not None]
    tails = [points]
    if recorded_until is not None:
        recorded = [p for p in points if p[0] <= recorded_until]
        if len(recorded) >= 2 and now - recorded[-1][0] <= gap:
            tails.append(recorded)
    for tail in tails:
        if len(tail) >= 2 and tail[-1][0] - tail[-2][0] > gap:
            gap_rate = _slope(tail[-2], tail[-1])
            if gap_rate is not None:
                rates.append(gap_rate)
    return max(0.0, max(rates)) if rates else None


def compute_runway(
    history: list[PoolSample], current: PoolSample, gap: timedelta = _GAP_RATE,
) -> Runway:
    """Rates and hours-to-full for ``current``, from its own pool's history."""
    samples = [s for s in history if s.ts <= current.ts and s.pool == current.pool]
    recorded_until = max((s.ts for s in samples), default=None)
    if not samples or samples[-1] != current:
        samples = [s for s in samples if s.ts != current.ts] + [current]
    now = current.ts

    def _axis(used_of, size_of) -> tuple[float | None, float | None]:
        used, size = used_of(current), size_of(current)
        if used is None or not size:
            return None, None
        pts = [(s.ts, used_of(s)) for s in samples if used_of(s) is not None]
        rate = growth_rate(pts, now, gap, recorded_until)
        if not rate:
            return rate, None
        return rate, max(0.0, size - used) / rate

    data_rate, data_hours = _axis(lambda s: s.data_used, lambda s: s.data_size)
    meta_rate, meta_hours = _axis(lambda s: s.meta_used, lambda s: s.meta_size)
    return Runway(data_rate, data_hours, meta_rate, meta_hours)


def early_reason(runway: Runway, horizon_hours: float) -> str | None:
    """Why the pool would fill within the horizon (the reason text), or None.

    Metadata first: a full metadata LV needs an offline repair.
    """
    if not horizon_hours or horizon_hours <= 0:
        return None
    if runway.meta_hours is not None and runway.meta_hours < horizon_hours:
        return (
            f"metadata would fill in ~{runway.meta_hours:.0f}h at its measured growth "
            f"(under the {horizon_hours:g}h early horizon)"
        )
    if runway.data_hours is not None and runway.data_hours < horizon_hours:
        return (
            f"data would fill in ~{runway.data_hours:.0f}h at its measured growth "
            f"(under the {horizon_hours:g}h early horizon)"
        )
    return None


# --- the early level's configuration -------------------------------------------


def _bad_num(cfg, name: str, lo: float, hi: float) -> str | None:
    v = getattr(cfg, name)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        return f"storage_pool.{name}={v!r} (expected a number in [{lo}, {hi}])"
    return None


def _bad_int(cfg, name: str, lo: int, hi: int) -> str | None:
    v = getattr(cfg, name)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        return f"storage_pool.{name}={v!r} (expected an integer in [{lo}, {hi}])"
    return None


def validate_early_config(config) -> str | None:
    """Why the EARLY level and the extend cannot be trusted, or None.

    Separate from :func:`validate_relief_config` on purpose: a typo in one of
    these keys turns off early relief and the extend only, never the reserve
    or delete-first rotation (review).
    """
    cfg = config.storage_pool
    for problem in (
        # 0 disables early relief / the lifeline age rule.
        _bad_num(cfg, "early_horizon_hours", 0, 24 * 30),
        _bad_num(cfg, "lifeline_max_age_hours", 0, 24 * 365),
        _bad_int(cfg, "history_sample_interval_s", 30, 24 * 3600),
        _bad_int(cfg, "history_max_samples", 10, 100_000),
        _bad_int(cfg, "extend_keep_free_mib", 0, 1024 * 1024),
    ):
        if problem:
            return problem
    # The shortest rate window needs 1.5h of history (75% of 2h): a history
    # that cannot hold 2h would leave early relief silently rate-less (review).
    if cfg.history_max_samples * cfg.history_sample_interval_s < 2 * 3600:
        return (
            f"storage_pool.history_max_samples={cfg.history_max_samples} x "
            f"history_sample_interval_s={cfg.history_sample_interval_s} holds under 2h "
            "of history, too little for any growth rate"
        )
    return None


# --- what the early level may take -------------------------------------------------


def lifeline_aged(created: datetime | None, now: datetime, cap_hours: float) -> bool:
    """The lifeline is older than the age cap; an unknown age, or a cap of 0,
    never is."""
    return cap_hours > 0 and created is not None and now - created > timedelta(hours=cap_hours)


def early_allowed(order: list[str], lifeline: str | None, lifeline_ok: bool) -> list[str]:
    """What EARLY relief may delete: ``order`` minus the rollback lifeline,
    unless ``lifeline_ok`` (see ``early_lifeline_ok``)."""
    if lifeline is None or lifeline_ok:
        return list(order)
    return [n for n in order if n != lifeline]


async def early_lifeline_ok(
    status: StoragePoolStatus, snapshots, lifeline_created: datetime | None,
    now: datetime, cap_hours: float,
) -> bool:
    """May EARLY relief take the rollback lifeline?

    Only once it is older than the cap (it has diverged the most by then).
    On LVM-thin, also only when LVM measures that the healthy snapshots hold
    space no live volume maps: the same evidence delete-first rotation
    requires, so a lifeline delete-first kept because it holds little is not
    deleted anyway, for nothing (review). btrfs/dir have no such measurement;
    age alone decides there.
    """
    if not lifeline_aged(lifeline_created, now, cap_hours):
        return False
    if not (status.vg_name and status.thinpool_lv):
        return True
    holds = getattr(snapshots, "lifeline_holds_space", None)
    if holds is None:
        return False
    try:
        return await holds() is not None
    except Exception:
        logger.warning("lifeline space measurement failed", exc_info=True)
        return False
