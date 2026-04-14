"""Binary PASS/FAIL scorers for eval harness.

All scorers return (passed: bool, score: float, detail: str).
Score is always 1.0 (pass) or 0.0 (fail) — no partial credit.
Zero cost: no LLM-as-judge.
"""

from __future__ import annotations

import json
from typing import Any

from genesis.eval.types import ScorerType

# Common LLM slop phrases that indicate low-quality output
_SLOP_PHRASES: list[str] = [
    "as an ai",
    "as a large language model",
    "i cannot and will not",
    "i'm happy to help",
    "great question",
    "certainly!",
    "absolutely!",
    "of course!",
    "let me think about",
    "here's what i think",
    "it's important to note",
    "it's worth noting",
    "in conclusion",
    "delve into",
    "navigating the",
    "tapestry of",
    "landscape of",
    "it is important to remember",
    "based on my training",
]

# Scorer registry
_SCORERS: dict[ScorerType, type[Scorer]] = {}


class Scorer:
    """Base scorer interface."""

    scorer_type: ScorerType

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if hasattr(cls, "scorer_type"):
            _SCORERS[cls.scorer_type] = cls

    def score(
        self, actual: str, expected: str, config: dict | None = None,
    ) -> tuple[bool, float, str]:
        """Score actual vs expected. Returns (passed, score, detail)."""
        raise NotImplementedError


class ExactMatch(Scorer):
    """Case-insensitive exact match after stripping whitespace."""

    scorer_type = ScorerType.EXACT_MATCH

    def score(
        self, actual: str, expected: str, config: dict | None = None,
    ) -> tuple[bool, float, str]:
        cfg = config or {}
        normalize = cfg.get("normalize", True)
        a = actual.strip()
        e = expected.strip()
        if normalize:
            a = a.lower()
            e = e.lower()
        passed = a == e
        detail = "" if passed else f"expected={e!r}, got={a!r}"
        return passed, 1.0 if passed else 0.0, detail


class JsonFieldMatch(Scorer):
    """Parse JSON output and check specific field(s) match expected values.

    Config:
        fields: list of field names to check (dot notation for nested)
        expected_values: dict mapping field names to expected values
        -- OR --
        If no fields/expected_values, compares the parsed "field" key
        from expected (also parsed as JSON).
    """

    scorer_type = ScorerType.JSON_FIELD_MATCH

    def score(
        self, actual: str, expected: str, config: dict | None = None,
    ) -> tuple[bool, float, str]:
        cfg = config or {}
        try:
            actual_obj = json.loads(actual.strip())
        except (json.JSONDecodeError, ValueError) as e:
            return False, 0.0, f"actual is not valid JSON: {e}"

        fields = cfg.get("fields", [])
        expected_values = cfg.get("expected_values", {})

        if not fields and not expected_values:
            # Default: parse expected as JSON and compare all keys
            try:
                expected_obj = json.loads(expected.strip())
            except (json.JSONDecodeError, ValueError):
                # Expected is a plain string — compare against full output
                return False, 0.0, "expected is not JSON and no fields configured"
            mismatches = []
            for key, val in expected_obj.items():
                actual_val = _get_nested(actual_obj, key)
                if not _values_match(actual_val, val):
                    mismatches.append(f"{key}: expected={val!r}, got={actual_val!r}")
            passed = len(mismatches) == 0
            detail = "; ".join(mismatches) if mismatches else ""
            return passed, 1.0 if passed else 0.0, detail

        # Check specified fields against expected_values
        mismatches = []
        for field_name in fields:
            actual_val = _get_nested(actual_obj, field_name)
            expected_val = expected_values.get(field_name)
            if expected_val is not None and not _values_match(actual_val, expected_val):
                mismatches.append(
                    f"{field_name}: expected={expected_val!r}, got={actual_val!r}"
                )
        passed = len(mismatches) == 0
        detail = "; ".join(mismatches) if mismatches else ""
        return passed, 1.0 if passed else 0.0, detail


class SetOverlap(Scorer):
    """Check that output contains required items (order-independent).

    Expected format: comma-separated or JSON list.
    Pass if all expected items appear in actual output.
    """

    scorer_type = ScorerType.SET_OVERLAP

    def score(
        self, actual: str, expected: str, config: dict | None = None,
    ) -> tuple[bool, float, str]:
        expected_items = _parse_set(expected)
        actual_items = _parse_set(actual)
        missing = expected_items - actual_items
        passed = len(missing) == 0
        detail = f"missing: {missing}" if missing else ""
        return passed, 1.0 if passed else 0.0, detail


class JsonValidity(Scorer):
    """Check that output is valid JSON. Expected is ignored."""

    scorer_type = ScorerType.JSON_VALIDITY

    def score(
        self, actual: str, expected: str, config: dict | None = None,
    ) -> tuple[bool, float, str]:
        cfg = config or {}
        try:
            obj = json.loads(actual.strip())
        except (json.JSONDecodeError, ValueError) as e:
            return False, 0.0, f"invalid JSON: {e}"

        # Optional: check required keys
        required_keys = cfg.get("required_keys", [])
        if required_keys and isinstance(obj, dict):
            missing = [k for k in required_keys if k not in obj]
            if missing:
                return False, 0.0, f"missing required keys: {missing}"

        return True, 1.0, ""


class SlopDetection(Scorer):
    """Check that output does NOT contain common LLM slop phrases.

    This is an inverse scorer — passing means no slop detected.
    Expected is ignored.
    """

    scorer_type = ScorerType.SLOP_DETECTION

    def score(
        self, actual: str, expected: str, config: dict | None = None,
    ) -> tuple[bool, float, str]:
        cfg = config or {}
        extra_phrases = cfg.get("extra_phrases", [])
        phrases = _SLOP_PHRASES + extra_phrases
        lower = actual.lower()
        found = [p for p in phrases if p in lower]
        passed = len(found) == 0
        detail = f"slop detected: {found}" if found else ""
        return passed, 1.0 if passed else 0.0, detail


def get_scorer(scorer_type: ScorerType) -> Scorer:
    """Get a scorer instance by type."""
    cls = _SCORERS.get(scorer_type)
    if cls is None:
        raise ValueError(f"unknown scorer type: {scorer_type}")
    return cls()


# -- Helpers --

def _get_nested(obj: Any, path: str) -> Any:
    """Get nested value by dot-notation path."""
    parts = path.split(".")
    current = obj
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _values_match(actual: Any, expected: Any) -> bool:
    """Flexible value comparison: case-insensitive for strings."""
    if isinstance(actual, str) and isinstance(expected, str):
        return actual.strip().lower() == expected.strip().lower()
    return actual == expected


def _parse_set(text: str) -> set[str]:
    """Parse a comma-separated or JSON list into a lowercase set."""
    text = text.strip()
    try:
        items = json.loads(text)
        if isinstance(items, list):
            return {str(i).strip().lower() for i in items}
    except (json.JSONDecodeError, ValueError):
        pass
    # Comma-separated fallback
    return {item.strip().lower() for item in text.split(",") if item.strip()}
