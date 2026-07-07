"""Tests for host storage-pool monitoring (pure logic)."""

from __future__ import annotations

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
    decide_alert,
    measure_storage_pool,
    parse_lvs_data_metadata,
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


class TestParseLvs:
    def test_parses_data_and_metadata(self):
        assert parse_lvs_data_metadata("  75.00  12.34\n") == (75.0, 12.34)

    def test_handles_single_column(self):
        assert parse_lvs_data_metadata("  88.5\n") == (88.5, None)

    def test_empty(self):
        assert parse_lvs_data_metadata("") == (None, None)
        assert parse_lvs_data_metadata("\n\n") == (None, None)

    def test_garbage_is_none(self):
        assert parse_lvs_data_metadata("  n/a  x\n") == (None, None)


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

        async def _lvm(_name):
            return None  # non-LVM backend

        async def _run(*a, **k):
            return 0, _df(48_318_382_080, 322_122_547_200), ""

        monkeypatch.setattr(pool_mod, "_detect_pool_name", _detect)
        monkeypatch.setattr(pool_mod, "_lvm_source", _lvm)
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

        async def _lvm(_name):
            return None

        async def _run(*a, **k):
            return 1, "", "df failed"

        monkeypatch.setattr(pool_mod, "_detect_pool_name", _detect)
        monkeypatch.setattr(pool_mod, "_lvm_source", _lvm)
        monkeypatch.setattr(pool_mod, "_run_subprocess", _run)

        status = await measure_storage_pool(object())
        assert status.detected is False
        assert status.pool_used_pct is None
