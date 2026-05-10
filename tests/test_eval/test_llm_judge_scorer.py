"""Tests for LLMJudgeScorer.

The scorer never calls litellm directly — every test stubs out
``Router.route_call`` so we exercise prompt construction, parsing,
clamping, and error handling without network access.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from genesis.eval.rubrics import _RUBRICS, Rubric, register_rubric
from genesis.eval.scorers import LLMJudgeScorer
from genesis.eval.types import ScorerType
from genesis.routing.types import RoutingResult


@dataclass
class StubRouter:
    """Test double for genesis.routing.router.Router.

    Records the last call_site_id, messages, and kwargs so prompt
    construction can be asserted, and returns a canned RoutingResult.
    """

    response_content: str = '{"score": 0.85, "rationale": "looks good"}'
    success: bool = True
    error: str | None = None
    model_id: str = "deepseek/deepseek-v4-pro"
    provider_used: str = "openrouter-deepseek-v4"
    last_call_site_id: str = ""
    last_messages: list[dict] | None = None
    last_kwargs: dict | None = None

    async def route_call(self, call_site_id, messages, **kwargs):
        self.last_call_site_id = call_site_id
        self.last_messages = messages
        self.last_kwargs = kwargs
        return RoutingResult(
            success=self.success,
            call_site_id=call_site_id,
            content=self.response_content if self.success else None,
            model_id=self.model_id if self.success else None,
            provider_used=self.provider_used if self.success else None,
            error=self.error,
        )


@pytest.fixture
def fresh_registry():
    snapshot = dict(_RUBRICS)
    yield
    _RUBRICS.clear()
    _RUBRICS.update(snapshot)


@pytest.fixture
def simple_rubric(fresh_registry):
    """A test rubric with no extra placeholders, threshold 0.5."""
    r = Rubric(
        name="test_simple",
        version="1.0.0",
        description="test",
        prompt_template="actual={actual} expected={expected}",
        pass_threshold=0.5,
    )
    register_rubric(r)
    return r


@pytest.fixture
def query_rubric(fresh_registry):
    """A test rubric with a {query} extra placeholder."""
    r = Rubric(
        name="test_query",
        version="1.0.0",
        description="test",
        prompt_template="q={query} a={actual} e={expected}",
        pass_threshold=0.7,
        extra_placeholders=("query",),
    )
    register_rubric(r)
    return r


def test_registry_picks_up_llm_judge():
    """ScorerType.LLM_JUDGE resolves through get_scorer like other scorers."""
    from genesis.eval.scorers import get_scorer
    s = get_scorer(ScorerType.LLM_JUDGE)
    assert isinstance(s, LLMJudgeScorer)


def test_score_sync_raises():
    s = LLMJudgeScorer()
    with pytest.raises(NotImplementedError, match="async-only"):
        s.score("a", "b")


async def test_score_async_without_router_raises(simple_rubric):
    s = LLMJudgeScorer()
    with pytest.raises(RuntimeError, match="no router"):
        await s.score_async("a", "b", {"rubric_name": "test_simple"})


async def test_score_async_missing_rubric_name_raises(simple_rubric):
    s = LLMJudgeScorer(router=StubRouter())
    with pytest.raises(ValueError, match="rubric_name"):
        await s.score_async("a", "b", {})


async def test_score_async_unknown_rubric_raises(simple_rubric):
    s = LLMJudgeScorer(router=StubRouter())
    with pytest.raises(KeyError):
        await s.score_async("a", "b", {"rubric_name": "nonexistent"})


async def test_score_async_missing_extra_placeholder_raises(query_rubric):
    s = LLMJudgeScorer(router=StubRouter())
    with pytest.raises(ValueError, match="extra placeholder"):
        # query_rubric needs {query}, but scorer_config doesn't supply it
        await s.score_async("a", "b", {"rubric_name": "test_query"})


async def test_score_async_pass(simple_rubric):
    router = StubRouter(
        response_content='{"score": 0.85, "rationale": "good"}',
    )
    s = LLMJudgeScorer(router=router)
    passed, score, detail_json = await s.score_async(
        "actual_text", "expected_text", {"rubric_name": "test_simple"},
    )
    assert passed is True
    assert score == 0.85
    detail = json.loads(detail_json)
    assert detail["rubric_name"] == "test_simple"
    assert detail["rubric_version"] == "1.0.0"
    assert detail["judge_score"] == 0.85
    assert detail["rationale"] == "good"
    assert detail["judge_model"] == "deepseek/deepseek-v4-pro"
    # And the call site was correct
    assert router.last_call_site_id == "judge"
    # Prompt got the substitutions
    assert "actual=actual_text" in router.last_messages[0]["content"]
    assert "expected=expected_text" in router.last_messages[0]["content"]


async def test_score_async_fail(simple_rubric):
    """Below pass_threshold (0.5) → passed=False."""
    router = StubRouter(
        response_content='{"score": 0.3, "rationale": "weak"}',
    )
    s = LLMJudgeScorer(router=router)
    passed, score, _ = await s.score_async(
        "a", "b", {"rubric_name": "test_simple"},
    )
    assert passed is False
    assert score == 0.3


async def test_score_async_at_threshold_passes(simple_rubric):
    """Exactly pass_threshold (0.5) is a pass — boundary check."""
    router = StubRouter(
        response_content='{"score": 0.5, "rationale": ""}',
    )
    s = LLMJudgeScorer(router=router)
    passed, _, _ = await s.score_async(
        "a", "b", {"rubric_name": "test_simple"},
    )
    assert passed is True


async def test_score_async_clamps_above_one(simple_rubric):
    """Models occasionally return 1.5 or similar — must clamp."""
    router = StubRouter(
        response_content='{"score": 1.5, "rationale": ""}',
    )
    s = LLMJudgeScorer(router=router)
    _, score, _ = await s.score_async(
        "a", "b", {"rubric_name": "test_simple"},
    )
    assert score == 1.0


async def test_score_async_clamps_below_zero(simple_rubric):
    router = StubRouter(
        response_content='{"score": -0.5, "rationale": ""}',
    )
    s = LLMJudgeScorer(router=router)
    _, score, _ = await s.score_async(
        "a", "b", {"rubric_name": "test_simple"},
    )
    assert score == 0.0


async def test_score_async_extracts_json_from_markdown(simple_rubric):
    """Tolerant parser handles ```json ... ``` fences."""
    router = StubRouter(
        response_content=(
            "Here is my judgment:\n"
            "```json\n"
            '{"score": 0.7, "rationale": "ok"}\n'
            "```\n"
        ),
    )
    s = LLMJudgeScorer(router=router)
    passed, score, _ = await s.score_async(
        "a", "b", {"rubric_name": "test_simple"},
    )
    assert passed is True
    assert score == 0.7


async def test_score_async_unparseable_returns_parse_fail(simple_rubric):
    router = StubRouter(
        response_content="not even close to JSON",
    )
    s = LLMJudgeScorer(router=router)
    passed, score, detail_json = await s.score_async(
        "a", "b", {"rubric_name": "test_simple"},
    )
    assert passed is False
    assert score == 0.0
    detail = json.loads(detail_json)
    assert detail["error"] == "judge_parse_fail"
    assert "raw_response" in detail


async def test_score_async_call_failure_returns_call_fail(simple_rubric):
    router = StubRouter(success=False, error="all providers exhausted")
    s = LLMJudgeScorer(router=router)
    passed, score, detail_json = await s.score_async(
        "a", "b", {"rubric_name": "test_simple"},
    )
    assert passed is False
    assert score == 0.0
    detail = json.loads(detail_json)
    assert detail["error"] == "judge_call_fail"
    assert detail["error_message"] == "all providers exhausted"


async def test_score_async_uses_extra_placeholder(query_rubric):
    """{query} substitution from scorer_config flows into the prompt."""
    router = StubRouter(
        response_content='{"score": 0.9, "rationale": ""}',
    )
    s = LLMJudgeScorer(router=router)
    await s.score_async(
        "memory_text", "label",
        {"rubric_name": "test_query", "query": "what was the decision"},
    )
    assert "q=what was the decision" in router.last_messages[0]["content"]
    assert "a=memory_text" in router.last_messages[0]["content"]


async def test_score_async_rejects_nan(simple_rubric):
    """NaN survives min/max clamping (propagates), so it must be
    rejected as malformed rather than leaking into the result."""
    router = StubRouter(
        response_content='{"score": NaN, "rationale": ""}',
    )
    s = LLMJudgeScorer(router=router)
    passed, score, detail_json = await s.score_async(
        "a", "b", {"rubric_name": "test_simple"},
    )
    assert passed is False
    assert score == 0.0
    detail = json.loads(detail_json)
    assert detail["error"] == "judge_parse_fail"
    assert "non-finite" in detail["error_message"]


async def test_score_async_rejects_infinity(simple_rubric):
    """+Infinity would clamp silently to 1.0 without the finite check."""
    router = StubRouter(
        response_content='{"score": Infinity, "rationale": ""}',
    )
    s = LLMJudgeScorer(router=router)
    passed, score, detail_json = await s.score_async(
        "a", "b", {"rubric_name": "test_simple"},
    )
    assert passed is False
    assert score == 0.0
    detail = json.loads(detail_json)
    assert detail["error"] == "judge_parse_fail"


async def test_score_async_empty_content_treated_as_parse_fail(simple_rubric):
    """A successful call with empty content must not produce a 0.0 pass —
    it should hit the parse-fail path so calibration treats it as an
    error, not as a confident negative judgment."""
    router = StubRouter(response_content="")
    s = LLMJudgeScorer(router=router)
    passed, score, detail_json = await s.score_async(
        "a", "b", {"rubric_name": "test_simple"},
    )
    assert passed is False
    assert score == 0.0
    detail = json.loads(detail_json)
    assert detail["error"] == "judge_parse_fail"


async def test_score_async_multiple_json_blocks(simple_rubric):
    """When the model emits explanatory JSON-like fragments before the
    real answer, the markdown-fence path picks the fenced block. Test
    that the fenced block wins over a stray earlier brace."""
    router = StubRouter(
        response_content=(
            "I considered options like {note: irrelevant} first.\n"
            "```json\n"
            '{"score": 0.6, "rationale": "ok"}\n'
            "```\n"
        ),
    )
    s = LLMJudgeScorer(router=router)
    passed, score, _ = await s.score_async(
        "a", "b", {"rubric_name": "test_simple"},
    )
    assert passed is True
    assert score == 0.6


async def test_set_router_after_construction(simple_rubric):
    s = LLMJudgeScorer()
    s.set_router(StubRouter(
        response_content='{"score": 0.6, "rationale": ""}',
    ))
    passed, score, _ = await s.score_async(
        "a", "b", {"rubric_name": "test_simple"},
    )
    assert passed is True
    assert score == 0.6
