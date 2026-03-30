"""Tests for genesis.autonomy.executor.review.TaskReviewer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock

import pytest

from genesis.autonomy.executor.review import (
    TaskReviewer,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeRoutingResult:
    success: bool
    content: str | None = None
    error: str | None = None
    cost_usd: float = 0.0


def _make_router(
    response_content: str | None,
    success: bool = True,
) -> AsyncMock:
    """Create a mock router that returns the same response for all call sites."""
    router = AsyncMock()
    router.route_call = AsyncMock(
        return_value=FakeRoutingResult(success=success, content=response_content),
    )
    return router


def _make_multi_router(
    responses: dict[str, tuple[bool, str | None]],
) -> AsyncMock:
    """Router that returns different responses per call site ID.

    ``responses`` maps call-site ID to ``(success, content)`` tuples.
    Unrecognized call sites return a failure.
    """
    async def _route(call_site_id: str, messages: list, **kwargs):
        entry = responses.get(call_site_id)
        if entry is None:
            return FakeRoutingResult(success=False)
        return FakeRoutingResult(success=entry[0], content=entry[1])

    router = AsyncMock()
    router.route_call = AsyncMock(side_effect=_route)
    return router


# ---------------------------------------------------------------------------
# Plan review tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestReviewPlan:
    async def test_passes_clean_plan(self) -> None:
        body = json.dumps({"passed": True, "gaps": [], "recommendations": []})
        reviewer = TaskReviewer(router=_make_router(body))
        result = await reviewer.review_plan("# Plan\nDo things", "Build X")

        assert result.passed is True
        assert result.gaps == []

    async def test_identifies_gaps(self) -> None:
        body = json.dumps({
            "passed": False,
            "gaps": ["Missing success criteria", "No test plan"],
            "recommendations": ["Add tests"],
        })
        reviewer = TaskReviewer(router=_make_router(body))
        result = await reviewer.review_plan("# Plan\nDo things", "Build X")

        assert result.passed is False
        assert len(result.gaps) == 2
        assert "Missing success criteria" in result.gaps

    async def test_routing_failure_passthrough(self) -> None:
        reviewer = TaskReviewer(router=_make_router(None, success=False))
        result = await reviewer.review_plan("# Plan", "task")

        # Should pass through rather than blocking
        assert result.passed is True

    async def test_routing_exception_passthrough(self) -> None:
        router = AsyncMock()
        router.route_call = AsyncMock(side_effect=RuntimeError("network"))
        reviewer = TaskReviewer(router=router)
        result = await reviewer.review_plan("# Plan", "task")

        assert result.passed is True

    async def test_unparseable_response_passes(self) -> None:
        reviewer = TaskReviewer(router=_make_router("This is not JSON at all"))
        result = await reviewer.review_plan("# Plan", "task")

        assert result.passed is True
        # Raw snippet should be in recommendations
        assert len(result.recommendations) == 1
        assert "This is not JSON" in result.recommendations[0]

    async def test_code_fenced_json_parsed(self) -> None:
        body = '```json\n{"passed": false, "gaps": ["gap1"]}\n```'
        reviewer = TaskReviewer(router=_make_router(body))
        result = await reviewer.review_plan("plan", "task")

        assert result.passed is False
        assert result.gaps == ["gap1"]


# ---------------------------------------------------------------------------
# Verify deliverable tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestVerifyDeliverable:
    async def test_passes_both_gates(self) -> None:
        pass_json = json.dumps({"verdict": "pass", "issues": [], "feedback": "OK"})
        reviewer = TaskReviewer(router=_make_router(pass_json))
        result = await reviewer.verify_deliverable(
            "def foo():\n    return 42\n# done",
            "Write a function",
        )

        assert result.passed is True
        assert result.programmatic_issues == []
        assert result.skipped_reason is None

    async def test_fails_programmatic_empty(self) -> None:
        router = _make_router("should not be called")
        reviewer = TaskReviewer(router=router)
        result = await reviewer.verify_deliverable("", "Write something")

        assert result.passed is False
        assert "empty" in result.programmatic_issues[0].lower()
        # LLM should not have been called for review sites
        calls = router.route_call.call_args_list
        for call in calls:
            site = call[0][0]
            assert site not in ("17_fresh_eyes_review", "20_adversarial_counterargument")

    async def test_fails_programmatic_short_code(self) -> None:
        reviewer = TaskReviewer(router=_make_router("ignored"))
        result = await reviewer.verify_deliverable(
            "x = 1\ny = 2",
            "Write a module",
            task_type="code",
        )

        assert result.passed is False
        assert any("suspiciously short" in i for i in result.programmatic_issues)

    async def test_short_non_code_passes_programmatic(self) -> None:
        """Short deliverable for non-code task types should NOT fail programmatic check."""
        pass_json = json.dumps({"verdict": "pass"})
        reviewer = TaskReviewer(router=_make_router(pass_json))
        result = await reviewer.verify_deliverable(
            "Analysis: yes",
            "Analyze the data",
            task_type="research",
        )

        assert result.passed is True
        assert result.programmatic_issues == []

    async def test_fails_fresh_eyes(self) -> None:
        responses = {
            "17_fresh_eyes_review": (True, json.dumps({"verdict": "fail", "issues": ["bad"]})),
            "20_adversarial_counterargument": (True, json.dumps({"verdict": "pass"})),
        }
        reviewer = TaskReviewer(router=_make_multi_router(responses))
        result = await reviewer.verify_deliverable(
            "def foo():\n    return 42\n# end",
            "Write a function",
        )

        assert result.passed is False

    async def test_fails_adversarial(self) -> None:
        responses = {
            "17_fresh_eyes_review": (True, json.dumps({"verdict": "pass"})),
            "20_adversarial_counterargument": (True, json.dumps({"verdict": "fail", "issues": ["flaw"]})),
        }
        reviewer = TaskReviewer(router=_make_multi_router(responses))
        result = await reviewer.verify_deliverable(
            "def foo():\n    return 42\n# end",
            "Write a function",
        )

        assert result.passed is False

    async def test_amendment5_both_fail_skip_warning(self) -> None:
        """Amendment #5: both routing fail -> deliver with warning."""
        responses = {
            "17_fresh_eyes_review": (False, None),
            "20_adversarial_counterargument": (False, None),
        }
        reviewer = TaskReviewer(router=_make_multi_router(responses))
        result = await reviewer.verify_deliverable(
            "def foo():\n    return 42\n# end",
            "Write a function",
        )

        assert result.passed is True
        assert result.skipped_reason is not None
        assert "cross-vendor" in result.skipped_reason.lower()

    async def test_one_routing_fails_other_succeeds(self) -> None:
        responses = {
            "17_fresh_eyes_review": (False, None),
            "20_adversarial_counterargument": (True, json.dumps({"verdict": "pass"})),
        }
        reviewer = TaskReviewer(router=_make_multi_router(responses))
        result = await reviewer.verify_deliverable(
            "def foo():\n    return 42\n# end",
            "Write a function",
        )

        assert result.passed is True
        assert result.fresh_eyes_feedback is None
        assert result.adversarial_feedback is not None

    async def test_iteration_field_passthrough(self) -> None:
        pass_json = json.dumps({"verdict": "pass"})
        reviewer = TaskReviewer(router=_make_router(pass_json))
        result = await reviewer.verify_deliverable(
            "def foo():\n    return 42\n# end",
            "Write a function",
            iteration=3,
        )

        assert result.iteration == 3


# ---------------------------------------------------------------------------
# Feedback assessment tests
# ---------------------------------------------------------------------------


class TestFeedbackAssessment:
    def test_json_pass_verdict(self) -> None:
        reviewer = TaskReviewer(router=AsyncMock())
        assert reviewer._assess_feedback(
            json.dumps({"verdict": "pass"}), None,
        ) is True

    def test_json_fail_verdict(self) -> None:
        reviewer = TaskReviewer(router=AsyncMock())
        assert reviewer._assess_feedback(
            json.dumps({"verdict": "fail", "issues": ["bad"]}), None,
        ) is False

    def test_code_fenced_json(self) -> None:
        reviewer = TaskReviewer(router=AsyncMock())
        fenced = '```json\n{"verdict": "fail"}\n```'
        assert reviewer._assess_feedback(fenced, None) is False

    def test_keyword_heuristic_fail(self) -> None:
        reviewer = TaskReviewer(router=AsyncMock())
        # Non-JSON with the verdict pattern
        text = 'The result has "verdict": "fail" because of issues'
        assert reviewer._assess_feedback(text, None) is False

    def test_no_signal_passes(self) -> None:
        reviewer = TaskReviewer(router=AsyncMock())
        assert reviewer._assess_feedback(
            "Looks good overall, nice work", None,
        ) is True

    def test_both_none_passes(self) -> None:
        reviewer = TaskReviewer(router=AsyncMock())
        assert reviewer._assess_feedback(None, None) is True

    def test_adversarial_fail_overrides_fresh_pass(self) -> None:
        reviewer = TaskReviewer(router=AsyncMock())
        assert reviewer._assess_feedback(
            json.dumps({"verdict": "pass"}),
            json.dumps({"verdict": "fail"}),
        ) is False
