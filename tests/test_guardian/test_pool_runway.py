"""Tests for the measured pool runway (pool_runway.py): history, growth rate,
hours-to-full, and the LVM partial extend's planner/executor (pool_extend.py).

Several cases replay review findings from the first rate-model design by their
own numbers; each says which.
"""

from __future__ import annotations

import json
import os
import random
from datetime import UTC, datetime, timedelta

import pytest

from genesis.guardian.pool import StoragePoolStatus
from genesis.guardian.pool_extend import GUARD_STOP, TIMED_OUT, extend_thinpool, plan_extend
from genesis.guardian.pool_runway import (
    PoolSample,
    compute_runway,
    early_reason,
    gap_threshold,
    growth_rate,
    load_history,
    record_sample,
    sample_from_status,
)

T0 = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
_GB = 1024**3
_MB = 1024**2
POOL = "default|vg0|IncusThinPool"


def _s(
    minutes: float,
    data_frac: float | None,
    meta_frac: float | None = None,
    *,
    size: int = 100 * _GB,
    meta_size: int = 80 * _MB,
    pool: str = POOL,
) -> PoolSample:
    return PoolSample(
        T0 + timedelta(minutes=minutes),
        pool,
        data_frac * size if data_frac is not None else None,
        size,
        meta_frac * meta_size if meta_frac is not None else None,
        meta_size,
    )


def _series(hours: float, frac_at, *, every_min: float = 5.0, jitter_s: float = 0.0, **kw):
    """Samples from T0 over ``hours``; ``frac_at(h)`` gives (data, meta)."""
    rng = random.Random(7)
    out, t = [], 0.0
    while t <= hours * 60:
        d, m = frac_at(t / 60.0)
        out.append(_s(t, d, m, **kw))
        t += every_min + (rng.uniform(0, jitter_s) / 60.0 if jitter_s else 0.0)
    return out


def _runway(samples):
    return compute_runway(samples[:-1], samples[-1])


# --- sampling -----------------------------------------------------------------


class TestSampleFromStatus:
    def test_lvm_records_bytes_for_data_and_metadata(self) -> None:
        st = StoragePoolStatus(
            detected=True,
            data_pct=50.0,
            metadata_pct=25.0,
            pool_size_bytes=100 * _GB,
            metadata_size_bytes=80 * _MB,
        )
        s = sample_from_status(st, T0, POOL)
        assert s.data_used == pytest.approx(50 * _GB)
        assert s.meta_used == pytest.approx(20 * _MB)

    def test_non_lvm_uses_pool_used(self) -> None:
        st = StoragePoolStatus(detected=True, pool_used_pct=40.0, pool_size_bytes=10 * _GB)
        s = sample_from_status(st, T0, "default||")
        assert s.data_used == pytest.approx(4 * _GB)
        assert s.meta_used is None

    @pytest.mark.parametrize(
        "st,pool",
        [
            (StoragePoolStatus(detected=False), POOL),
            (StoragePoolStatus(detected=True, data_pct=50.0, pool_size_bytes=10 * _GB), None),
            # No byte sizes: nothing to rate.
            (StoragePoolStatus(detected=True, data_pct=50.0, metadata_pct=20.0), POOL),
        ],
    )
    def test_nothing_to_record(self, st, pool) -> None:
        assert sample_from_status(st, T0, pool) is None


class TestHistory:
    def test_missing_file_is_empty(self, tmp_path) -> None:
        assert load_history(tmp_path / "h.jsonl") == []

    def test_interval_roundtrip_and_private(self, tmp_path) -> None:
        p = tmp_path / "h.jsonl"
        record_sample(p, _s(0, 0.5, 0.2), min_interval_s=300, max_samples=100)
        record_sample(p, _s(1, 0.6), min_interval_s=300, max_samples=100)  # too soon
        h = record_sample(p, _s(6, 0.7, 0.3), min_interval_s=300, max_samples=100)
        assert [round(s.data_used / _GB) for s in load_history(p)] == [50, 70]
        assert h == load_history(p)
        assert os.stat(p).st_mode & 0o777 == 0o600

    def test_bounded(self, tmp_path) -> None:
        p = tmp_path / "h.jsonl"
        for i in range(40):
            record_sample(p, _s(i * 10, 0.5), min_interval_s=60, max_samples=20)
        h = load_history(p)
        assert 20 <= len(h) <= 22
        assert h[-1].ts == T0 + timedelta(minutes=390)
        assert os.stat(p).st_mode & 0o777 == 0o600

    def test_corrupt_lines_skipped(self, tmp_path) -> None:
        p = tmp_path / "h.jsonl"
        good = json.dumps({"ts": T0.isoformat(), "pool": POOL, "data_used": 1.0, "data_size": 10})
        bad_value = json.dumps({"ts": T0.isoformat(), "data_used": "nan?"})
        p.write_text("not json\n" + good + '\n{"ts": "x"}\n' + bad_value + "\n")
        assert len(load_history(p)) == 1

    def test_a_future_sample_cannot_defeat_the_interval_gate(self, tmp_path) -> None:
        # Review: one sample dated ahead (a clock stepped forward, then
        # corrected) stayed history[-1], so every pass appended and the 7-day
        # history shrank to hours.
        p = tmp_path / "h.jsonl"
        record_sample(p, _s(0, 0.5), min_interval_s=300, max_samples=100)
        record_sample(p, _s(365 * 24 * 60, 0.5), min_interval_s=300, max_samples=100)
        for i in range(20):
            record_sample(p, _s(1 + i * 0.5, 0.5), min_interval_s=300, max_samples=100)
        h = load_history(p)
        assert all(s.ts <= T0 + timedelta(hours=2) for s in h)
        assert len(h) <= 4

    def test_clock_going_backwards_still_records(self, tmp_path) -> None:
        p = tmp_path / "h.jsonl"
        record_sample(p, _s(60, 0.5), min_interval_s=300, max_samples=100)
        record_sample(p, _s(0, 0.5), min_interval_s=300, max_samples=100)
        assert len(load_history(p)) == 2


# --- growth rate ----------------------------------------------------------------


class TestGrowthRate:
    def test_flat_history_has_no_growth(self) -> None:
        r = _runway(_series(24, lambda h: (0.5, 0.3)))
        assert r.data_rate == 0.0 and r.data_hours is None
        assert early_reason(r, 48) is None

    def test_short_history_has_no_rate(self) -> None:
        r = _runway(_series(1, lambda h: (0.5 + h * 0.01, 0.3)))
        assert r.data_rate is None and early_reason(r, 48) is None

    def test_jittered_samples_yield_a_2h_rate(self) -> None:
        # CodeRabbit: the 2h window required a full 2h span, which jittered
        # ~5-minute samples almost never have.
        ramp = _series(2, lambda h: (0.50 + h * 0.02, None), jitter_s=30)
        assert ramp[-1].ts - ramp[0].ts < timedelta(hours=2)
        r = _runway(ramp)
        assert r.data_rate == pytest.approx(0.02 * 100 * _GB, rel=0.05)

    def test_steady_growth_gives_hours_to_full(self) -> None:
        r = _runway(_series(12, lambda h: (0.50 + h * 0.01, None)))
        # 1 GB/h into 38 GB free.
        assert r.data_hours == pytest.approx(38, rel=0.05)
        assert "data would fill" in early_reason(r, 48)
        assert early_reason(r, 24) is None

    def test_a_one_off_backup_step_is_not_a_rate(self) -> None:
        r = _runway(_series(24, lambda h: (0.50 if h < 11 else 0.52, None)))
        assert not r.data_rate

    @pytest.mark.parametrize("at_hours", [0.5, 5, 12, 20, 30])
    def test_a_snapshot_metadata_step_at_a_high_baseline_is_not_a_rate(self, at_hours) -> None:
        # Devin (round 2): at 92% metadata a fresh snapshot adds four points,
        # then usage stays flat; the old design read the step as sustained
        # growth once the history passed 20h and deleted the lifeline.
        r = _runway(_series(36, lambda h: (0.5, 0.92 if h < at_hours else 0.96)))
        assert early_reason(r, 48) is None

    def test_a_daily_recurring_step_reads_as_its_average(self) -> None:
        r = _runway(_series(72, lambda h: (0.50 + 0.02 * int(h // 24), None)))
        assert r.data_rate is not None and r.data_rate > 0

    def test_a_metadata_fill_inside_ten_hours_is_caught(self) -> None:
        # Codex (round 1, P1): metadata rising 80% -> 95% over ten hours was
        # invisible until the history spanned twenty hours.
        r = _runway(_series(10, lambda h: (0.5, 0.80 + h * 0.015)))
        assert r.meta_hours == pytest.approx(0.05 / 0.015, rel=0.1)
        assert "metadata would fill" in early_reason(r, 48)

    def test_a_metadata_resize_does_not_hide_growth(self) -> None:
        # Devin (round 2): LVM doubles the metadata LV; the percentage halves
        # while usage keeps rising. Tracked in bytes, growth stays visible.
        def at(h):
            used = (0.60 + h * 0.01) * 80 * _MB
            return used, (80 if h < 6 else 160) * _MB

        samples = []
        t = 0.0
        while t <= 12 * 60:
            used, size = at(t / 60)
            samples.append(
                PoolSample(T0 + timedelta(minutes=t), POOL, 50 * _GB, 100 * _GB, used, size)
            )
            t += 5
        r = _runway(samples)
        assert r.meta_rate == pytest.approx(0.01 * 80 * _MB, rel=0.05)

    def test_a_pool_extend_is_not_shrinkage(self) -> None:
        samples = [_s(m, 0.80, size=100 * _GB) for m in range(0, 360, 5)]
        samples += [
            PoolSample(T0 + timedelta(minutes=m), POOL, 80 * _GB, 110 * _GB)
            for m in range(360, 720, 5)
        ]
        r = _runway(samples)
        assert r.data_rate == 0.0

    def test_a_delayed_tick_rise_is_a_rate(self) -> None:
        # Codex (round 2, P1): a 100 GiB pool rising 80% -> 96% across a
        # 3.5-hour gap between passes read as no rate at all.
        r = compute_runway([_s(0, 0.80)], _s(210, 0.96))
        assert r.data_hours is not None and r.data_hours < 2
        assert early_reason(r, 48) is not None

    def test_the_gap_rule_scales_with_the_sample_interval(self) -> None:
        # Review: with a one-hour sample interval every consecutive pair is
        # over 30 minutes apart, so a one-off step read as a rate.
        gap = gap_threshold(3600)
        assert gap == timedelta(hours=3)
        hist = [_s(m, 0.5, 0.40) for m in range(0, 24 * 60, 60)]
        step = _s(24 * 60 - 60 + 45, 0.5, 0.45)
        assert compute_runway(hist, step, gap).meta_rate in (None, 0.0)
        assert compute_runway(hist, step).meta_rate  # the fixed 30-minute rule would fire

    def test_a_gap_under_the_threshold_alone_is_no_rate(self) -> None:
        r = compute_runway([_s(0, 0.80)], _s(20, 0.81))
        assert r.data_rate is None

    def test_another_pools_history_is_ignored(self) -> None:
        # The container moved pools: the old pool's figures must not read as a
        # burst or a rate on the new one.
        old = [_s(m, 0.20, pool="other|vg1|tp") for m in range(0, 600, 5)]
        r = compute_runway(old, _s(605, 0.90))
        assert r.data_rate is None

    def test_growth_rate_never_negative(self) -> None:
        pts = [(T0 + timedelta(minutes=m), 100.0 - m) for m in range(0, 240, 5)]
        assert growth_rate(pts, pts[-1][0]) == 0.0


class TestEarlyReason:
    def test_metadata_first(self) -> None:
        from genesis.guardian.pool_runway import Runway

        r = Runway(1.0, 10.0, 1.0, 20.0)
        assert early_reason(r, 48).startswith("metadata")

    def test_horizon_zero_disables(self) -> None:
        from genesis.guardian.pool_runway import Runway

        assert early_reason(Runway(1.0, 1.0, 1.0, 1.0), 0) is None



# --- the LVM partial extend ---------------------------------------------------------


def _pool(**kw) -> StoragePoolStatus:
    base = dict(
        detected=True,
        data_pct=85.0,
        metadata_pct=40.0,
        pool_size_bytes=100 * _GB,
        pool_name="default",
        vg_name="vg0",
        thinpool_lv="IncusThinPool",
        vg_free_bytes=6 * _GB,
        metadata_size_bytes=84 * _MB,
        thinpool_profile="genesis-thinpool",
    )
    base.update(kw)
    return StoragePoolStatus(**base)


class TestPlanExtend:
    def test_the_incident_shape_extends_into_the_stranded_space(self) -> None:
        # VG free (6 GiB) is under one 20% step (20 GiB): autoextend cannot act.
        assert plan_extend(_pool(), 512) == 6 * _GB - 512 * _MB

    @pytest.mark.parametrize(
        "kw",
        [
            {"thinpool_profile": None},
            {"thinpool_profile": "other"},
            {"data_pct": 79.9},
            {"data_pct": None},
            {"vg_free_bytes": 20 * _GB},  # a full step: dmeventd's job
            {"vg_free_bytes": 1 * _GB},  # under 1 GiB after the keep
            {"vg_free_bytes": None},
            {"thinpool_lv": None},
            {"vg_name": None},
            {"pool_size_bytes": None},
        ],
    )
    def test_refuses(self, kw) -> None:
        assert plan_extend(_pool(**kw), 512) is None

    def test_keeps_twice_the_metadata_lv_when_larger(self) -> None:
        assert plan_extend(_pool(metadata_size_bytes=1 * _GB), 512) == 4 * _GB


class _Run:
    def __init__(self, *, extent: str = "  4194304\n", vgs_rc: int = 0, lvextend_rc: int = 0):
        self.calls: list[tuple] = []
        self.extent, self.vgs_rc, self.lvextend_rc = extent, vgs_rc, lvextend_rc

    async def __call__(self, *argv, timeout: float = 0, stdin_data=None):  # noqa: ARG002
        self.calls.append(argv)
        if "vgs" in argv:
            return self.vgs_rc, self.extent if self.vgs_rc == 0 else "", "vgs: boom"
        if "lvextend" in argv:
            return self.lvextend_rc, "", "" if self.lvextend_rc == 0 else "lvextend: no space"
        raise AssertionError(argv)


async def _yes() -> int:
    return 10 * _GB


async def _no() -> None:
    return None


class TestExtendThinpool:
    @pytest.mark.asyncio
    async def test_extends_by_whole_extents(self) -> None:
        run = _Run()
        ok, attempted, detail = await extend_thinpool(_pool(), 5 * _GB + 123, run, _yes)
        assert (ok, attempted) == (True, True)
        assert run.calls[-1] == (
            "sudo",
            "-n",
            "lvextend",
            "-L",
            f"+{5 * _GB}b",
            "vg0/IncusThinPool",
        )
        assert "grew vg0/IncusThinPool" in detail

    @pytest.mark.asyncio
    async def test_unreadable_extent_is_not_an_attempt(self) -> None:
        run = _Run(vgs_rc=5)
        guard_called = []

        async def guard():
            guard_called.append(1)
            return 10 * _GB

        ok, attempted, _ = await extend_thinpool(_pool(), 5 * _GB, run, guard)
        assert (ok, attempted) == (False, False)
        assert not guard_called  # the cooldown stamp lives in the guard
        assert not any("lvextend" in c for c in run.calls)

    @pytest.mark.asyncio
    async def test_guard_refusal_stops_before_the_mutation(self) -> None:
        run = _Run()
        ok, attempted, detail = await extend_thinpool(_pool(), 5 * _GB, run, _no)
        assert (ok, attempted, detail) == (False, False, GUARD_STOP)
        assert not any("lvextend" in c for c in run.calls)

    @pytest.mark.asyncio
    async def test_failed_lvextend_is_an_attempt(self) -> None:
        ok, attempted, detail = await extend_thinpool(_pool(), 5 * _GB, _Run(lvextend_rc=5), _yes)
        assert (ok, attempted) == (False, True)
        assert "lvextend failed" in detail


def test_the_measured_host_lvs_row_parses_identity_and_profile() -> None:
    """The row a live host's `lvs --reportformat json` printed (LVM 2.03.16)
    for the fields relief reads, verbatim apart from whitespace."""
    from genesis.guardian.pool import parse_lvs_report

    measured = (
        '{"report": [{"lv": [{"data_percent":"56.83", "metadata_percent":"41.81", '
        '"lv_size":"88684363776", "lv_name":"IncusThinPool", '
        '"lv_metadata_size":"88080384", "lv_profile":"genesis-thinpool"}]}]}'
    )
    r = parse_lvs_report(measured, "IncusThinPool")
    assert (r.data_pct, r.metadata_pct, r.size_bytes) == (56.83, 41.81, 88684363776)
    assert (r.metadata_size_bytes, r.profile) == (88080384, "genesis-thinpool")
    blank = measured.replace('"genesis-thinpool"', '""').replace('"88080384"', '""')
    r = parse_lvs_report(blank, "IncusThinPool")
    assert (r.metadata_size_bytes, r.profile) == (None, None)


class TestExtendThinpoolReplanAndTimeout:
    @pytest.mark.asyncio
    async def test_never_exceeds_the_fresh_plan(self) -> None:
        # Review: VG free fell between the decision and the mutation; the
        # stale size was still issued.
        run = _Run()

        async def smaller() -> int:
            return 2 * _GB

        ok, _, _ = await extend_thinpool(_pool(), 5 * _GB, run, smaller)
        assert ok and run.calls[-1][4] == f"+{2 * _GB}b"

    @pytest.mark.asyncio
    async def test_a_timeout_is_unknown_not_failed(self) -> None:
        class _Slow(_Run):
            async def __call__(self, *argv, timeout: float = 0, stdin_data=None):  # noqa: ARG002
                if "lvextend" in argv:
                    self.calls.append(argv)
                    return -1, "", "timeout"
                return await super().__call__(*argv, timeout=timeout)

        ok, attempted, detail = await extend_thinpool(_pool(), 5 * _GB, _Slow(), _yes)
        assert (ok, attempted, detail) == (False, True, TIMED_OUT)



def test_an_undecodable_byte_costs_its_line_and_the_file_heals(tmp_path) -> None:
    # Review: one non-UTF-8 byte made load_history raise, which also stopped
    # record_sample (it loads first) from ever rewriting the file.
    p = tmp_path / "h.jsonl"
    record_sample(p, _s(0, 0.5), min_interval_s=300, max_samples=100)
    with p.open("ab") as fh:
        fh.write(b'{"ts": "\xff\xfe"}\n')
    assert len(load_history(p)) == 1
    h = record_sample(p, _s(10, 0.5), min_interval_s=300, max_samples=100)
    assert len(h) == 2


def test_the_gap_rate_survives_the_next_pass() -> None:
    # Review: during an outage the post-cycle pass records the post-gap sample;
    # the next tick's pre-cycle pass, seconds later, saw no gap and no rate.
    hist = [_s(0, 0.80), _s(60, 0.82)]  # recorded, an hour apart
    after = _s(60.5, 0.82)  # the next pass, not recorded
    r = compute_runway(hist, after)
    assert r.data_rate == pytest.approx(0.02 * 100 * _GB, rel=0.05)
    # ...but not forever: once the reading is a whole gap past the last sample.
    later = _s(60 + 31, 0.82)
    assert not compute_runway(hist, later).data_rate


def test_negative_history_values_are_corruption(tmp_path) -> None:
    p = tmp_path / "h.jsonl"
    p.write_text(json.dumps({"ts": T0.isoformat(), "pool": POOL, "data_used": -5.0,
                             "data_size": 10}) + "\n")
    (s,) = load_history(p)
    assert s.data_used is None


@pytest.mark.asyncio
async def test_an_infinite_extent_size_is_not_a_crash() -> None:
    ok, attempted, _ = await extend_thinpool(_pool(), 5 * _GB, _Run(extent="  inf\n"), _yes)
    assert (ok, attempted) == (False, False)
