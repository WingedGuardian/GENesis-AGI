"""Tests for genesis.autonomy.executor.review.TaskReviewer."""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

from genesis.autonomy.executor.review import (
    TaskReviewer,
)
from genesis.cc.types import CCOutput

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent real codex exec from running in tests.

    Tests that specifically test the codex path patch this themselves.
    """
    monkeypatch.setattr(
        "genesis.autonomy.executor.review.shutil.which",
        lambda name: None,
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
            assert site not in ("17_executor_review", "20_adversarial_counterargument")

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
            "17_executor_review": (True, json.dumps({"verdict": "fail", "issues": ["bad"]})),
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
            "17_executor_review": (True, json.dumps({"verdict": "pass"})),
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
            "17_executor_review": (False, None),
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
            "17_executor_review": (False, None),
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


# ---------------------------------------------------------------------------
# Tool-capable verification chain tests
# ---------------------------------------------------------------------------


def _codex_jsonl_output(text: str) -> bytes:
    """Build fake codex --json JSONL output containing an agent_message."""
    item = {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": text},
    }
    return (json.dumps(item) + "\n").encode("utf-8")


def _make_fake_cc_output(
    text: str = "", is_error: bool = False, error_message: str | None = None,
) -> CCOutput:
    return CCOutput(
        session_id="test-session",
        text=text,
        model_used="claude-sonnet-4-6",
        cost_usd=0.01,
        input_tokens=100,
        output_tokens=50,
        duration_ms=1000,
        exit_code=0 if not is_error else 1,
        is_error=is_error,
        error_message=error_message,
    )


@pytest.mark.asyncio
class TestVerifyViaCodex:
    async def test_codex_not_installed_returns_none(self) -> None:
        """When codex is not on PATH, _verify_via_codex returns None."""
        # _no_codex fixture already patches shutil.which -> None
        reviewer = TaskReviewer(router=AsyncMock())
        result = await reviewer._verify_via_codex("deliverable", "requirements")
        assert result is None

    async def test_codex_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When codex returns a verdict, parse the JSONL output."""
        monkeypatch.setattr(
            "genesis.autonomy.executor.review.shutil.which",
            lambda name: "/usr/bin/codex",
        )
        verdict_text = json.dumps({
            "verdict": "pass",
            "checks": [{"criterion": "file exists", "verified": True}],
        })

        fake_proc = AsyncMock()
        fake_proc.communicate = AsyncMock(
            return_value=(_codex_jsonl_output(verdict_text), b""),
        )
        fake_proc.returncode = 0

        with patch(
            "genesis.autonomy.executor.review.asyncio.create_subprocess_exec",
            return_value=fake_proc,
        ):
            reviewer = TaskReviewer(router=AsyncMock())
            result = await reviewer._verify_via_codex("deliverable", "reqs")

        assert result is not None
        assert "pass" in result

    async def test_codex_nonzero_exit_returns_none(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "genesis.autonomy.executor.review.shutil.which",
            lambda name: "/usr/bin/codex",
        )

        fake_proc = AsyncMock()
        fake_proc.communicate = AsyncMock(return_value=(b"", b"error"))
        fake_proc.returncode = 1

        with patch(
            "genesis.autonomy.executor.review.asyncio.create_subprocess_exec",
            return_value=fake_proc,
        ):
            reviewer = TaskReviewer(router=AsyncMock())
            result = await reviewer._verify_via_codex("deliverable", "reqs")

        assert result is None

    async def test_codex_empty_output_returns_none(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "genesis.autonomy.executor.review.shutil.which",
            lambda name: "/usr/bin/codex",
        )

        fake_proc = AsyncMock()
        fake_proc.communicate = AsyncMock(return_value=(b"{}\n", b""))
        fake_proc.returncode = 0

        with patch(
            "genesis.autonomy.executor.review.asyncio.create_subprocess_exec",
            return_value=fake_proc,
        ):
            reviewer = TaskReviewer(router=AsyncMock())
            result = await reviewer._verify_via_codex("deliverable", "reqs")

        assert result is None


@pytest.mark.asyncio
class TestVerifyViaInvoker:
    async def test_invoker_none_returns_none(self) -> None:
        reviewer = TaskReviewer(router=AsyncMock(), invoker=None)
        result = await reviewer._verify_via_invoker("deliverable", "reqs")
        assert result is None

    async def test_invoker_pass(self) -> None:
        invoker = AsyncMock()
        invoker.run = AsyncMock(
            return_value=_make_fake_cc_output(
                text=json.dumps({"verdict": "pass"}),
            ),
        )
        reviewer = TaskReviewer(router=AsyncMock(), invoker=invoker)
        result = await reviewer._verify_via_invoker("deliverable", "reqs")

        assert result is not None
        assert "pass" in result

    async def test_invoker_error_returns_none(self) -> None:
        invoker = AsyncMock()
        invoker.run = AsyncMock(
            return_value=_make_fake_cc_output(
                is_error=True, error_message="session failed",
            ),
        )
        reviewer = TaskReviewer(router=AsyncMock(), invoker=invoker)
        result = await reviewer._verify_via_invoker("deliverable", "reqs")
        assert result is None

    async def test_invoker_exception_returns_none(self) -> None:
        invoker = AsyncMock()
        invoker.run = AsyncMock(side_effect=RuntimeError("crash"))
        reviewer = TaskReviewer(router=AsyncMock(), invoker=invoker)
        result = await reviewer._verify_via_invoker("deliverable", "reqs")
        assert result is None


@pytest.mark.asyncio
class TestToolCapableReviewChain:
    async def test_codex_success_skips_invoker(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When codex succeeds, invoker and API are not called."""
        monkeypatch.setattr(
            "genesis.autonomy.executor.review.shutil.which",
            lambda name: "/usr/bin/codex",
        )
        verdict = json.dumps({"verdict": "pass"})

        fake_proc = AsyncMock()
        fake_proc.communicate = AsyncMock(
            return_value=(_codex_jsonl_output(verdict), b""),
        )
        fake_proc.returncode = 0

        invoker = AsyncMock()
        router = AsyncMock()

        with patch(
            "genesis.autonomy.executor.review.asyncio.create_subprocess_exec",
            return_value=fake_proc,
        ):
            reviewer = TaskReviewer(router=router, invoker=invoker)
            result = await reviewer._tool_capable_review("deliverable", "reqs")

        assert result is not None
        # Invoker should NOT have been called
        invoker.run.assert_not_called()
        # Router (API fallback) should NOT have been called for adversarial
        for call in router.route_call.call_args_list:
            assert "20_adversarial" not in call[0][0]

    async def test_codex_fail_falls_to_invoker(self) -> None:
        """When codex is not installed, invoker is tried next."""
        # _no_codex fixture: shutil.which -> None
        invoker = AsyncMock()
        invoker.run = AsyncMock(
            return_value=_make_fake_cc_output(
                text=json.dumps({"verdict": "pass"}),
            ),
        )
        reviewer = TaskReviewer(router=AsyncMock(), invoker=invoker)
        result = await reviewer._tool_capable_review("deliverable", "reqs")

        assert result is not None
        invoker.run.assert_called_once()

    async def test_all_fail_falls_to_api(self) -> None:
        """When codex and invoker both fail, API call site 20 is used."""
        # codex not installed (_no_codex fixture), invoker=None
        pass_json = json.dumps({"verdict": "pass"})
        router = _make_router(pass_json)
        reviewer = TaskReviewer(router=router, invoker=None)
        result = await reviewer._tool_capable_review("deliverable", "reqs")

        assert result is not None
        # Verify the router was called with adversarial call site
        router.route_call.assert_called()

    async def test_chain_total_failure(self) -> None:
        """When all three links fail, returns None."""
        # codex not installed, invoker=None, router returns failure
        router = _make_router(None, success=False)
        reviewer = TaskReviewer(router=router, invoker=None)
        result = await reviewer._tool_capable_review("deliverable", "reqs")
        assert result is None
