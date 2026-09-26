"""Tests for guardian pool-pressure maths + history (pool_pressure.py).

The acceptance fixture is the shape of a real thin-pool exhaustion: a pool at
~75% whose one snapshot kept diverging at ~2.3 GB/day until the pool hit 100%
about seven days later. The relief rule must fire with a day or more to spare.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from genesis.guardian.config import StoragePoolConfig
from genesis.guardian.pool import StoragePoolStatus
from genesis.guardian.pool_pressure import (
    LEVEL_EARLY,
    LEVEL_NONE,
    LEVEL_URGENT,
    PoolSample,
    SnapshotInfo,
    assess_pressure,
    check_pool_pressure,
    compute_runway,
    data_pressed,
    effective_relief_mode,
    load_history,
    plan_delete,
    plan_extend,
    record_sample,
    sample_from_status,
)


def relief_reason(rw, cfg):
    """Reason text when ANY stage fires (test shorthand)."""
    level, reason = assess_pressure(rw, cfg)
    return reason if level != LEVEL_NONE else None


_GB = 1024**3
T0 = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)


def _cfg(**kw) -> StoragePoolConfig:
    return StoragePoolConfig(**kw)


def _series(points: list[tuple[float, float]], size: int, step_min: int = 5):
    """Linear-interpolated samples between (hours_from_T0, data_pct) points."""
    out: list[PoolSample] = []
    for (h0, p0), (h1, p1) in zip(points, points[1:], strict=False):
        t = h0
        while t < h1:
            frac = (p0 + (p1 - p0) * (t - h0) / (h1 - h0)) / 100.0
            out.append(PoolSample(T0 + timedelta(hours=t), frac, 0.4, size))
            t += step_min / 60.0
    h, p = points[-1]
    out.append(PoolSample(T0 + timedelta(hours=h), p / 100.0, 0.4, size))
    return out


def _meta_series(data: float, m0: float, m1: float, hours: int, size: int):
    """Hourly samples with metadata rising linearly m0 -> m1, data flat."""
    return [
        PoolSample(T0 + timedelta(hours=h), data, m0 + (m1 - m0) * h / hours, size)
        for h in range(hours + 1)
    ]


# --- sample_from_status -----------------------------------------------------


class TestSampleFromStatus:
    def test_lvm_uses_data_and_meta(self) -> None:
        s = sample_from_status(
            StoragePoolStatus(
                detected=True, data_pct=69.4, metadata_pct=32.7, pool_size_bytes=70 * _GB
            ),
            T0,
        )
        assert s is not None
        assert s.data_frac == pytest.approx(0.694)
        assert s.meta_frac == pytest.approx(0.327)
        assert s.size_bytes == 70 * _GB

    def test_non_lvm_falls_back_to_pool_used(self) -> None:
        s = sample_from_status(
            StoragePoolStatus(detected=True, pool_used_pct=50.0, pool_size_bytes=100 * _GB),
            T0,
        )
        assert s is not None and s.data_frac == pytest.approx(0.5)
        assert s.meta_frac is None

    def test_undetected_or_empty_is_none(self) -> None:
        assert sample_from_status(StoragePoolStatus(detected=False), T0) is None
        assert sample_from_status(StoragePoolStatus(detected=True), T0) is None


# --- history persistence ----------------------------------------------------


class TestHistory:
    def test_missing_file_is_empty(self, tmp_path) -> None:
        assert load_history(tmp_path / "h.jsonl") == []

    def test_record_respects_interval_and_roundtrips(self, tmp_path) -> None:
        p = tmp_path / "h.jsonl"
        a = PoolSample(T0, 0.5, 0.3, 10 * _GB)
        assert record_sample(p, a, min_interval_s=300, max_samples=100) is True
        # 60s later: not due
        b = PoolSample(T0 + timedelta(seconds=60), 0.51, 0.3, 10 * _GB)
        assert record_sample(p, b, min_interval_s=300, max_samples=100) is False
        c = PoolSample(T0 + timedelta(seconds=301), 0.52, 0.3, 10 * _GB)
        assert record_sample(p, c, min_interval_s=300, max_samples=100) is True
        got = load_history(p)
        assert [s.data_frac for s in got] == [0.5, 0.52]
        assert got[0].ts == T0

    def test_bounded_to_max_samples(self, tmp_path) -> None:
        p = tmp_path / "h.jsonl"
        for i in range(50):
            record_sample(
                p,
                PoolSample(T0 + timedelta(minutes=5 * i), 0.5, None, _GB),
                min_interval_s=300,
                max_samples=20,
            )
        got = load_history(p)
        # Trimmed with hysteresis: never above max_samples + slack, newest kept.
        assert len(got) <= 20 + 20 // 10 + 1
        assert got[-1].ts == T0 + timedelta(minutes=5 * 49)
        assert len(p.read_text().splitlines()) == len(got)

    def test_corrupt_lines_are_skipped(self, tmp_path) -> None:
        p = tmp_path / "h.jsonl"
        good = {"ts": T0.isoformat(), "data": 0.5, "meta": None, "size": _GB}
        p.write_text("not json\n" + json.dumps(good) + '\n{"ts": "bad"}\n')
        got = load_history(p)
        assert len(got) == 1 and got[0].data_frac == 0.5

    def test_clock_going_backwards_still_records(self, tmp_path) -> None:
        """A host clock step back must not freeze sampling forever."""
        p = tmp_path / "h.jsonl"
        record_sample(p, PoolSample(T0, 0.5, None, _GB), min_interval_s=300, max_samples=100)
        back = PoolSample(T0 - timedelta(hours=2), 0.6, None, _GB)
        assert record_sample(p, back, min_interval_s=300, max_samples=100) is True


# --- runway maths -----------------------------------------------------------


class TestRunway:
    def test_short_history_has_no_rate_but_has_floor_reserve(self) -> None:
        size = 100 * _GB
        hist = [PoolSample(T0, 0.5, 0.3, size)]
        cur = PoolSample(T0 + timedelta(minutes=5), 0.5, 0.3, size)
        rw = compute_runway(hist, cur, _cfg())
        assert rw.rate_bytes_per_h is None
        assert rw.hours_to_full is None
        assert rw.reserve_bytes == int(0.03 * size)
        assert rw.free_bytes == 50 * _GB

    def test_flat_history_with_room_needs_no_relief(self) -> None:
        size = 100 * _GB
        hist = _series([(0, 60.0), (48, 60.0)], size)
        rw = compute_runway(hist, hist[-1], _cfg())
        assert rw.rate_bytes_per_h == 0
        assert rw.hours_to_full is None  # not growing → infinite runway
        assert relief_reason(rw, _cfg()) is None

    def test_rate_is_worse_of_short_and_long_window(self) -> None:
        size = 100 * _GB
        # Slow for 18h, then a fast last 6h: the short window must dominate.
        hist = _series([(0, 50.0), (18, 51.0), (24, 57.0)], size)
        rw = compute_runway(hist, hist[-1], _cfg())
        assert rw.rate_bytes_per_h == pytest.approx(1 * _GB, rel=0.1)

    def test_backup_sized_burst_does_not_trigger_relief(self) -> None:
        """A routine ~2 GB write burst on a pool with room must not cost the
        rollback snapshot. (With a 1h rate window it would: ~48 GB/day.)"""
        size = 70 * _GB
        hist = _series([(0, 70.0), (24, 70.5)], size)
        t = hist[-1].ts
        for k in range(1, 5):  # 2 GB over 20 minutes
            hist.append(PoolSample(t + timedelta(minutes=5 * k), 0.705 + 0.00714 * k, 0.4, size))
        cfg = _cfg()
        rw = compute_runway(hist, hist[-1], cfg)
        assert relief_reason(rw, cfg) is None, rw.describe()

    def test_rate_uses_used_bytes_across_a_pool_resize(self) -> None:
        """An extend drops data% without freeing anything: not a negative rate."""
        hist = [
            PoolSample(T0, 0.90, 0.4, 100 * _GB),
            PoolSample(T0 + timedelta(hours=3), 0.75, 0.4, 120 * _GB),  # +20G extend
            PoolSample(T0 + timedelta(hours=6), 0.75, 0.4, 120 * _GB),
        ]
        rw = compute_runway(hist, hist[-1], _cfg())
        assert rw.rate_bytes_per_h == pytest.approx(0.0, abs=1)
        assert rw.free_bytes == int(0.25 * 120 * _GB)

    def test_burst_sets_reserve(self) -> None:
        size = 100 * _GB
        hist = _series([(0, 50.0), (10, 50.0)], size)
        # one 5-minute jump of 4 GB
        hist.append(PoolSample(hist[-1].ts + timedelta(minutes=5), 0.54, 0.4, size))
        rw = compute_runway(hist, hist[-1], _cfg(burst_multiplier=2.0))
        assert rw.burst_bytes == pytest.approx(4 * _GB, rel=0.01)
        assert rw.reserve_bytes == pytest.approx(8 * _GB, rel=0.01)

    def test_relief_when_free_below_reserve(self) -> None:
        size = 100 * _GB
        hist = [PoolSample(T0, 0.985, 0.3, size)]
        rw = compute_runway(hist, hist[-1], _cfg())
        assert rw.free_bytes < rw.reserve_bytes
        assert "reserve" in (relief_reason(rw, _cfg()) or "")

    def test_relief_when_runway_short(self) -> None:
        size = 100 * _GB
        hist = _series([(0, 80.0), (30, 90.0)], size)  # 8 GB/day, 10 GB free
        cfg = _cfg()
        rw = compute_runway(hist, hist[-1], cfg)
        assert rw.hours_to_full == pytest.approx(30, rel=0.1)
        assert assess_pressure(rw, cfg)[0] == LEVEL_EARLY  # 30h: inside 48, not < 24
        tight = _cfg(early_horizon_hours=12, urgent_horizon_hours=6)
        assert assess_pressure(rw, tight)[0] == LEVEL_NONE
        loose = _cfg(urgent_horizon_hours=36, early_horizon_hours=48)
        assert assess_pressure(rw, loose)[0] == LEVEL_URGENT

    def test_metadata_runway_counts(self) -> None:
        size = 100 * _GB
        hist = _meta_series(0.5, 0.70, 0.90, 24, size)
        rw = compute_runway(hist, hist[-1], _cfg())
        assert rw.meta_hours_to_full == pytest.approx(12, rel=0.05)
        assert rw.hours_to_full == pytest.approx(12, rel=0.05)
        assert "metadata" in (relief_reason(rw, _cfg()) or "")

    def test_no_size_means_no_byte_maths(self) -> None:
        hist = [PoolSample(T0, 0.99, None, None)]
        rw = compute_runway(hist, hist[-1], _cfg())
        assert rw.free_bytes is None and rw.reserve_bytes is None
        assert relief_reason(rw, _cfg()) is None


# --- acceptance bar: the incident's real shape ---------------------------------

# (hours from the last successful rotation, data %) — the measured series, as
# logged by the guardian's own tier alerts and snapshot refusals over the week
# the lifeline was never rotated, on a ~64.9 GiB pool.
_INCIDENT = [
    (0.0, 75.0),
    (16.4, 81.0),
    (27.3, 85.0),
    (33.5, 86.0),
    (39.5, 87.0),
    (48.0, 88.4),
    (51.5, 90.0),
    (53.7, 92.0),
    (72.0, 92.5),
    (89.0, 95.0),
    (96.0, 94.4),
    (101.0, 96.0),
    (120.0, 96.1),
    (125.0, 97.0),
    (144.0, 97.6),
    (149.0, 98.0),
    (155.0, 98.5),
    (160.6, 100.0),
]
_INCIDENT_SIZE = int(64.9 * _GB)


def test_incident_replay_relief_fires_with_a_day_to_spare() -> None:
    samples = _series(_INCIDENT, _INCIDENT_SIZE)
    cfg = _cfg()
    first_fire: datetime | None = None
    for i in range(1, len(samples)):
        rw = compute_runway(samples[:i], samples[i], cfg)
        if relief_reason(rw, cfg):
            first_fire = samples[i].ts
            break
    assert first_fire is not None, "relief never fired on the incident series"
    full_at = T0 + timedelta(hours=160.6)
    spare_h = (full_at - first_fire).total_seconds() / 3600
    assert spare_h >= 24, f"relief fired only {spare_h:.1f}h before the pool filled"


def test_burst_older_than_a_day_no_longer_sizes_the_reserve() -> None:
    """One restore-sized burst must not pin the reserve (and relief) for a week."""
    size = 100 * _GB
    hist = _series([(0, 50.0), (1, 50.0)], size)
    hist.append(PoolSample(hist[-1].ts + timedelta(minutes=5), 0.60, 0.4, size))  # +10G
    hist += _series([(1.1, 60.0), (40, 60.0)], size)
    rw = compute_runway(hist, hist[-1], _cfg())
    assert rw.reserve_bytes == int(0.03 * size)


# --- plan_delete ------------------------------------------------------------


def _snap(name: str, hours_old: float | None, healthy: bool) -> SnapshotInfo:
    created = None if hours_old is None else T0 - timedelta(hours=hours_old)
    return SnapshotInfo(name, created, healthy)


class TestPlanDelete:
    def test_nothing_without_pressure(self) -> None:
        snaps = [_snap("guardian-a-pre-recovery", 100, False)]
        assert plan_delete(snaps, LEVEL_NONE, T0, 48) is None

    def test_pre_recovery_first_oldest_first(self) -> None:
        snaps = [
            _snap("guardian-new-healthy", 100, True),
            _snap("guardian-b-pre-recovery", 1, False),
            _snap("guardian-a-pre-recovery", 5, False),
        ]
        assert plan_delete(snaps, LEVEL_EARLY, T0, 48) == "guardian-a-pre-recovery"

    def test_early_keeps_a_fresh_lifeline(self) -> None:
        snaps = [_snap("guardian-x-healthy", 20, True)]
        assert plan_delete(snaps, LEVEL_EARLY, T0, 48) is None

    def test_early_takes_a_lifeline_past_the_cap(self) -> None:
        snaps = [_snap("guardian-x-healthy", 49, True)]
        assert plan_delete(snaps, LEVEL_EARLY, T0, 48) == "guardian-x-healthy"

    def test_early_never_takes_an_unknown_age_lifeline(self) -> None:
        snaps = [_snap("guardian-x-healthy", None, True)]
        assert plan_delete(snaps, LEVEL_EARLY, T0, 48) is None
        assert plan_delete(snaps, LEVEL_URGENT, T0, 48) == "guardian-x-healthy"

    def test_cap_disabled_keeps_lifeline_early_only(self) -> None:
        snaps = [_snap("guardian-x-healthy", 500, True)]
        assert plan_delete(snaps, LEVEL_EARLY, T0, 0) is None
        assert plan_delete(snaps, LEVEL_URGENT, T0, 0) == "guardian-x-healthy"

    def test_superseded_healthy_before_lifeline(self) -> None:
        snaps = [
            _snap("guardian-new-healthy", 2, True),
            _snap("guardian-old-healthy", 30, True),
        ]
        assert plan_delete(snaps, LEVEL_EARLY, T0, 48) == "guardian-old-healthy"

    def test_urgent_takes_lifeline_last(self) -> None:
        snaps = [_snap("guardian-x-healthy", 1, True)]
        assert plan_delete(snaps, LEVEL_URGENT, T0, 48) == "guardian-x-healthy"
        assert plan_delete([], LEVEL_URGENT, T0, 48) is None


# --- plan_extend ------------------------------------------------------------


def _lvm(**kw) -> StoragePoolStatus:
    base = dict(
        detected=True,
        data_pct=85.0,
        metadata_pct=40.0,
        vg_free_bytes=4 * _GB,
        pool_size_bytes=65 * _GB,
        metadata_size_bytes=84 * 1024**2,
        vg_name="vg0",
        thinpool_lv="IncusThinPool",
        thinpool_profile="genesis-thinpool",
    )
    base.update(kw)
    return StoragePoolStatus(**base)


class TestPlanExtend:
    def test_the_incident_shape_extends_into_the_stranded_space(self) -> None:
        grow = plan_extend(_lvm(), LEVEL_EARLY, _cfg())
        assert grow == 4 * _GB - 512 * 1024**2

    @pytest.mark.parametrize(
        "kw",
        [
            {"thinpool_profile": None},  # no opt-in
            {"thinpool_profile": "other"},
            {"data_pct": 79.0},  # autoextend not due yet
            {"vg_free_bytes": 14 * _GB},  # >= one 20% step: dmeventd's job
            {"vg_free_bytes": 1 * _GB},  # too small to bother after keep
            {"vg_free_bytes": None},
            {"thinpool_lv": None},  # ambiguous / unnamed pool
            {"pool_size_bytes": None},
        ],
    )
    def test_refuses(self, kw) -> None:
        assert plan_extend(_lvm(**kw), LEVEL_URGENT, _cfg()) is None

    def test_no_pressure_no_extend(self) -> None:
        assert plan_extend(_lvm(), LEVEL_NONE, _cfg()) is None

    def test_keeps_twice_metadata_when_larger(self) -> None:
        grow = plan_extend(_lvm(metadata_size_bytes=1 * _GB), LEVEL_EARLY, _cfg())
        assert grow == 4 * _GB - 2 * _GB


def test_snapshot_metadata_step_on_a_fresh_history_is_not_a_rate() -> None:
    """A fresh thin snapshot steps metadata up ~4.4 points within ~30 minutes
    (measured live). On a history that short that step must not read as a
    runway — it once would have deleted the brand-new lifeline."""
    size = 70 * _GB
    hist = [
        PoolSample(T0, 0.70, 0.327, size),
        PoolSample(T0 + timedelta(minutes=30), 0.70, 0.371, size),
        PoolSample(T0 + timedelta(minutes=90), 0.70, 0.374, size),
    ]
    cfg = _cfg()
    rw = compute_runway(hist, hist[-1], cfg)
    assert rw.meta_hours_to_full is None
    assert assess_pressure(rw, cfg)[0] == LEVEL_NONE


def test_snapshot_metadata_step_inside_a_long_history_is_averaged() -> None:
    size = 70 * _GB
    hist = _series([(0, 70.0), (24, 70.0)], size)  # meta 0.4 flat
    t = hist[-1].ts
    hist += [
        PoolSample(t + timedelta(minutes=30), 0.70, 0.444, size),
        PoolSample(t + timedelta(minutes=90), 0.70, 0.447, size),
    ]
    cfg = _cfg()
    rw = compute_runway(hist, hist[-1], cfg)
    assert assess_pressure(rw, cfg)[0] == LEVEL_NONE, rw.describe()


# --- review fixes -------------------------------------------------------------


def test_yaml_off_is_off_not_alert_only() -> None:
    """YAML 1.1 reads a bare `off` as False; the documented lever must work."""
    import yaml

    raw = yaml.safe_load("relief_mode: off")["relief_mode"]
    assert raw is False
    assert effective_relief_mode(_cfg(relief_mode=raw)) == "off"
    assert effective_relief_mode(_cfg(relief_mode="bogus")) == "alert_only"


def test_kill_switch_forces_alert_only(monkeypatch) -> None:
    monkeypatch.setenv("GUARDIAN_POOL_RELIEF_DISABLED", "1")
    assert effective_relief_mode(_cfg()) == "alert_only"


def test_metadata_only_pressure_is_not_data_pressure() -> None:
    size = 100 * _GB
    hist = _meta_series(0.85, 0.70, 0.90, 24, size)
    cfg = _cfg()
    rw = compute_runway(hist, hist[-1], cfg)
    assert assess_pressure(rw, cfg)[0] == LEVEL_URGENT
    assert data_pressed(rw, cfg) is False


class _Snaps:
    def __init__(self, names):
        self.names = dict(names)
        self.deleted: list[str] = []

    async def list_snapshot_meta_strict(self):
        return sorted(self.names.items(), reverse=True)

    async def delete(self, name):
        self.deleted.append(name)
        self.names.pop(name, None)
        return True


def _pass_cfg(tmp_path):
    from genesis.guardian.config import GuardianConfig

    c = GuardianConfig()
    c.state_dir = str(tmp_path)
    return c


async def _run_pass(cfg, snaps, status, now):
    from unittest.mock import AsyncMock, patch

    d = AsyncMock()

    async def measure(_c):
        return status

    with patch("genesis.guardian.pool.measure_storage_pool", measure):
        out = await check_pool_pressure(cfg, d, snaps, now=now, run=AsyncMock())
    return out, d


@pytest.mark.asyncio
async def test_relief_settles_between_deletes(tmp_path) -> None:
    """One delete, then hold for a history interval before the next — btrfs
    frees asynchronously and an immediate re-measure reads 'nothing changed'."""
    cfg = _pass_cfg(tmp_path)
    old = T0 - timedelta(days=3)
    snaps = _Snaps({"guardian-a-pre-recovery": old, "guardian-b-pre-recovery": old})
    full = StoragePoolStatus(
        detected=True, pool_used_pct=99.0, pool_size_bytes=100 * _GB, pool_name="default"
    )
    out1, _ = await _run_pass(cfg, snaps, full, T0)
    out2, _ = await _run_pass(cfg, snaps, full, T0 + timedelta(seconds=30))
    out3, _ = await _run_pass(cfg, snaps, full, T0 + timedelta(seconds=330))
    assert out1.startswith("deleted:")
    assert out2.startswith("settling:")
    assert out3.startswith("deleted:")
    assert len(snaps.deleted) == 2


@pytest.mark.asyncio
async def test_ambiguous_thin_pool_is_never_acted_on(tmp_path) -> None:
    cfg = _pass_cfg(tmp_path)
    snaps = _Snaps({"guardian-a-pre-recovery": T0 - timedelta(days=3)})
    st = StoragePoolStatus(
        detected=True,
        data_pct=99.0,
        metadata_pct=50.0,
        vg_name="vg0",
        thinpool_lv=None,
    )
    out, d = await _run_pass(cfg, snaps, st, T0)
    assert out == "ambiguous_pool"
    assert snaps.deleted == []


@pytest.mark.asyncio
async def test_off_mode_never_measures_or_acts(tmp_path) -> None:
    cfg = _pass_cfg(tmp_path)
    cfg.storage_pool.relief_mode = False  # what `relief_mode: off` loads as
    snaps = _Snaps({"guardian-a-pre-recovery": T0 - timedelta(days=3)})
    full = StoragePoolStatus(
        detected=True, pool_used_pct=99.0, pool_size_bytes=100 * _GB, pool_name="default"
    )
    out, d = await _run_pass(cfg, snaps, full, T0)
    assert out == "off"
    assert snaps.deleted == []
    d.send.assert_not_called()


async def _two_pass_extend_probe(tmp_path, samples):
    """Run two relief passes over (hours, data%, meta%) samples on an
    extend-eligible LVM pool; return (last outcome, lvextend attempted?)."""
    from unittest.mock import AsyncMock, patch

    cfg = _pass_cfg(tmp_path)
    snaps = _Snaps({"guardian-a-pre-recovery": T0 - timedelta(days=3)})
    base = dict(
        pool_name="default",
        detected=True,
        vg_free_bytes=4 * _GB,
        pool_size_bytes=65 * _GB,
        metadata_size_bytes=84 * 1024**2,
        vg_name="vg0",
        thinpool_lv="IncusThinPool",
        thinpool_profile="genesis-thinpool",
    )

    async def run(*argv, **kw):
        if "vg_extent_size" in argv:
            return 0, "4194304\n", ""
        return 0, "IncusThinPool\n", ""

    run = AsyncMock(side_effect=run)
    outs: list[str] = []
    for hours, data, meta in samples:
        st = StoragePoolStatus(data_pct=data, metadata_pct=meta, **base)

        async def measure(_c, st=st):
            return st

        with (
            patch("genesis.guardian.pool.measure_storage_pool", measure),
            patch("genesis.guardian.pool._detect_pool_name", AsyncMock(return_value="default")),
        ):
            out = await check_pool_pressure(
                cfg,
                AsyncMock(),
                snaps,
                now=T0 + timedelta(hours=hours),
                run=run,
            )
            outs.append(out)
    extended = any("lvextend" in c.args for c in run.await_args_list)
    return outs, extended


@pytest.mark.asyncio
async def test_data_pressure_does_extend(tmp_path) -> None:
    """Positive arm: proves the probe below CAN see an extend."""
    outs, extended = await _two_pass_extend_probe(
        tmp_path,
        [(h, 85.0 + h, 40.0) for h in range(11)],  # hourly, +1 point/h
    )
    assert "extended" in outs and extended, outs


@pytest.mark.asyncio
async def test_metadata_only_pressure_never_extends_the_data_lv(tmp_path) -> None:
    outs, extended = await _two_pass_extend_probe(
        tmp_path,
        [(h, 85.0, 70.0 + 20.0 * h / 24) for h in range(25)],  # metadata only
    )
    assert any(o.startswith("deleted:") for o in outs), outs  # pressure really acted
    assert not extended


@pytest.mark.asyncio
async def test_current_pressure_never_raises_on_bad_config(tmp_path) -> None:
    from unittest.mock import AsyncMock, patch

    from genesis.guardian.pool_pressure import current_pressure

    cfg = _pass_cfg(tmp_path)
    cfg.storage_pool.min_reserve_pct = "3%"  # _build_sub does not coerce types
    st = StoragePoolStatus(detected=True, pool_used_pct=99.0, pool_size_bytes=100 * _GB)
    with patch("genesis.guardian.pool.measure_storage_pool", AsyncMock(return_value=st)):
        assert await current_pressure(cfg) == LEVEL_NONE
    # guard-the-guard: the same config really does raise in the maths
    with pytest.raises(TypeError):
        compute_runway([], sample_from_status(st, T0), cfg.storage_pool)


def test_state_files_are_private(tmp_path) -> None:
    import stat

    p = tmp_path / "h.jsonl"
    record_sample(p, PoolSample(T0, 0.5, None, _GB), min_interval_s=300, max_samples=10)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


@pytest.mark.parametrize("mode", ["alert_only", False])
@pytest.mark.asyncio
async def test_brake_modes_forbid_delete_first(tmp_path, mode, monkeypatch) -> None:
    """The kill switch / alert_only must stop delete-first rotation too."""
    from unittest.mock import AsyncMock, patch

    from genesis.guardian.pool_pressure import current_pressure

    cfg = _pass_cfg(tmp_path)
    cfg.storage_pool.relief_mode = mode
    st = StoragePoolStatus(detected=True, pool_used_pct=99.0, pool_size_bytes=100 * _GB)
    with patch("genesis.guardian.pool.measure_storage_pool", AsyncMock(return_value=st)):
        assert await current_pressure(cfg) == LEVEL_NONE
        cfg.storage_pool.relief_mode = "live"
        assert await current_pressure(cfg) == LEVEL_URGENT  # control arm
        monkeypatch.setenv("GUARDIAN_POOL_RELIEF_DISABLED", "1")
        assert await current_pressure(cfg) == LEVEL_NONE


@pytest.mark.asyncio
async def test_current_pressure_refuses_an_ambiguous_pool(tmp_path) -> None:
    from unittest.mock import AsyncMock, patch

    from genesis.guardian.pool_pressure import current_pressure

    cfg = _pass_cfg(tmp_path)
    st = StoragePoolStatus(
        detected=True,
        data_pct=99.0,
        metadata_pct=50.0,
        vg_name="vg0",
        thinpool_lv=None,
        pool_size_bytes=100 * _GB,  # sized, so ONLY the guard can answer NONE
    )
    with patch("genesis.guardian.pool.measure_storage_pool", AsyncMock(return_value=st)):
        assert await current_pressure(cfg) == LEVEL_NONE


@pytest.mark.parametrize("baseline", [0.60, 0.80])
def test_fresh_snapshot_metadata_step_never_reads_as_growth(baseline) -> None:
    """Devin's example: history starts, a fresh snapshot steps metadata ~4.4
    points within minutes, then it stays flat. At 2h, 6h and 20h the step must
    not become a rate — at a warning AND a critical metadata baseline."""
    size = 70 * _GB
    hist = [PoolSample(T0, 0.70, baseline, size)]
    for minutes in range(6, 21 * 60, 5):
        hist.append(PoolSample(T0 + timedelta(minutes=minutes), 0.70, baseline + 0.044, size))
    cfg = _cfg()
    for hours in (2, 6, 20):
        cur = [s for s in hist if s.ts <= T0 + timedelta(hours=hours)]
        rw = compute_runway(cur, cur[-1], cfg)
        assert assess_pressure(rw, cfg)[0] == LEVEL_NONE, (hours, rw.describe())


def test_pool_migration_does_not_compare_two_pools() -> None:
    """Devin's example: history from a 50 GiB pool using 25 GiB, then the
    container moves to a 100 GiB pool steadily using 90 GiB. The old pool's
    samples must not read as a 65 GiB burst."""
    old = [
        PoolSample(T0 + timedelta(minutes=5 * i), 0.50, 0.3, 50 * _GB, "a|vg0|tp")
        for i in range(80)
    ]
    t = old[-1].ts
    new = [
        PoolSample(t + timedelta(minutes=5 * i), 0.90, 0.3, 100 * _GB, "b|vg1|tp")
        for i in range(1, 4)
    ]
    cfg = _cfg()
    rw = compute_runway(old + new, new[-1], cfg)
    assert assess_pressure(rw, cfg)[0] == LEVEL_NONE, rw.describe()
    # control arm: without pool identity the same history DOES read as urgent
    anon = [PoolSample(s.ts, s.data_frac, s.meta_frac, s.size_bytes) for s in old + new]
    rw2 = compute_runway(anon, anon[-1], cfg)
    assert assess_pressure(rw2, cfg)[0] == LEVEL_URGENT


def test_pool_key_round_trips_through_history(tmp_path) -> None:
    p = tmp_path / "h.jsonl"
    record_sample(
        p,
        PoolSample(T0, 0.5, None, _GB, "default|vg0|IncusThinPool"),
        min_interval_s=300,
        max_samples=10,
    )
    assert load_history(p)[0].pool == "default|vg0|IncusThinPool"


# --- review round 1 (Codex / Devin at 9c24658) --------------------------------


def test_high_baseline_snapshot_step_is_not_a_rate() -> None:
    """Devin: at 92% metadata a fresh snapshot steps it to 96%, then flat. The
    step must not become a phantom RATE at hour 20 or 24. (At 96% the pool is
    genuinely short of metadata, so the RESERVE rule does fire — by design.)"""
    size = 70 * _GB
    hist = [PoolSample(T0, 0.70, 0.92, size)]
    hist += [PoolSample(T0 + timedelta(minutes=m), 0.70, 0.96, size) for m in range(6, 25 * 60, 5)]
    cfg = _cfg()
    for hours in (20, 24):
        cur = [s for s in hist if s.ts <= T0 + timedelta(hours=hours)]
        rw = compute_runway(cur, cur[-1], cfg)
        assert rw.meta_hours_to_full is None, (hours, rw.describe())
        level, reason = assess_pressure(rw, cfg)
        assert level == LEVEL_URGENT and "reserve" in reason


def test_sub_day_metadata_fill_is_caught() -> None:
    """Codex: metadata 80% -> 95% over ten hours must be seen well before full."""
    size = 70 * _GB
    hist = _meta_series(0.70, 0.80, 0.95, 10, size)
    cfg = _cfg()
    rw = compute_runway(hist, hist[-1], cfg)
    assert rw.meta_hours_to_full == pytest.approx(3.3, rel=0.1)
    assert assess_pressure(rw, cfg)[0] == LEVEL_URGENT


def test_fresh_pool_metadata_fill_is_caught_early() -> None:
    """Devin: a fresh pool at 70% metadata heading to 98% in ten hours is
    caught by the rate within the first hours, not only at the reserve."""
    size = 70 * _GB
    hist = _meta_series(0.70, 0.70, 0.98, 10, size)
    cfg = _cfg()
    fired_at = None
    for h in range(2, 11):
        rw = compute_runway(hist[: h + 1], hist[h], cfg)
        if assess_pressure(rw, cfg)[0] != LEVEL_NONE:
            fired_at = h
            break
    assert fired_at is not None and fired_at <= 3, fired_at


def test_fill_across_a_delayed_tick_is_a_burst() -> None:
    """Codex: ticks an hour apart (an outage's diagnosis). 100 GiB rising from
    80% to 96% in one hour must read as pressure, not be discarded."""
    size = 100 * _GB
    hist = [
        PoolSample(T0, 0.80, 0.4, size),
        PoolSample(T0 + timedelta(hours=1), 0.96, 0.4, size),
    ]
    cfg = _cfg()
    rw = compute_runway(hist, hist[-1], cfg)
    assert rw.burst_bytes == pytest.approx(16 * _GB, rel=0.01)
    assert assess_pressure(rw, cfg)[0] == LEVEL_URGENT


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("storage_pool", "min_reserve_pct", 300),
        ("storage_pool", "min_reserve_pct", "3%"),
        ("storage_pool", "burst_multiplier", 0),
        ("storage_pool", "early_horizon_hours", "48h"),
        ("storage_pool", "urgent_horizon_hours", 100),  # > early
        ("snapshots", "prefix", ""),
        ("snapshots", "prefix", "   "),
    ],
)
@pytest.mark.asyncio
async def test_invalid_config_never_acts(tmp_path, section, key, value) -> None:
    """Codex: config values steer automatic deletes; invalid ones must degrade
    to alert-only, never to acting on a guess."""
    cfg = _pass_cfg(tmp_path)
    setattr(getattr(cfg, section), key, value)
    snaps = _Snaps({"guardian-20260101-000000-pre-recovery": T0 - timedelta(days=3)})
    full = StoragePoolStatus(
        detected=True, pool_used_pct=99.0, pool_size_bytes=100 * _GB, pool_name="default"
    )
    out, d = await _run_pass(cfg, snaps, full, T0)
    assert out == "invalid_config"
    assert snaps.deleted == []
    assert "not live" in d.send.await_args.args[0].title


@pytest.mark.asyncio
async def test_unwritable_state_stops_the_delete(tmp_path) -> None:
    """Codex: if the settle stamp cannot persist, the delete must not happen —
    the next tick would otherwise delete again at once."""
    from unittest.mock import patch

    cfg = _pass_cfg(tmp_path)
    snaps = _Snaps({"guardian-20260101-000000-pre-recovery": T0 - timedelta(days=3)})
    full = StoragePoolStatus(
        detected=True, pool_used_pct=99.0, pool_size_bytes=100 * _GB, pool_name="default"
    )
    with patch("genesis.guardian.pool_pressure._save_state", return_value=False):
        out, _ = await _run_pass(cfg, snaps, full, T0)
    assert out == "state_unwritable"
    assert snaps.deleted == []


@pytest.mark.asyncio
async def test_pool_change_before_the_act_stops_it(tmp_path) -> None:
    """Codex: the pool is re-read right before mutating; a different pool
    than the one measured means no action."""
    from unittest.mock import AsyncMock, patch

    cfg = _pass_cfg(tmp_path)
    snaps = _Snaps({"guardian-20260101-000000-pre-recovery": T0 - timedelta(days=3)})
    first = StoragePoolStatus(
        detected=True, pool_used_pct=99.0, pool_size_bytes=100 * _GB, pool_name="default"
    )
    moved = StoragePoolStatus(
        detected=True, pool_used_pct=50.0, pool_size_bytes=100 * _GB, pool_name="other"
    )
    measure = AsyncMock(side_effect=[first, moved])
    with patch("genesis.guardian.pool.measure_storage_pool", measure):
        out = await check_pool_pressure(cfg, AsyncMock(), snaps, now=T0, run=AsyncMock())
    assert out == "pool_changed"
    assert snaps.deleted == []


@pytest.mark.asyncio
async def test_extend_probe_failure_does_not_burn_the_daily_cooldown(tmp_path) -> None:
    """Codex: a failed extent-size read happens BEFORE any mutation; it must
    not stamp the 24h extend cooldown."""
    import json as _json
    from unittest.mock import AsyncMock, patch

    cfg = _pass_cfg(tmp_path)
    snaps = _Snaps({})
    st = StoragePoolStatus(
        detected=True,
        data_pct=98.0,
        metadata_pct=40.0,
        vg_free_bytes=4 * _GB,
        pool_size_bytes=65 * _GB,
        metadata_size_bytes=84 * 1024**2,
        vg_name="vg0",
        thinpool_lv="IncusThinPool",
        thinpool_profile="genesis-thinpool",
        pool_name="default",
    )

    async def run(*argv, **kw):
        if "vg_extent_size" in argv:
            return 5, "", "vgs: transient failure"
        return 0, "IncusThinPool\n", ""

    with (
        patch("genesis.guardian.pool.measure_storage_pool", AsyncMock(return_value=st)),
        patch("genesis.guardian.pool._detect_pool_name", AsyncMock(return_value="default")),
    ):
        runner = AsyncMock(side_effect=run)
        await check_pool_pressure(cfg, AsyncMock(), snaps, now=T0, run=runner)
    # guard-the-guard: the extend path really was reached and probed
    assert any("vg_extent_size" in c.args for c in runner.await_args_list)
    state_file = tmp_path / "pool_relief_state.json"
    state = _json.loads(state_file.read_text()) if state_file.exists() else {}
    assert "extend" not in state


@pytest.mark.asyncio
async def test_strict_list_refuses_an_empty_prefix(tmp_path) -> None:
    import json as _json
    from unittest.mock import patch

    from genesis.guardian.snapshots import SnapshotManager

    cfg = _pass_cfg(tmp_path)
    cfg.snapshots.prefix = ""
    rows = [{"name": "20260101-000000-mine"}, {"name": "20260102-000000"}]

    async def listing(*a, **k):  # a listing that WOULD match an empty prefix
        return 0, _json.dumps(rows), ""

    with patch("genesis.guardian.snapshots._run_subprocess", listing):
        assert await SnapshotManager(cfg).list_snapshot_meta_strict() is None


# --- internal review of the round-1 delta ------------------------------------


def test_daily_recurring_step_reads_as_its_average() -> None:
    """A job writing 5 GB once a day is the commonest real fill; each step
    lands in one half of every <=24h window, so only the 72h window sees it.
    It must read as a rate (not 0), so the EARLY stage can act in time."""
    size = 100 * _GB
    used = 40.0
    hist = []
    for h in range(0, 24 * 7):
        if h % 24 == 2 and h > 0:
            used += 5.0
        for m in (0, 30):
            hist.append(PoolSample(T0 + timedelta(hours=h, minutes=m), used / 100, 0.4, size))
    cfg = _cfg()
    rw = compute_runway(hist, hist[-1], cfg)
    assert rw.rate_bytes_per_h == pytest.approx(5 * _GB / 24, rel=0.35), rw.describe()


def test_long_sample_gap_does_not_inflate_the_reserve() -> None:
    """Steady growth across a 20h gap is not a burst (the rate covers it)."""
    size = 100 * _GB
    hist = [
        PoolSample(T0, 0.50, 0.4, size),
        PoolSample(T0 + timedelta(minutes=5), 0.50, 0.4, size),
        PoolSample(T0 + timedelta(hours=20), 0.60, 0.4, size),
    ]
    rw = compute_runway(hist, hist[-1], _cfg())
    assert rw.burst_bytes == pytest.approx(0.0, abs=1)
    assert rw.reserve_bytes == int(0.03 * size)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("snapshots", "lifeline_max_age_hours", float("nan")),
        ("snapshots", "lifeline_max_age_hours", float("inf")),
        ("storage_pool", "history_max_samples", 100.5),
        ("storage_pool", "history_sample_interval_s", 300.0),
    ],
)
def test_validation_rejects_non_finite_and_non_integer(tmp_path, section, key, value) -> None:
    from genesis.guardian.pool_pressure import validate_relief_config

    cfg = _pass_cfg(tmp_path)
    setattr(getattr(cfg, section), key, value)
    assert validate_relief_config(cfg) is not None
    assert validate_relief_config(_pass_cfg(tmp_path)) is None  # control: defaults valid


def _extend_eligible(pool_name="default"):
    return StoragePoolStatus(
        detected=True,
        data_pct=98.0,
        metadata_pct=40.0,
        vg_free_bytes=4 * _GB,
        pool_size_bytes=65 * _GB,
        metadata_size_bytes=84 * 1024**2,
        vg_name="vg0",
        thinpool_lv="IncusThinPool",
        thinpool_profile="genesis-thinpool",
        pool_name=pool_name,
    )


async def _extend_run(*argv, **kw):
    if "vg_extent_size" in argv:
        return 0, "4194304\n", ""
    return 0, "IncusThinPool\n", ""


@pytest.mark.asyncio
async def test_lvextend_never_runs_without_a_persisted_stamp(tmp_path) -> None:
    from unittest.mock import AsyncMock, patch

    cfg = _pass_cfg(tmp_path)
    runner = AsyncMock(side_effect=_extend_run)
    d = AsyncMock()
    with (
        patch(
            "genesis.guardian.pool.measure_storage_pool", AsyncMock(return_value=_extend_eligible())
        ),
        patch("genesis.guardian.pool._detect_pool_name", AsyncMock(return_value="default")),
        patch("genesis.guardian.pool_pressure._save_state", return_value=False),
    ):
        outs = [
            await check_pool_pressure(
                cfg,
                d,
                _Snaps({}),
                now=T0 + timedelta(seconds=30 * i),
                run=runner,
            )
            for i in range(5)
        ]
    assert any("vg_extent_size" in c.args for c in runner.await_args_list)  # reached
    assert not any("lvextend" in c.args for c in runner.await_args_list)
    assert set(outs) == {"extend_stopped"}
    d.send.assert_not_called()  # no per-tick alert storm when state is unwritable


@pytest.mark.asyncio
async def test_lvextend_never_runs_on_a_pool_that_changed(tmp_path) -> None:
    from unittest.mock import AsyncMock, patch

    cfg = _pass_cfg(tmp_path)
    runner = AsyncMock(side_effect=_extend_run)
    measure = AsyncMock(side_effect=[_extend_eligible(), _extend_eligible("other")])
    with (
        patch("genesis.guardian.pool.measure_storage_pool", measure),
        patch("genesis.guardian.pool._detect_pool_name", AsyncMock(return_value="default")),
    ):
        out = await check_pool_pressure(cfg, AsyncMock(), _Snaps({}), now=T0, run=runner)
    assert out == "extend_stopped"
    assert not any("lvextend" in c.args for c in runner.await_args_list)


@pytest.mark.asyncio
async def test_lvextend_positive_control(tmp_path) -> None:
    """Proves the two tests above can SEE an lvextend when nothing stops it."""
    from unittest.mock import AsyncMock, patch

    cfg = _pass_cfg(tmp_path)
    runner = AsyncMock(side_effect=_extend_run)
    with (
        patch(
            "genesis.guardian.pool.measure_storage_pool", AsyncMock(return_value=_extend_eligible())
        ),
        patch("genesis.guardian.pool._detect_pool_name", AsyncMock(return_value="default")),
    ):
        out = await check_pool_pressure(cfg, AsyncMock(), _Snaps({}), now=T0, run=runner)
    assert out == "extended"
    assert any("lvextend" in c.args for c in runner.await_args_list)


@pytest.mark.asyncio
async def test_delete_first_waits_out_relief_settle(tmp_path) -> None:
    """Relief acted this tick: delete-first must not stack a second delete."""
    import json as _json
    from datetime import datetime as _dt
    from unittest.mock import AsyncMock, patch

    from genesis.guardian.pool_pressure import current_pressure

    cfg = _pass_cfg(tmp_path)
    st = StoragePoolStatus(
        detected=True, pool_used_pct=99.0, pool_size_bytes=100 * _GB, pool_name="default"
    )
    with patch("genesis.guardian.pool.measure_storage_pool", AsyncMock(return_value=st)):
        assert await current_pressure(cfg) == LEVEL_URGENT  # control
        (tmp_path / "pool_relief_state.json").write_text(
            _json.dumps({"last_action": _dt.now(UTC).isoformat()})
        )
        assert await current_pressure(cfg) == LEVEL_NONE


@pytest.mark.asyncio
async def test_unwritable_state_never_storms_alerts(tmp_path) -> None:
    """Urgent pressure with nothing left to free alerts once per realert window
    — and not at all when its throttle stamp cannot persist (else every 30s)."""
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, patch

    cfg = _pass_cfg(tmp_path)
    full = StoragePoolStatus(
        detected=True, pool_used_pct=99.0, pool_size_bytes=100 * _GB, pool_name="default"
    )
    for writable, expected in ((True, 1), (False, 0)):
        d = AsyncMock()
        (tmp_path / "pool_relief_state.json").unlink(missing_ok=True)
        with ExitStack() as stack:
            stack.enter_context(
                patch("genesis.guardian.pool.measure_storage_pool", AsyncMock(return_value=full))
            )
            if not writable:
                stack.enter_context(
                    patch("genesis.guardian.pool_pressure._save_state", return_value=False)
                )
            for i in range(5):
                out = await check_pool_pressure(
                    cfg, d, _Snaps({}), now=T0 + timedelta(seconds=30 * i), run=AsyncMock()
                )
                assert out == "no_target:urgent"
        assert d.send.await_count == expected, (writable, d.send.await_count)
