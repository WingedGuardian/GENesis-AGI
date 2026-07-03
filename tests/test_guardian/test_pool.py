"""Tests for host storage-pool monitoring (pure logic)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from genesis.guardian.config import StoragePoolConfig
from genesis.guardian.pool import (
    TIER_CRIT,
    TIER_HIGH,
    TIER_OK,
    TIER_WARN,
    StoragePoolStatus,
    decide_alert,
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
