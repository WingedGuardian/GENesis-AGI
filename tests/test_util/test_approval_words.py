"""Tests for the canonical approve/reject vocabulary."""

from __future__ import annotations

from genesis.util.approval_words import (
    APPROVE_PHRASES,
    APPROVE_TOKENS,
    REJECT_PHRASES,
    REJECT_TOKENS,
    leading_token_decision,
    normalize,
    phrase_decision,
    scoped_decision,
)


class TestNormalize:
    def test_lowercase_collapse_strip(self):
        assert normalize("  Ok   Sounds  GOOD!! ") == "ok sounds good"

    def test_empty(self):
        assert normalize("") == ""
        assert normalize(None) == ""


class TestPhraseDecision:
    def test_multiword_phrases(self):
        assert phrase_decision("go for it") == "approved"
        assert phrase_decision("Sounds good.") == "approved"
        assert phrase_decision("not now") == "rejected"
        assert phrase_decision("hold off") == "rejected"

    def test_emoji(self):
        assert phrase_decision("\U0001f44d") == "approved"
        assert phrase_decision("❌") == "rejected"

    def test_longer_message_is_none(self):
        assert phrase_decision("sounds good but rename the flag") is None


class TestLeadingTokenDecision:
    def test_leading_approve(self):
        assert leading_token_decision("Ok sounds good, ship it") == "approved"
        assert leading_token_decision("approve — and rename the flag") == "approved"

    def test_leading_reject(self):
        assert leading_token_decision("No, this duplicates recon") == "rejected"

    def test_non_decision_opener(self):
        assert leading_token_decision("please approve it") is None
        assert leading_token_decision("what does this do?") is None


class TestScopedDecision:
    def test_phrase_wins_over_token(self):
        # "not now" has no decision leading token ("not" isn't one) but is
        # a known phrase — scoped_decision must catch it.
        assert scoped_decision("not now") == "rejected"

    def test_falls_back_to_leading_token(self):
        assert scoped_decision("yes, with the caveat noted") == "approved"


def test_no_word_in_both_camps():
    assert not (APPROVE_TOKENS & REJECT_TOKENS)
    assert not (APPROVE_PHRASES & REJECT_PHRASES)


def test_tokens_subset_expected_by_gate():
    # The gate's historical words must all still be present — unification
    # may only ADD vocabulary, never silently drop it.
    assert {"approve", "approved", "ok", "yes", "go", "lgtm"} <= APPROVE_TOKENS
    assert {"reject", "rejected", "deny", "denied", "no", "nope"} <= REJECT_TOKENS
