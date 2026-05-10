"""Scorers for the eval harness.

All scorers return (passed: bool, score: float, detail: str).

Most scorers are deterministic: they compare actual vs expected without
calling out to an LLM. ``LLMJudgeScorer`` is the exception — it grades
free-form output against a versioned rubric via the ``judge`` call site
in ``config/model_routing.yaml``. It is async-only; the eval runner
detects ``score_async`` and dispatches accordingly.
"""

from __future__ import annotations

import json
import logging
import math
from typing import TYPE_CHECKING, Any

from genesis.eval.types import ScorerType

if TYPE_CHECKING:
    from genesis.eval.rubrics import Rubric
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

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
        extracted = _extract_json(actual)
        try:
            actual_obj = json.loads(extracted)
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
        # Every field listed in `fields` must appear in expected_values — a missing
        # entry is a dataset config bug that would silently produce false passes.
        mismatches = []
        for field_name in fields:
            if field_name not in expected_values:
                return False, 0.0, f"config error: field '{field_name}' in fields list but not in expected_values"
            actual_val = _get_nested(actual_obj, field_name)
            expected_val = expected_values[field_name]
            if not _values_match(actual_val, expected_val):
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
        extracted = _extract_json(actual)
        try:
            obj = json.loads(extracted)
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
    """Get a scorer instance by type.

    Note: ``LLMJudgeScorer`` returned by this helper has no router
    attached — the caller MUST attach one via ``set_router()`` before
    calling ``score_async()`` or it will raise. The registry pattern
    is sync and can't accept constructor args; the runner is responsible
    for wiring the router in.
    """
    cls = _SCORERS.get(scorer_type)
    if cls is None:
        raise ValueError(f"unknown scorer type: {scorer_type}")
    return cls()


# Sentinel for unparseable / failed judge responses. Packed into the
# scorer_detail JSON so downstream tooling (calibration aggregator, the
# eval results MCP) can distinguish "judge said the case fails" from
# "judge call broke."
_JUDGE_PARSE_FAIL = "judge_parse_fail"
_JUDGE_CALL_FAIL = "judge_call_fail"


class LLMJudgeScorer(Scorer):
    """Async LLM-as-judge scorer.

    Grades ``actual`` against ``expected`` using a versioned rubric
    looked up from the registry via ``config["rubric_name"]``. The judge
    call goes through the ``judge`` call site in
    ``config/model_routing.yaml`` (so cost is tracked, the chain
    fallback applies, and the circuit breaker integrates) — never via
    ``litellm.acompletion`` directly. That contract is what gives us
    observability for free.

    This scorer is async-only. ``score()`` (the sync base method) raises
    ``NotImplementedError``. The runner detects ``score_async`` on the
    instance and dispatches accordingly.

    Construction:
        ``LLMJudgeScorer(router=...)`` — pass the runtime ``Router``.
        ``LLMJudgeScorer()`` is permitted (the registry needs a no-arg
        constructor) but ``score_async`` will raise ``RuntimeError``
        unless a router is attached via ``set_router()`` first.

    Returns ``(passed, score, detail)`` where ``detail`` is a JSON
    string with shape::

        {"rubric_name": str, "rubric_version": str,
         "judge_model": str, "judge_score": float,
         "rationale": str, "raw_response": str}

    On call failure, ``passed=False, score=0.0`` and ``detail`` carries
    a sentinel (``judge_parse_fail`` / ``judge_call_fail``) plus the
    error message so calibration can distinguish "judge disagrees" from
    "judge broken."
    """

    scorer_type = ScorerType.LLM_JUDGE

    def __init__(self, *, router: Router | None = None) -> None:
        self._router = router

    def set_router(self, router: Router) -> None:
        """Attach a router after construction. Used by the runner when
        the scorer was created via ``get_scorer()`` (which can't pass
        constructor args).
        """
        self._router = router

    def score(
        self, actual: str, expected: str, config: dict | None = None,
    ) -> tuple[bool, float, str]:
        msg = (
            "LLMJudgeScorer is async-only — call score_async(). The eval "
            "runner detects score_async and dispatches it; sync runners "
            "cannot use this scorer."
        )
        raise NotImplementedError(msg)

    async def score_async(
        self, actual: str, expected: str, config: dict | None = None,
    ) -> tuple[bool, float, str]:
        from genesis.eval.rubrics import get_rubric

        if self._router is None:
            msg = (
                "LLMJudgeScorer.score_async called with no router attached. "
                "Pass router= at construction or call set_router() first."
            )
            raise RuntimeError(msg)

        cfg = config or {}
        rubric_name = cfg.get("rubric_name")
        if not rubric_name:
            msg = "LLMJudgeScorer requires config['rubric_name']"
            raise ValueError(msg)

        rubric: Rubric = get_rubric(rubric_name)

        # Build prompt — base placeholders + any rubric-declared extras
        # pulled from scorer_config. Missing extras are an explicit error
        # rather than silent KeyError from str.format.
        format_kwargs = {"actual": actual, "expected": expected}
        for placeholder in rubric.extra_placeholders:
            if placeholder not in cfg:
                msg = (
                    f"rubric {rubric.name!r} declares extra placeholder "
                    f"{placeholder!r} but it is missing from scorer_config"
                )
                raise ValueError(msg)
            format_kwargs[placeholder] = cfg[placeholder]

        prompt = rubric.prompt_template.format(**format_kwargs)
        messages = [{"role": "user", "content": prompt}]

        result = await self._router.route_call(
            call_site_id="judge",
            messages=messages,
            temperature=0.0,
        )

        if not result.success:
            detail = json.dumps({
                "rubric_name": rubric.name,
                "rubric_version": rubric.version,
                "error": _JUDGE_CALL_FAIL,
                "error_message": result.error or "unknown",
            })
            return False, 0.0, detail

        raw = result.content or ""
        judge_model = result.model_id or result.provider_used or "unknown"

        # Parse JSON tolerantly — borrow the existing _extract_json
        # helper that handles markdown fences and prose-wrapped JSON.
        extracted = _extract_json(raw)
        try:
            parsed = json.loads(extracted)
            score_val = float(parsed.get("score", 0.0))
            rationale = str(parsed.get("rationale", ""))
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.debug(
                "LLMJudgeScorer parse failure on rubric %s: %s; raw=%r",
                rubric.name, exc, raw[:200],
            )
            detail = json.dumps({
                "rubric_name": rubric.name,
                "rubric_version": rubric.version,
                "judge_model": judge_model,
                "error": _JUDGE_PARSE_FAIL,
                "error_message": str(exc),
                "raw_response": raw[:1000],
            })
            return False, 0.0, detail

        # Reject non-finite scores BEFORE clamping. NaN survives min/max
        # (NaN propagates), and ±inf would clamp silently. Either is a
        # malformed judge response, so treat it like a parse failure.
        if not math.isfinite(score_val):
            logger.debug(
                "LLMJudgeScorer rejected non-finite score on rubric %s: %r",
                rubric.name, score_val,
            )
            detail = json.dumps({
                "rubric_name": rubric.name,
                "rubric_version": rubric.version,
                "judge_model": judge_model,
                "error": _JUDGE_PARSE_FAIL,
                "error_message": f"non-finite score: {score_val!r}",
                "raw_response": raw[:1000],
            })
            return False, 0.0, detail

        # Clamp to [0, 1] — defends against models returning 1.5 or -0.2.
        score_val = max(0.0, min(1.0, score_val))
        passed = score_val >= rubric.pass_threshold

        detail = json.dumps({
            "rubric_name": rubric.name,
            "rubric_version": rubric.version,
            "judge_model": judge_model,
            "judge_score": score_val,
            "rationale": rationale,
            "raw_response": raw[:1000],
        })
        return passed, score_val, detail


# -- Helpers --

def _extract_json(text: str) -> str:
    """Extract JSON from text that may contain markdown fences or prose.

    LLMs often wrap JSON in ```json ... ``` blocks or add explanatory text.
    This extracts the JSON content for scoring.
    """
    import re
    text = text.strip()
    # Try direct parse first
    try:
        json.loads(text)
        return text
    except (json.JSONDecodeError, ValueError):
        pass
    # Strip markdown code fences
    m = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Find first { or [ and match to last } or ]
    for start_char, end_char in [('{', '}'), ('[', ']')]:
        start = text.find(start_char)
        end = text.rfind(end_char)
        if start != -1 and end > start:
            candidate = text[start:end + 1]
            try:
                json.loads(candidate)
                return candidate
            except (json.JSONDecodeError, ValueError):
                pass
    return text


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
