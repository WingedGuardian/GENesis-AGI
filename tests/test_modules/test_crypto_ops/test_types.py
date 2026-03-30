"""Tests for crypto ops types."""

from genesis.modules.crypto_ops.types import (
    Chain,
    LaunchPackage,
    LaunchStatus,
    Narrative,
    NarrativeStatus,
    TokenPosition,
)


class TestNarrative:
    def test_defaults(self):
        n = Narrative(name="AI agents")
        assert n.name == "AI agents"
        assert n.status == NarrativeStatus.EMERGING
        assert n.momentum_score == 0.0
        assert n.id

    def test_signals_list(self):
        n = Narrative(signals=["sig1", "sig2"])
        assert len(n.signals) == 2


class TestLaunchPackage:
    def test_defaults(self):
        lp = LaunchPackage(token_name="TestCoin", token_ticker="TST")
        assert lp.status == LaunchStatus.PROPOSED
        assert lp.chain == Chain.SOLANA

    def test_base_chain(self):
        lp = LaunchPackage(chain=Chain.BASE)
        assert lp.chain == "base"


class TestTokenPosition:
    def test_defaults(self):
        tp = TokenPosition(token_name="Test", entry_price=1.0)
        assert tp.entry_price == 1.0
        assert tp.exit_price is None
        assert tp.pnl == 0.0
