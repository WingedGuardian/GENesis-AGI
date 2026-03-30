"""Tests for CryptoOutcomeTracker."""

from genesis.modules.crypto_ops.tracker import CryptoOutcomeTracker


class TestCryptoOutcomeTracker:
    def test_record_launch(self):
        tracker = CryptoOutcomeTracker()
        tracker.record_launch("l1", narrative_name="AI", chain="solana", invested=100)
        assert tracker.total_launches == 1

    def test_record_exit(self):
        tracker = CryptoOutcomeTracker()
        tracker.record_launch("l1", invested=100)
        tracker.record_exit("l1", pnl=50, narrative_accurate=True, timing="on-time")
        assert tracker.total_pnl() == 50

    def test_win_rate(self):
        tracker = CryptoOutcomeTracker()
        tracker.record_launch("l1", invested=100)
        tracker.record_launch("l2", invested=100)
        tracker.record_exit("l1", pnl=50)
        tracker.record_exit("l2", pnl=-30)
        assert tracker.win_rate() == 0.5

    def test_average_return(self):
        tracker = CryptoOutcomeTracker()
        tracker.record_launch("l1", invested=100)
        tracker.record_exit("l1", pnl=50)
        avg = tracker.average_return()
        assert avg is not None
        assert avg == 0.5

    def test_narrative_accuracy(self):
        tracker = CryptoOutcomeTracker()
        tracker.record_launch("l1")
        tracker.record_launch("l2")
        tracker.record_exit("l1", pnl=0, narrative_accurate=True)
        tracker.record_exit("l2", pnl=0, narrative_accurate=False)
        assert tracker.narrative_accuracy() == 0.5

    def test_timing_breakdown(self):
        tracker = CryptoOutcomeTracker()
        tracker.record_launch("l1")
        tracker.record_launch("l2")
        tracker.record_exit("l1", pnl=0, timing="early")
        tracker.record_exit("l2", pnl=0, timing="on-time")
        breakdown = tracker.timing_breakdown()
        assert breakdown["early"] == 1
        assert breakdown["on-time"] == 1

    def test_chain_breakdown(self):
        tracker = CryptoOutcomeTracker()
        tracker.record_launch("l1", chain="solana", invested=100)
        tracker.record_launch("l2", chain="base", invested=100)
        tracker.record_exit("l1", pnl=50)
        breakdown = tracker.chain_breakdown()
        assert "solana" in breakdown
        assert "base" in breakdown

    def test_stats(self):
        tracker = CryptoOutcomeTracker()
        stats = tracker.stats()
        assert stats["total_launches"] == 0
        assert stats["win_rate"] is None

    def test_empty_metrics(self):
        tracker = CryptoOutcomeTracker()
        assert tracker.win_rate() is None
        assert tracker.average_return() is None
        assert tracker.narrative_accuracy() is None
        assert tracker.total_pnl() == 0.0
