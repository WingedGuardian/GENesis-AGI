"""Tests for engagement heuristics."""

from __future__ import annotations

from genesis.learning.engagement import classify_engagement
from genesis.learning.types import EngagementOutcome, EngagementSignal


class TestClassifyEngagement:
    # --- WhatsApp / Telegram ---

    def test_whatsapp_reply_fast_engaged(self) -> None:
        sig = classify_engagement("whatsapp", response_latency_s=3600.0, has_reply=True)
        assert sig.outcome == EngagementOutcome.ENGAGED

    def test_whatsapp_reaction_engaged(self) -> None:
        sig = classify_engagement("whatsapp", response_latency_s=None, has_reaction=True)
        assert sig.outcome == EngagementOutcome.ENGAGED

    def test_whatsapp_no_response_ignored(self) -> None:
        sig = classify_engagement("whatsapp", response_latency_s=90000.0)
        assert sig.outcome == EngagementOutcome.IGNORED

    def test_whatsapp_slow_reply_neutral(self) -> None:
        sig = classify_engagement("whatsapp", response_latency_s=50000.0, has_reply=True)
        assert sig.outcome == EngagementOutcome.NEUTRAL

    def test_telegram_same_as_whatsapp(self) -> None:
        sig = classify_engagement("telegram", response_latency_s=3600.0, has_reply=True)
        assert sig.outcome == EngagementOutcome.ENGAGED

    # --- Web ---

    def test_web_click_fast_engaged(self) -> None:
        sig = classify_engagement("web", response_latency_s=120.0, has_reply=True)
        assert sig.outcome == EngagementOutcome.ENGAGED

    def test_web_no_interaction_ignored(self) -> None:
        sig = classify_engagement("web", response_latency_s=90000.0)
        assert sig.outcome == EngagementOutcome.IGNORED

    def test_web_moderate_neutral(self) -> None:
        sig = classify_engagement("web", response_latency_s=600.0, has_reply=True)
        assert sig.outcome == EngagementOutcome.NEUTRAL

    # --- Terminal ---

    def test_terminal_fast_substantive_engaged(self) -> None:
        sig = classify_engagement(
            "terminal", response_latency_s=30.0, has_reply=True, reply_substantive=True
        )
        assert sig.outcome == EngagementOutcome.ENGAGED

    def test_terminal_fast_monosyllabic_neutral(self) -> None:
        sig = classify_engagement(
            "terminal", response_latency_s=30.0, has_reply=True, reply_substantive=False
        )
        assert sig.outcome == EngagementOutcome.NEUTRAL

    def test_terminal_no_reply_ignored(self) -> None:
        sig = classify_engagement("terminal", response_latency_s=4000.0)
        assert sig.outcome == EngagementOutcome.IGNORED

    # --- Edge cases ---

    def test_none_latency_no_signals_neutral(self) -> None:
        sig = classify_engagement("whatsapp", response_latency_s=None)
        assert sig.outcome == EngagementOutcome.NEUTRAL

    def test_unknown_channel_uses_defaults(self) -> None:
        sig = classify_engagement("unknown_channel", response_latency_s=90000.0)
        assert sig.outcome == EngagementOutcome.IGNORED

    def test_returns_engagement_signal(self) -> None:
        sig = classify_engagement("whatsapp", response_latency_s=100.0, has_reply=True)
        assert isinstance(sig, EngagementSignal)
        assert sig.channel == "whatsapp"
        assert sig.latency_seconds == 100.0
        assert sig.evidence != ""
