"""Tests for host storage-pool monitoring (pure logic)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from genesis.guardian import pool as pool_mod
from genesis.guardian.config import StoragePoolConfig
from genesis.guardian.pool import (
    TIER_CRIT,
    TIER_HIGH,
    TIER_OK,
    TIER_WARN,
    StoragePoolStatus,
    ThinPoolReport,
    decide_alert,
    measure_storage_pool,
    parse_lvs_report,
    worst_tier,
)


def _status(data=None, meta=None) -> StoragePoolStatus:
    return StoragePoolStatus(detected=True, data_pct=data, metadata_pct=meta)


class TestWorstTier:
    cfg = StoragePoolConfig()

    def test_ok_when_both_low(self):
        assert worst_tier(_status(50, 40), self.cfg) == TIER_OK

    def test_data_tiers(self):
        assert worst_tier(_status(76, 0), self.cfg) == TIER_WARN
        assert worst_tier(_status(86, 0), self.cfg) == TIER_HIGH
        assert worst_tier(_status(93, 0), self.cfg) == TIER_CRIT

    def test_metadata_alerts_earlier_than_data(self):
        # metadata 72% is HIGH (>=70) while the same 72% data is only WARN.
        assert worst_tier(_status(0, 72), self.cfg) == TIER_HIGH
        assert worst_tier(_status(72, 0), self.cfg) == TIER_OK  # 72 < data_warn 75

    def test_worst_of_the_two_wins(self):
        # data WARN (76) + metadata CRIT (81) → CRIT
        assert worst_tier(_status(76, 81), self.cfg) == TIER_CRIT

    def test_none_percents_are_ok(self):
        assert worst_tier(_status(None, None), self.cfg) == TIER_OK

    def test_pool_used_is_the_fallback_signal(self):
        # Non-LVM backend (btrfs): only pool_used_pct carries signal — it must
        # tier, else a btrfs pool filling to 100% stays OK forever.
        def _btrfs(used):
            return StoragePoolStatus(detected=True, pool_used_pct=used)

        assert worst_tier(_btrfs(50.0), self.cfg) == TIER_OK
        assert worst_tier(_btrfs(76.0), self.cfg) == TIER_WARN
        assert worst_tier(_btrfs(86.0), self.cfg) == TIER_HIGH
        assert worst_tier(_btrfs(93.0), self.cfg) == TIER_CRIT

    def test_pool_used_ignored_when_lvm_percents_present(self):
        # LVM-thin: data%/metadata% stay the sole authority. Even if a used%
        # were present, it must NOT change long-standing behavior.
        s = StoragePoolStatus(
            detected=True, data_pct=50.0, metadata_pct=40.0, pool_used_pct=95.0,
        )
        assert worst_tier(s, self.cfg) == TIER_OK

    def test_pool_used_applies_when_only_one_lvm_percent_missing_is_not_fallback(self):
        # Even ONE present LVM percent keeps the LVM authority (partial lvs
        # output) — fallback engages only when BOTH are absent.
        s = StoragePoolStatus(detected=True, data_pct=50.0, pool_used_pct=95.0)
        assert worst_tier(s, self.cfg) == TIER_OK


def _lvs_json(*rows: dict) -> str:
    """The shape a real `lvs --reportformat json` prints (measured, LVM 2.03)."""
    return json.dumps({"report": [{"lv": list(rows)}]}, indent=2)


def _row(data="73.57", meta="43.51", size="73903636480", name="IncusThinPool") -> dict:
    return {"data_percent": data, "metadata_percent": meta, "lv_size": size, "lv_name": name}


class TestParseLvsReport:
    """Fields are bound by NAME: a blank percent (an inactive LV) must read as
    no signal, never shift the byte size into the data% column."""

    def test_single_row(self):
        assert parse_lvs_report(_lvs_json(_row())) == ThinPoolReport(
            data_pct=73.57, metadata_pct=43.51,
            size_bytes=73_903_636_480, lv_name="IncusThinPool",
        )

    def test_blank_percents_are_none_not_shifted(self):
        rep = parse_lvs_report(_lvs_json(_row(data="", meta="")))
        assert rep.data_pct is None and rep.metadata_pct is None
        assert rep.size_bytes == 73_903_636_480 and rep.lv_name == "IncusThinPool"

    def test_blank_percents_tier_ok_not_crit(self):
        rep = parse_lvs_report(_lvs_json(_row(data="", meta="")))
        status = StoragePoolStatus(
            detected=True, data_pct=rep.data_pct, metadata_pct=rep.metadata_pct,
        )
        assert worst_tier(status, StoragePoolConfig()) == TIER_OK

    def test_two_thin_pools_keep_percents_but_no_identity(self):
        rep = parse_lvs_report(_lvs_json(_row(name="poolA"), _row("10.00", "5.00", "1", "poolB")))
        assert (rep.data_pct, rep.metadata_pct) == (73.57, 43.51)
        assert rep.size_bytes is None and rep.lv_name is None

    @pytest.mark.parametrize("out", [
        "", "\n", "  73.57  43.51  73903636480  IncusThinPool\n", "{}", '{"report": []}',
        '{"report": [{"lv": []}]}', '{"report": [{"lv": ["x"]}]}', "[1, 2]",
    ])
    def test_unexpected_shape_is_no_signal(self, out):
        assert parse_lvs_report(out) == ThinPoolReport()

    def test_garbage_values_are_none(self):
        rep = parse_lvs_report(_lvs_json(_row(data="n/a", meta="nan", size="73.9g", name="  ")))
        assert rep == ThinPoolReport()


class TestMeasureLvmPool:
    """End-to-end of the LVM branch: identity fields reach the status, and a
    VG with two thin pools leaves the LV unnamed (relief then never acts)."""

    @staticmethod
    def _patch(monkeypatch, lvs_out: str, calls: list | None = None):
        async def _detect(_config):
            return "default"

        async def _backend(_name):
            return "lvm", "vg0"

        async def _run(*a, **k):
            if calls is not None:
                calls.append(a)
            if "lvs" in a:
                assert a[a.index("--reportformat") + 1] == "json"
                return 0, lvs_out, ""
            if "vgs" in a:
                return 0, "  21474836480\n", ""
            return 1, "", "unexpected"

        monkeypatch.setattr(pool_mod, "_detect_pool_name", _detect)
        monkeypatch.setattr(pool_mod, "_pool_driver_and_source", _backend)
        monkeypatch.setattr(pool_mod, "_run_subprocess", _run)

    @pytest.mark.asyncio
    async def test_identity_reaches_status(self, monkeypatch):
        calls: list[tuple] = []
        self._patch(monkeypatch, _lvs_json(_row()), calls)
        status = await measure_storage_pool(object())
        assert status.detected is True
        assert (status.data_pct, status.metadata_pct) == (73.57, 43.51)
        assert status.pool_size_bytes == 73_903_636_480
        assert (status.pool_name, status.vg_name, status.thinpool_lv) == (
            "default", "vg0", "IncusThinPool",
        )
        assert status.vg_free_bytes == 21_474_836_480
        lvs = next(c for c in calls if "lvs" in c)
        # Bytes, not lvm's default human units — the size is used as a number.
        assert "--units" in lvs and lvs[lvs.index("--units") + 1] == "b"

    @pytest.mark.asyncio
    async def test_two_thin_pools_leave_lv_unnamed(self, monkeypatch):
        self._patch(
            monkeypatch, _lvs_json(_row(name="poolA"), _row("10.00", "5.00", "1", "poolB")),
        )
        status = await measure_storage_pool(object())
        assert status.detected is True
        assert status.thinpool_lv is None and status.pool_size_bytes is None


class TestDecideAlert:
    now = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)

    def test_tier_increase_alerts(self):
        d = decide_alert(TIER_WARN, TIER_OK, None, self.now, 6.0)
        assert d.should_alert and not d.is_resolution

    def test_jump_multiple_tiers_alerts(self):
        d = decide_alert(TIER_CRIT, TIER_OK, None, self.now, 6.0)
        assert d.should_alert and d.tier == TIER_CRIT

    def test_recovery_to_ok_sends_resolution(self):
        d = decide_alert(TIER_OK, TIER_HIGH, self.now - timedelta(hours=1), self.now, 6.0)
        assert d.should_alert and d.is_resolution

    def test_ok_to_ok_silent(self):
        assert not decide_alert(TIER_OK, TIER_OK, None, self.now, 6.0).should_alert

    def test_sustained_realerts_after_interval(self):
        last = self.now - timedelta(hours=7)
        assert decide_alert(TIER_HIGH, TIER_HIGH, last, self.now, 6.0).should_alert

    def test_sustained_silent_within_interval(self):
        last = self.now - timedelta(hours=2)
        assert not decide_alert(TIER_HIGH, TIER_HIGH, last, self.now, 6.0).should_alert

    def test_tier_decrease_still_nonok_is_silent(self):
        # crit → high (still bad) does not spam; caller records new tier.
        assert not decide_alert(TIER_HIGH, TIER_CRIT, self.now, self.now, 6.0).should_alert

    def test_sustained_with_no_prior_time_alerts(self):
        # Defensive: missing last_alert_at shouldn't suppress a live problem.
        assert decide_alert(TIER_WARN, TIER_WARN, None, self.now, 6.0).should_alert


def _df(used: int, size: int) -> str:
    # `df -B1 --output=used,size` form: header row + one data row.
    return f"       Used    1B-blocks\n{used} {size}\n"


class TestPoolUsedViaDf:
    """The non-LVM used% signal. `incus storage info` has no machine-readable
    space for an uncapped btrfs-on-LV pool (its `--format json` flag doesn't
    even exist), so the mount is read via df."""

    @pytest.mark.asyncio
    async def test_computes_used_pct(self, monkeypatch):
        async def _run(*a, **k):
            return 0, _df(48_318_382_080, 322_122_547_200), ""

        monkeypatch.setattr(pool_mod, "_run_subprocess", _run)
        pct = await pool_mod._pool_used_pct_via_df("/mnt/pool")
        assert pct == pytest.approx(15.0, abs=0.1)

    @pytest.mark.asyncio
    async def test_rc_nonzero_is_none(self, monkeypatch):
        async def _run(*a, **k):
            return 1, "", "df: no such file or directory"

        monkeypatch.setattr(pool_mod, "_run_subprocess", _run)
        assert await pool_mod._pool_used_pct_via_df("/nope") is None

    @pytest.mark.asyncio
    async def test_single_line_output_is_none(self, monkeypatch):
        async def _run(*a, **k):
            return 0, "only a header\n", ""

        monkeypatch.setattr(pool_mod, "_run_subprocess", _run)
        assert await pool_mod._pool_used_pct_via_df("/mnt/pool") is None

    @pytest.mark.asyncio
    async def test_nonnumeric_row_is_none(self, monkeypatch):
        async def _run(*a, **k):
            return 0, "Used 1B-blocks\nfoo bar\n", ""

        monkeypatch.setattr(pool_mod, "_run_subprocess", _run)
        assert await pool_mod._pool_used_pct_via_df("/mnt/pool") is None

    @pytest.mark.asyncio
    async def test_zero_size_is_none(self, monkeypatch):
        # Never divide by zero — a 0-size mount yields no signal, not a crash.
        async def _run(*a, **k):
            return 0, _df(0, 0), ""

        monkeypatch.setattr(pool_mod, "_run_subprocess", _run)
        assert await pool_mod._pool_used_pct_via_df("/mnt/pool") is None


class TestMeasureNonLvmPool:
    """End-to-end of the non-LVM branch of measure_storage_pool: a btrfs pool
    must surface a real pool_used_pct (from df) so worst_tier can tier it."""

    @pytest.mark.asyncio
    async def test_btrfs_pool_populates_used_pct(self, monkeypatch):
        async def _detect(_config):
            return "genesis-btrfs"

        async def _backend(_name):
            return "btrfs", "/dev/vg/btrfs"  # non-LVM backend

        async def _run(*a, **k):
            return 0, _df(48_318_382_080, 322_122_547_200), ""

        monkeypatch.setattr(pool_mod, "_detect_pool_name", _detect)
        monkeypatch.setattr(pool_mod, "_pool_driver_and_source", _backend)
        monkeypatch.setattr(pool_mod, "_run_subprocess", _run)

        status = await measure_storage_pool(object())
        assert status.detected is True
        assert status.data_pct is None and status.metadata_pct is None
        assert status.pool_used_pct == pytest.approx(15.0, abs=0.1)
        # The whole point: a measured btrfs pool now tiers.
        assert worst_tier(status, StoragePoolConfig()) == TIER_OK

    @pytest.mark.asyncio
    async def test_btrfs_df_failure_is_not_detected(self, monkeypatch):
        async def _detect(_config):
            return "genesis-btrfs"

        async def _backend(_name):
            return "btrfs", "/dev/vg/btrfs"

        async def _run(*a, **k):
            return 1, "", "df failed"

        monkeypatch.setattr(pool_mod, "_detect_pool_name", _detect)
        monkeypatch.setattr(pool_mod, "_pool_driver_and_source", _backend)
        monkeypatch.setattr(pool_mod, "_run_subprocess", _run)

        status = await measure_storage_pool(object())
        assert status.detected is False
        assert status.pool_used_pct is None


class TestBackendUnknown:
    """A failed backend lookup must read as UNDETECTED, never as "not LVM":
    the df fallback would then measure the host filesystem, not the pool."""

    @pytest.mark.asyncio
    async def test_storage_show_failure_is_not_detected(self, monkeypatch):
        calls: list[tuple] = []

        async def _detect(_config):
            return "default"

        async def _run(*a, **k):
            calls.append(a)
            if a[:3] == ("incus", "storage", "show"):
                return 1, "", "incus: daemon unreachable"
            return 0, _df(90, 100), ""  # would look 90% full if it were read

        monkeypatch.setattr(pool_mod, "_detect_pool_name", _detect)
        monkeypatch.setattr(pool_mod, "_run_subprocess", _run)
        status = await measure_storage_pool(object())
        assert status.detected is False
        assert not any(c and c[0] == "df" for c in calls)
