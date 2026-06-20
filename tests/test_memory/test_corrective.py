"""Tests for genesis.memory.corrective — selective corrective retrieval (W-CRAG).

Everything external is mocked: the router (route_call), the retriever (recall),
the reranker, the web tools (_impl_web_search / _impl_web_fetch), and the j9
calibration emit (emit_recall_corrected — added separately in j9_hooks).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.memory import corrective
from genesis.memory.corrective import (
    _bucket,
    _norm,
    _parse_grade,
    maybe_correct_recall,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _routing_result(*, success: bool, content: str | None) -> MagicMock:
    """A RoutingResult-like object with .success and .content."""
    r = MagicMock()
    r.success = success
    r.content = content
    return r


def _grade_content(score: float) -> str:
    return json.dumps({"score": score, "rationale": "ok"})


def _mem_result(mid: str, content: str, score: float) -> dict:
    """memory_recall-shaped result dict."""
    return {"memory_id": mid, "content": content, "score": score, "payload": {}}


def _kb_result(uid: str, content: str, score: float) -> dict:
    """knowledge_recall-shaped result dict."""
    return {"unit_id": uid, "content": content, "score": score, "origin": "vector"}


@pytest.fixture(autouse=True)
def _patch_emit():
    """Stub the j9 calibration emit so tests don't depend on its addition."""
    with patch.object(corrective, "_log", new=AsyncMock(return_value=None)):
        yield


@pytest.fixture
def retriever():
    """A retriever whose .recall is an AsyncMock (no real re-retrieval)."""
    r = MagicMock()
    r.recall = AsyncMock(return_value=[])
    return r


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestNorm:
    def test_memory_id_shape(self):
        n = _norm(_mem_result("m1", "hello", 0.5))
        assert n == {"id": "m1", "content": "hello", "score": 0.5}

    def test_unit_id_shape(self):
        n = _norm(_kb_result("u1", "world", 0.4))
        assert n == {"id": "u1", "content": "world", "score": 0.4}

    def test_missing_fields_default(self):
        n = _norm({})
        assert n == {"id": "", "content": "", "score": 0.0}

    def test_non_numeric_score_coerces(self):
        n = _norm({"memory_id": "m", "content": "c", "score": "oops"})
        assert n["score"] == 0.0


class TestParseGrade:
    def test_plain_json(self):
        assert _parse_grade(_grade_content(0.7)) == 0.7

    def test_fenced_json(self):
        raw = "```json\n" + _grade_content(0.42) + "\n```"
        assert _parse_grade(raw) == 0.42

    def test_clamps(self):
        assert _parse_grade(json.dumps({"score": 1.5})) == 1.0
        assert _parse_grade(json.dumps({"score": -0.2})) == 0.0

    def test_unparseable_is_none(self):
        assert _parse_grade("not json at all") is None

    def test_non_finite_is_none(self):
        assert _parse_grade(json.dumps({"score": float("inf")})) is None


class TestBucket:
    def test_correct(self):
        assert _bucket([0.8, 0.2, None]) == "Correct"

    def test_incorrect(self):
        assert _bucket([0.1, 0.05, None]) == "Incorrect"

    def test_ambiguous(self):
        assert _bucket([0.45, 0.4]) == "Ambiguous"


# ---------------------------------------------------------------------------
# Behavior: drift skip
# ---------------------------------------------------------------------------


async def test_drift_skip_returns_original(retriever):
    results = [_mem_result("m1", "x", 0.2)]
    router = MagicMock()
    router.route_call = AsyncMock()

    out = await maybe_correct_recall(
        query="q",
        results=results,
        retriever=retriever,
        db=MagicMock(),
        path="memory",
        pipeline_used="auto_drift",
        router=router,
    )

    assert out is results
    router.route_call.assert_not_called()
    retriever.recall.assert_not_called()


# ---------------------------------------------------------------------------
# Behavior: latency pre-gate
# ---------------------------------------------------------------------------


async def test_latency_pregate_skips_grading(retriever):
    # Top score above _SKIP_GRADE_ABOVE (0.75) → no grading.
    results = [_mem_result("m1", "x", 0.9), _mem_result("m2", "y", 0.1)]
    router = MagicMock()
    router.route_call = AsyncMock()

    out = await maybe_correct_recall(
        query="q",
        results=results,
        retriever=retriever,
        db=MagicMock(),
        path="memory",
        router=router,
    )

    assert out is results
    router.route_call.assert_not_called()
    retriever.recall.assert_not_called()


# ---------------------------------------------------------------------------
# Behavior: grader success=False → not Incorrect
# ---------------------------------------------------------------------------


async def test_grader_failure_returns_original(retriever):
    results = [_mem_result("m1", "x", 0.2)]
    router = MagicMock()
    # success=False simulates degradation-skip / provider down.
    router.route_call = AsyncMock(
        return_value=_routing_result(success=False, content=None),
    )

    out = await maybe_correct_recall(
        query="q",
        results=results,
        retriever=retriever,
        db=MagicMock(),
        path="memory",
        router=router,
    )

    assert out is results
    # The grader was attempted but its failure must NOT trigger correction.
    router.route_call.assert_called()
    retriever.recall.assert_not_called()


# ---------------------------------------------------------------------------
# Behavior: all grades time out → return original
# ---------------------------------------------------------------------------


async def test_all_grades_timeout_returns_original(retriever):
    results = [_mem_result("m1", "x", 0.2), _mem_result("m2", "y", 0.2)]
    router = MagicMock()

    async def _hang(*_a, **_k):
        raise TimeoutError

    router.route_call = AsyncMock(side_effect=_hang)

    out = await maybe_correct_recall(
        query="q",
        results=results,
        retriever=retriever,
        db=MagicMock(),
        path="memory",
        router=router,
    )

    assert out is results
    retriever.recall.assert_not_called()


# ---------------------------------------------------------------------------
# Behavior: Correct bucket drops sub-0.6 items
# ---------------------------------------------------------------------------


async def test_correct_bucket_drops_irrelevant(retriever):
    results = [
        _mem_result("m1", "relevant", 0.2),
        _mem_result("m2", "irrelevant", 0.2),
    ]
    grades = iter([_grade_content(0.9), _grade_content(0.1)])
    router = MagicMock()

    async def _route(*_a, **_k):
        return _routing_result(success=True, content=next(grades))

    router.route_call = AsyncMock(side_effect=_route)

    out = await maybe_correct_recall(
        query="q",
        results=results,
        retriever=retriever,
        db=MagicMock(),
        path="memory",
        router=router,
    )

    # Max grade 0.9 >= 0.6 → Correct; the 0.1 item is dropped.
    assert len(out) == 1
    assert out[0]["memory_id"] == "m1"
    retriever.recall.assert_not_called()


# ---------------------------------------------------------------------------
# Behavior: Ambiguous = dark (no action, no retriever re-call)
# ---------------------------------------------------------------------------


async def test_ambiguous_is_dark(retriever):
    results = [_mem_result("m1", "x", 0.2), _mem_result("m2", "y", 0.2)]
    grades = iter([_grade_content(0.45), _grade_content(0.4)])
    router = MagicMock()

    async def _route(*_a, **_k):
        return _routing_result(success=True, content=next(grades))

    router.route_call = AsyncMock(side_effect=_route)

    out = await maybe_correct_recall(
        query="q",
        results=results,
        retriever=retriever,
        db=MagicMock(),
        path="memory",
        router=router,
    )

    # Best grade 0.45 → Ambiguous → DARK: original returned, no re-retrieval.
    assert out is results
    retriever.recall.assert_not_called()


# ---------------------------------------------------------------------------
# Behavior: Incorrect on memory path → re-retrieve + raw KB, NEVER web
# ---------------------------------------------------------------------------


async def test_incorrect_memory_path_no_web(retriever):
    results = [_mem_result("m1", "off-topic", 0.2)]
    router = MagicMock()
    router.route_call = AsyncMock(
        return_value=_routing_result(success=True, content=_grade_content(0.05)),
    )
    # Re-retrieval returns one augmented RetrievalResult-like object.
    aug = MagicMock()
    aug.memory_id = "m99"
    aug.content = "augmented"
    aug.score = 0.8
    aug.payload = {}
    retriever.recall = AsyncMock(return_value=[aug])

    mock_search = AsyncMock(return_value={"results": []})
    mock_fetch = AsyncMock(return_value={"content": ""})
    mock_reranker = MagicMock()
    mock_reranker.rerank = AsyncMock(return_value=[])
    mock_reranker.close = AsyncMock()

    with (
        patch("genesis.mcp.health.web_tools._impl_web_search", mock_search),
        patch("genesis.mcp.health.web_tools._impl_web_fetch", mock_fetch),
        patch("genesis.memory.reranker.VoyageReranker", return_value=mock_reranker),
    ):
        out = await maybe_correct_recall(
            query="q",
            results=results,
            retriever=retriever,
            db=MagicMock(),
            path="memory",
            router=router,
        )

    # Re-retrieval happened (relaxed + KB = 2 recall calls), web did NOT.
    assert retriever.recall.await_count == 2
    mock_search.assert_not_called()
    mock_fetch.assert_not_called()
    # Augmented result is present in output.
    assert any(r.get("memory_id") == "m99" for r in out)


# ---------------------------------------------------------------------------
# Behavior: Incorrect on knowledge path → MAY call web
# ---------------------------------------------------------------------------


async def test_incorrect_knowledge_path_may_call_web(retriever):
    results = [_kb_result("u1", "off-topic", 0.2)]
    router = MagicMock()
    router.route_call = AsyncMock(
        return_value=_routing_result(success=True, content=_grade_content(0.05)),
    )
    retriever.recall = AsyncMock(return_value=[])

    mock_search = AsyncMock(
        return_value={"results": [{"url": "https://example.com", "title": "t"}]},
    )
    mock_fetch = AsyncMock(
        return_value={"content": "para one about q\n\npara two about q"},
    )
    mock_reranker = MagicMock()
    mock_reranker.rerank = AsyncMock(return_value=[{"id": "0", "score": 0.7}])
    mock_reranker.close = AsyncMock()

    with (
        patch("genesis.mcp.health.web_tools._impl_web_search", mock_search),
        patch("genesis.mcp.health.web_tools._impl_web_fetch", mock_fetch),
        patch("genesis.memory.reranker.VoyageReranker", return_value=mock_reranker),
    ):
        out = await maybe_correct_recall(
            query="q",
            results=results,
            retriever=retriever,
            db=MagicMock(),
            path="knowledge",
            router=router,
        )

    # Web search was reachable on the knowledge path.
    mock_search.assert_called()
    assert isinstance(out, list)


# ---------------------------------------------------------------------------
# Behavior: shape normalization works for both id flavors end-to-end
# ---------------------------------------------------------------------------


async def test_shape_normalization_both(retriever):
    # memory_id flavor — Correct keeps the relevant one.
    mem_results = [_mem_result("m1", "rel", 0.2), _mem_result("m2", "no", 0.2)]
    grades_mem = iter([_grade_content(0.9), _grade_content(0.1)])
    router = MagicMock()

    async def _route_mem(*_a, **_k):
        return _routing_result(success=True, content=next(grades_mem))

    router.route_call = AsyncMock(side_effect=_route_mem)
    out_mem = await maybe_correct_recall(
        query="q",
        results=mem_results,
        retriever=retriever,
        db=MagicMock(),
        path="memory",
        router=router,
    )
    assert [r["memory_id"] for r in out_mem] == ["m1"]

    # unit_id flavor — same Correct-drop logic on a different shape.
    kb_results = [_kb_result("u1", "rel", 0.2), _kb_result("u2", "no", 0.2)]
    grades_kb = iter([_grade_content(0.9), _grade_content(0.1)])

    async def _route_kb(*_a, **_k):
        return _routing_result(success=True, content=next(grades_kb))

    router.route_call = AsyncMock(side_effect=_route_kb)
    out_kb = await maybe_correct_recall(
        query="q",
        results=kb_results,
        retriever=retriever,
        db=MagicMock(),
        path="knowledge",
        router=router,
    )
    assert [r["unit_id"] for r in out_kb] == ["u1"]
