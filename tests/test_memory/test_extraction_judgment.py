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
