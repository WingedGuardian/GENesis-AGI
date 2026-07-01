"""Tests for the C2b multi-procedure builder (judge_multi_procedure).

Covers: empty result, real store with grounding recorded, retry on an
unparseable envelope, the per-session max_new cap, sub-procedure tagging, and the
warning-only grounding gate (a weakly-grounded procedure still stores).

The novelty embedder is forced to None (get_embedding_provider patched) so these
tests are deterministic and offline — storage then goes through the fail-open
path, exercising the builder's loop/store wiring without a real embedding backend.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.db.crud import procedural
from genesis.learning.procedural import embedding as embedding_mod
from genesis.learning.procedural.judge import judge_multi_procedure

_SPINE = [{
    "turn": 1, "type": "tool", "tool": "Bash",
    "args_summary": '{"command": "echo hi"}', "outcome": "ok", "error_text": "",
}]
_SCOPING_TASK = '{"procedure_type": "task_procedure"}'


@dataclass
class _Result:
    success: bool = True
    content: str = ""
    error: str | None = None


def _router_seq(*results: _Result) -> MagicMock:
    r = MagicMock()
    r.route_call = AsyncMock(side_effect=list(results))
    return r


def _builder_json(procedures: list[dict]) -> str:
    return "```json\n" + json.dumps({"procedures": procedures}) + "\n```"


def _proc(task_type="cut-release", principle="ship the release", steps=None, **extra):
    p = {
        "task_type": task_type,
        "principle": principle,
        "steps": steps or ["run `echo hi`"],
        "tools_used": ["Bash"],
        "context_tags": ["release"],
    }
    p.update(extra)
    return p


@pytest.fixture(autouse=True)
def _no_embedder(monkeypatch):
    """Force the novelty embedder to None → deterministic fail-open store path."""
    embedding_mod._EMBEDDING_PROVIDER = None
    embedding_mod._fail_open_timestamps.clear()
    monkeypatch.setattr(
        "genesis.learning.procedural.judge.get_embedding_provider", lambda: None,
    )
    yield
    embedding_mod._fail_open_timestamps.clear()


@pytest.mark.asyncio
async def test_builder_empty_response_returns_empty(db):
    router = _router_seq(_Result(content=_builder_json([])))
    assert await judge_multi_procedure(db, _SPINE, "", 0.5, router) == []


@pytest.mark.asyncio
async def test_builder_stores_real_procedure_and_records_grounding(db):
    router = _router_seq(
        _Result(content=_builder_json([_proc()])),  # builder
        _Result(content=_SCOPING_TASK),             # scoping verdict
    )
    haystack = '{"command": "echo hi"}'  # grounds the step
    stored = await judge_multi_procedure(db, _SPINE, haystack, 0.5, router)
    assert len(stored) == 1

    row = await procedural.find_by_task_type(db, "cut-release")
    assert row is not None
    assert row["draft"] == 1 and row["activation_tier"] == "DORMANT"
    source = json.loads(row["source"])
    assert source["type"] == "struggle_extraction"
    assert "grounding_score" in source and source["grounding_score"] >= 0.5


@pytest.mark.asyncio
async def test_builder_retries_on_unparseable_envelope(db):
    router = _router_seq(
        _Result(content="not json — total garbage"),  # attempt 1 → unparseable
        _Result(content=_builder_json([_proc()])),     # attempt 2 → valid (retry)
        _Result(content=_SCOPING_TASK),                 # scoping
    )
    stored = await judge_multi_procedure(db, _SPINE, '{"command": "echo hi"}', 0.5, router)
    assert len(stored) == 1


@pytest.mark.asyncio
async def test_builder_respects_max_new(db):
    procs = [_proc(task_type=f"task-{i}") for i in range(3)]
    router = _router_seq(
        _Result(content=_builder_json(procs)),  # builder returns 3
        _Result(content=_SCOPING_TASK),          # scoping for the 1 we keep
    )
    stored = await judge_multi_procedure(
        db, _SPINE, '{"command": "echo hi"}', 0.5, router, max_new=1,
    )
    assert len(stored) == 1


@pytest.mark.asyncio
async def test_builder_tags_subprocedure(db):
    proc = _proc(task_type="solve-captcha", is_subprocedure_of="publish-medium-post")
    router = _router_seq(
        _Result(content=_builder_json([proc])),
        _Result(content=_SCOPING_TASK),
    )
    stored = await judge_multi_procedure(db, _SPINE, '{"command": "echo hi"}', 0.5, router)
    assert len(stored) == 1

    row = await procedural.find_by_task_type(db, "solve-captcha")
    tags = json.loads(row["context_tags"])
    assert "subprocedure_of:publish-medium-post" in tags


@pytest.mark.asyncio
async def test_builder_low_grounding_still_stores(db):
    """Grounding is warning-only: a weakly-grounded procedure is STILL stored,
    and its low score is recorded for observability."""
    proc = _proc(task_type="weak-ground", steps=["run `frobnicate --xyz /a/b/c`"])
    router = _router_seq(
        _Result(content=_builder_json([proc])),
        _Result(content=_SCOPING_TASK),
    )
    haystack = '{"command": "totally unrelated execution record"}'
    stored = await judge_multi_procedure(db, _SPINE, haystack, 0.5, router)
    assert len(stored) == 1  # stored despite low grounding

    row = await procedural.find_by_task_type(db, "weak-ground")
    source = json.loads(row["source"])
    assert source["grounding_score"] < 0.25
