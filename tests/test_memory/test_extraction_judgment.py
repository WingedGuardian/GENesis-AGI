"""MW-1 — Tier-0 extraction judgment fields (parse + kwargs + constants).

Covers the WRITE side of the three judgment axes (provenance / speech_act /
durability) as they ride the extraction LLM call: the parser must extract them,
default SAFE when absent/garbage, and thread them into store() kwargs.
"""

from __future__ import annotations

import json

import pytest

from genesis.memory import judgment
from genesis.memory.extraction import (
    Extraction,
    extractions_to_store_kwargs,
    parse_extraction_response,
)


def _wrap(item: dict) -> str:
    return "```json\n" + json.dumps({"extractions": [item]}) + "\n```"


class TestJudgmentConstants:
    def test_protected_is_subset_of_speech_acts(self):
        assert judgment.PROTECTED_SPEECH_ACTS <= judgment.SPEECH_ACTS

    def test_preference_is_not_protected(self):
        # Preferences get superseded; the identity layer owns standing prefs.
        assert "preference" not in judgment.PROTECTED_SPEECH_ACTS
        assert {"rule", "decision", "correction"} == judgment.PROTECTED_SPEECH_ACTS

    def test_protection_confidence_floor(self):
        assert judgment.PROTECTION_CONFIDENCE_FLOOR == 0.7

    def test_defaults(self):
        assert judgment.DEFAULT_SPEECH_ACT == "observation"
        assert judgment.DEFAULT_DURABILITY == "permanent"


class TestJudgmentNormalizers:
    @pytest.mark.parametrize("value", sorted(judgment.SPEECH_ACTS))
    def test_normalize_speech_act_passthrough(self, value):
        assert judgment.normalize_speech_act(value) == value

    @pytest.mark.parametrize("value", ["", "blah", None, 3, "Rule"])
    def test_normalize_speech_act_unknown_defaults_observation(self, value):
        assert judgment.normalize_speech_act(value) == "observation"

    @pytest.mark.parametrize("value", ["", "forever", None, "PERMANENT"])
    def test_normalize_durability_unknown_defaults_permanent(self, value):
        assert judgment.normalize_durability(value) == "permanent"

    @pytest.mark.parametrize("value", ["user", "external", "self_inference"])
    def test_normalize_provenance_passthrough(self, value):
        assert judgment.normalize_provenance(value) == value

    @pytest.mark.parametrize("value", ["", "robot", None, "User"])
    def test_normalize_provenance_unknown_is_none(self, value):
        assert judgment.normalize_provenance(value) is None

    def test_clamp_unit(self):
        assert judgment.clamp_unit(1.5) == 1.0
        assert judgment.clamp_unit(-0.2) == 0.0
        assert judgment.clamp_unit(0.42) == 0.42
        assert judgment.clamp_unit("x") == 0.5
        assert judgment.clamp_unit(True) == 0.5  # bool is not a confidence


class TestParseJudgmentFields:
    def test_extracts_all_five(self):
        item = {
            "content": "Never push private data to public repos",
            "type": "decision",
            "confidence": 0.9,
            "speech_act": "rule",
            "speech_act_confidence": 0.95,
            "provenance": "user",
            "durability": "permanent",
            "expires_at": None,
        }
        result = parse_extraction_response(_wrap(item))
        assert len(result) == 1
        ext = result[0]
        assert ext.speech_act == "rule"
        assert ext.speech_act_confidence == 0.95
        assert ext.assertion_provenance == "user"
        assert ext.durability == "permanent"
        assert ext.expires_at is None

    def test_temporary_with_expiry(self):
        item = {
            "content": "Waiting on CI run for PR #1323 to go green",
            "type": "action_item",
            "confidence": 0.8,
            "speech_act": "observation",
            "durability": "temporary",
            "expires_at": "2026-08-07",
        }
        ext = parse_extraction_response(_wrap(item))[0]
        assert ext.durability == "temporary"
        assert ext.expires_at == "2026-08-07"

    def test_defaults_when_absent(self):
        item = {"content": "Some fact", "type": "entity", "confidence": 0.7}
        ext = parse_extraction_response(_wrap(item))[0]
        assert ext.speech_act == "observation"
        assert ext.assertion_provenance is None
        assert ext.durability == "permanent"
        assert ext.expires_at is None

    def test_invalid_enums_default_safe(self):
        item = {
            "content": "x",
            "type": "entity",
            "confidence": 0.5,
            "speech_act": "shouting",
            "provenance": "robot",
            "durability": "eternal",
        }
        ext = parse_extraction_response(_wrap(item))[0]
        assert ext.speech_act == "observation"
        assert ext.assertion_provenance is None
        assert ext.durability == "permanent"

    def test_clamps_speech_act_confidence(self):
        item = {
            "content": "x",
            "type": "entity",
            "confidence": 0.5,
            "speech_act": "rule",
            "speech_act_confidence": 1.7,
        }
        ext = parse_extraction_response(_wrap(item))[0]
        assert ext.speech_act_confidence == 1.0


class TestKwargsThreading:
    def test_kwargs_include_judgment_fields(self):
        ext = Extraction(
            content="Never push private data to public repos",
            extraction_type="decision",
            confidence=0.9,
            speech_act="rule",
            speech_act_confidence=0.95,
            assertion_provenance="user",
            durability="permanent",
            expires_at=None,
        )
        kwargs = extractions_to_store_kwargs(ext)
        assert kwargs["speech_act"] == "rule"
        assert kwargs["speech_act_confidence"] == 0.95
        assert kwargs["assertion_provenance"] == "user"
        assert kwargs["durability"] == "permanent"
        assert kwargs["expires_at"] is None


class TestPreferenceDomain:
    """MW-4 satellite (2026-09-06, from the Clarity exchange): a preference is
    not a scalar fact — 'favorite color' differs by domain (work vs this-month
    vs vehicles). Capturing the domain at extraction time lets a detected
    conflict dissolve into two coexisting domain-scoped statements instead of
    newest-wins. Write-only until MW-4 ranking consumes it."""

    def test_normalize_lowercases_and_strips(self):
        assert (
            judgment.normalize_preference_domain("  Work  ", speech_act="preference")
            == "work"
        )

    def test_normalize_collapses_inner_whitespace(self):
        assert (
            judgment.normalize_preference_domain("home  audio", speech_act="preference")
            == "home audio"
        )

    @pytest.mark.parametrize("value", ["", "   ", None, 7, ["work"]])
    def test_normalize_garbage_is_none(self, value):
        assert judgment.normalize_preference_domain(value, speech_act="preference") is None

    def test_normalize_dropped_for_non_preference_acts(self):
        # The domain qualifies a PREFERENCE; a stray domain on a claim is noise,
        # not signal — dropped at the write path so the column stays meaningful.
        assert judgment.normalize_preference_domain("work", speech_act="claim") is None

    def test_parse_carries_domain_on_preference(self):
        item = {
            "content": "I prefer red for work materials",
            "type": "preference",
            "confidence": 0.85,
            "speech_act": "preference",
            "preference_domain": "Work",
        }
        ext = parse_extraction_response(_wrap(item))[0]
        assert ext.preference_domain == "work"

    def test_parse_absent_is_none(self):
        item = {
            "content": "I prefer tea",
            "type": "preference",
            "confidence": 0.8,
            "speech_act": "preference",
        }
        assert parse_extraction_response(_wrap(item))[0].preference_domain is None

    def test_parse_drops_domain_on_non_preference(self):
        item = {
            "content": "The deploy failed twice",
            "type": "entity",
            "confidence": 0.8,
            "speech_act": "observation",
            "preference_domain": "work",
        }
        assert parse_extraction_response(_wrap(item))[0].preference_domain is None

    def test_kwargs_include_preference_domain(self):
        ext = Extraction(
            content="I prefer red for work materials",
            extraction_type="preference",
            confidence=0.85,
            speech_act="preference",
            preference_domain="work",
        )
        assert extractions_to_store_kwargs(ext)["preference_domain"] == "work"

    def test_prompt_carries_the_instruction_not_just_the_example(self):
        """Asserting the bare token passes on the JSON example alone — deleting
        the whole instruction bullet would leave it green. Anchor on the bullet."""
        from genesis.memory.extraction import EXTRACTION_PROMPT

        assert 'for speech_act="preference" only' in EXTRACTION_PROMPT
        assert '"preference_domain": "work"' in EXTRACTION_PROMPT  # positive example

    def test_capitalized_speech_act_normalizes_away_and_drops_the_domain(self):
        """ACCEPTED BEHAVIOR, locked deliberately: normalize_speech_act is an
        EXACT-match membership test (MW-1), so "Preference" becomes
        "observation" and the domain is dropped with it. Not widened here on
        purpose — case-folding would also promote "Rule"/"Decision" into
        PROTECTED_SPEECH_ACTS, a protection-eligibility change that belongs to
        MW-1/MW-5, not to this satellite."""
        assert judgment.normalize_speech_act("Preference") == "observation"
        item = {
            "content": "I prefer red for work",
            "type": "preference",
            "confidence": 0.8,
            "speech_act": "Preference",
            "preference_domain": "work",
        }
        assert parse_extraction_response(_wrap(item))[0].preference_domain is None
