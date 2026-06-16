"""Tests for the outbound egress gate (genesis.content.egress)."""

from __future__ import annotations

from genesis.content.egress import EgressResult, gate, should_gate

EM = "—"
# A high-confidence secret the output scanner flags (api_key_openai -> critical).
SECRET = "sk-abcdefghij1234567890ABCDXYZ"


class TestShouldGate:
    def test_external_channels_gate(self):
        assert should_gate("email", None) is True
        assert should_gate("discord", "alert") is True

    def test_content_category_gates_any_channel(self):
        assert should_gate("telegram", "content") is True

    def test_user_facing_channels_skip(self):
        assert should_gate("telegram", "notification") is False
        assert should_gate("voice", "alert") is False
        assert should_gate("telegram", None) is False


class TestGateFiring:
    def test_skips_user_facing_unchanged(self):
        text = f"hey, just you and me {EM} no gate here"
        r = gate(text, channel="telegram", category="notification")
        assert r.applied is False
        assert r.text == text  # untouched
        assert r.scan is None

    def test_em_dash_fixed_on_email(self):
        r = gate(f"Hi there {EM} quick note.", channel="email", category="surplus")
        assert r.applied is True
        assert r.text == "Hi there—quick note."
        assert r.fixes_applied == ["spaced_em_dash:1"]

    def test_em_dash_fixed_on_discord(self):
        r = gate(f"ship it {EM} now", channel="discord", category="notification")
        assert r.applied is True
        assert "—" in r.text and f" {EM} " not in r.text

    def test_content_draft_to_telegram_is_scrubbed(self):
        # The Medium drafting-side check: a CONTENT review copy on telegram
        # still gets anti-slop scrubbed even though telegram is user-facing.
        r = gate(f"Draft body {EM} here", channel="telegram", category="content")
        assert r.applied is True
        assert r.text == "Draft body—here"


class TestPIIScan:
    def test_external_send_with_secret_quarantines(self):
        r = gate(f"key is {SECRET}", channel="email", category="surplus")
        assert r.applied is True
        assert r.scan is not None
        assert r.quarantined is True

    def test_discord_send_with_secret_quarantines(self):
        r = gate(f"token {SECRET}", channel="discord", category="content")
        assert r.quarantined is True

    def test_clean_external_send_not_quarantined(self):
        r = gate("totally clean message", channel="email", category="surplus")
        assert r.scan is not None
        assert r.quarantined is False

    def test_content_review_copy_not_pii_blocked(self):
        # A CONTENT draft headed to the user (telegram) is scrubbed for slop but
        # NOT PII-blocked — it's a review copy, not an external delivery.
        r = gate(f"draft mentions {SECRET}", channel="telegram", category="content")
        assert r.applied is True
        assert r.scan is None
        assert r.quarantined is False


class TestFlags:
    def test_banned_words_flagged_on_external(self):
        r = gate("We leverage robust synergy.", channel="email", category="surplus")
        assert any("banned_words" in f for f in r.flags)
        assert r.text == "We leverage robust synergy."  # not deleted


def test_egressresult_default_is_inert():
    r = EgressResult(text="x")
    assert r.applied is False
    assert r.quarantined is False
