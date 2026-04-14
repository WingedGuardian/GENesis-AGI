"""Tests for eval scorers."""

from __future__ import annotations

import json

import pytest

from genesis.eval.scorers import (
    ExactMatch,
    JsonFieldMatch,
    JsonValidity,
    SetOverlap,
    SlopDetection,
    get_scorer,
)
from genesis.eval.types import ScorerType


class TestExactMatch:
    def test_exact_match_pass(self):
        s = ExactMatch()
        passed, score, detail = s.score("3", "3")
        assert passed is True
        assert score == 1.0

    def test_exact_match_case_insensitive(self):
        s = ExactMatch()
        passed, _, _ = s.score("HELLO", "hello")
        assert passed is True

    def test_exact_match_strips_whitespace(self):
        s = ExactMatch()
        passed, _, _ = s.score("  3\n", "3")
        assert passed is True

    def test_exact_match_fail(self):
        s = ExactMatch()
        passed, score, detail = s.score("2", "3")
        assert passed is False
        assert score == 0.0
        assert "expected" in detail

    def test_exact_match_case_sensitive(self):
        s = ExactMatch()
        passed, _, _ = s.score("HELLO", "hello", {"normalize": False})
        assert passed is False


class TestJsonFieldMatch:
    def test_json_field_match_pass(self):
        s = JsonFieldMatch()
        actual = json.dumps({"depth": 3, "reason": "complex"})
        expected = json.dumps({"depth": 3})
        passed, _, _ = s.score(actual, expected)
        assert passed is True

    def test_json_field_match_fail(self):
        s = JsonFieldMatch()
        actual = json.dumps({"depth": 2})
        expected = json.dumps({"depth": 3})
        passed, _, detail = s.score(actual, expected)
        assert passed is False
        assert "depth" in detail

    def test_json_field_match_nested(self):
        s = JsonFieldMatch()
        actual = json.dumps({"result": {"depth": 4}})
        config = {"fields": ["result.depth"], "expected_values": {"result.depth": 4}}
        passed, _, _ = s.score(actual, "", config)
        assert passed is True

    def test_json_field_match_invalid_json(self):
        s = JsonFieldMatch()
        passed, _, detail = s.score("not json", "{}")
        assert passed is False
        assert "not valid JSON" in detail


class TestSetOverlap:
    def test_set_overlap_pass(self):
        s = SetOverlap()
        passed, _, _ = s.score("a, b, c, d", "a, b, c")
        assert passed is True

    def test_set_overlap_missing(self):
        s = SetOverlap()
        passed, _, detail = s.score("a, b", "a, b, c")
        assert passed is False
        assert "missing" in detail

    def test_set_overlap_json_list(self):
        s = SetOverlap()
        passed, _, _ = s.score(json.dumps(["x", "y"]), json.dumps(["x", "y"]))
        assert passed is True


class TestJsonValidity:
    def test_valid_json(self):
        s = JsonValidity()
        passed, _, _ = s.score('{"key": "value"}', "")
        assert passed is True

    def test_invalid_json(self):
        s = JsonValidity()
        passed, _, detail = s.score("not json", "")
        assert passed is False
        assert "invalid JSON" in detail

    def test_required_keys(self):
        s = JsonValidity()
        passed, _, _ = s.score('{"a": 1}', "", {"required_keys": ["a"]})
        assert passed is True

    def test_missing_required_keys(self):
        s = JsonValidity()
        passed, _, detail = s.score('{"a": 1}', "", {"required_keys": ["b"]})
        assert passed is False
        assert "missing required keys" in detail


class TestSlopDetection:
    def test_no_slop(self):
        s = SlopDetection()
        passed, _, _ = s.score("The depth classification is 3.", "")
        assert passed is True

    def test_slop_detected(self):
        s = SlopDetection()
        passed, _, detail = s.score(
            "Great question! Let me delve into this.", ""
        )
        assert passed is False
        assert "slop detected" in detail

    def test_extra_phrases(self):
        s = SlopDetection()
        passed, _, _ = s.score(
            "The answer is foobar", "",
            {"extra_phrases": ["foobar"]},
        )
        assert passed is False


class TestGetScorer:
    def test_all_types_registered(self):
        for st in ScorerType:
            scorer = get_scorer(st)
            assert scorer is not None

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="unknown scorer"):
            get_scorer("nonexistent")
