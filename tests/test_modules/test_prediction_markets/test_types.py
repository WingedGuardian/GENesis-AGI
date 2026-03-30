"""Tests for prediction market types."""

from genesis.modules.prediction_markets.types import (
    BetRecord,
    BetStatus,
    Estimate,
    Market,
    MarketSource,
    MarketStatus,
)


class TestMarket:
    def test_defaults(self):
        m = Market(title="Will X happen?")
        assert m.title == "Will X happen?"
        assert m.status == MarketStatus.OPEN
        assert m.current_price == 0.5
        assert m.source == MarketSource.CUSTOM
        assert m.id  # auto-generated

    def test_polymarket_source(self):
        m = Market(source=MarketSource.POLYMARKET, title="Test")
        assert m.source == "polymarket"


class TestEstimate:
    def test_edge_calculation(self):
        e = Estimate(estimated_probability=0.7, market_price=0.5, edge=0.2)
        assert e.edge == 0.2

    def test_defaults(self):
        e = Estimate()
        assert e.confidence_in_estimate == 0.5


class TestBetRecord:
    def test_defaults(self):
        b = BetRecord(market_id="m1", market_title="Test")
        assert b.status == BetStatus.CONSIDERED
        assert b.direction == "yes"
        assert b.actual_outcome is None

    def test_placed_status(self):
        b = BetRecord(status=BetStatus.PLACED, position_size=100.0)
        assert b.position_size == 100.0
