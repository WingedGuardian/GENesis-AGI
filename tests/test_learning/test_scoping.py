"""Tests for the procedure scoping gate — behavioral-directive classifier.

The gate keeps behavioral DIRECTIVES (general working-style rules) out of the
procedure store. Its safety contract is FAIL-OPEN: any classifier failure must
default to "keep" so a real procedure is never silently suppressed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.learning.procedural.extractor import extract_procedure
from genesis.learning.procedural.judge import (
    judge_extraction_candidate,
    judge_struggle_procedure,
)
from genesis.learning.procedural.scoping import (
    PROCEDURE_TYPE_DIRECTIVE,
    PROCEDURE_TYPE_TASK,
    _parse_procedure_type,
    is_behavioral_directive,
)


@dataclass
class _Result:
    success: bool = True
    content: str = ""
    error: str | None = None


def _router(content: str, *, success: bool = True) -> MagicMock:
    r = MagicMock()
    r.route_call = AsyncMock(return_value=_Result(success=success, content=content))
    return r


def _router_seq(*results: _Result) -> MagicMock:
    r = MagicMock()
    r.route_call = AsyncMock(side_effect=list(results))
    return r


def _embedder() -> MagicMock:
    emb = MagicMock()
    emb.embed = AsyncMock(return_value=[1.0, 0.0, 0.0])
    return emb


# ── _parse_procedure_type ────────────────────────────────────────────────────

def test_parse_backticked_directive():
    out = _parse_procedure_type('```json\n{"procedure_type": "behavioral_directive"}\n```')
    assert out == PROCEDURE_TYPE_DIRECTIVE


def test_parse_raw_task_procedure():
    out = _parse_procedure_type('{"procedure_type": "task_procedure", "reason": "docker"}')
    assert out == PROCEDURE_TYPE_TASK


def test_parse_lenient_substring_when_not_json():
    # Model prose without valid JSON — fall back to substring detection.
    assert _parse_procedure_type("Verdict: behavioral_directive — it's a habit.") == PROCEDURE_TYPE_DIRECTIVE


def test_parse_returns_none_on_garbage():
    assert _parse_procedure_type("no label anywhere") is None
    assert _parse_procedure_type("") is None
    assert _parse_procedure_type(None) is None


def test_parse_returns_none_on_unknown_value():
    # Unknown label must not be treated as a directive (fail open to keep).
    assert _parse_procedure_type('{"procedure_type": "something_else"}') is None


def test_parse_lenient_both_present_falls_to_task():
    # Both labels in non-JSON prose — must NOT suppress; falls to task_procedure (keep).
    out = _parse_procedure_type(
        "This is a task_procedure but has some behavioral_directive qualities too."
    )
    assert out == PROCEDURE_TYPE_TASK


# ── is_behavioral_directive ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_directive_is_detected():
    r = _router('```json\n{"procedure_type": "behavioral_directive"}\n```')
    out = await is_behavioral_directive(
        r, task_type="honest-confidence-gate", principle="always state confidence", steps=[],
    )
    assert out is True


@pytest.mark.asyncio
async def test_real_procedure_is_kept():
    r = _router('```json\n{"procedure_type": "task_procedure"}\n```')
    out = await is_behavioral_directive(
        r, task_type="docker-cache-bust", principle="rm the COPY layer", steps=["docker build"],
    )
    assert out is False


@pytest.mark.asyncio
async def test_fail_open_on_call_exception():
    r = MagicMock()
    r.route_call = AsyncMock(side_effect=RuntimeError("router down"))
    out = await is_behavioral_directive(r, task_type="x", principle="p", steps=["s"])
    assert out is False  # keep


@pytest.mark.asyncio
async def test_fail_open_on_unsuccessful_result():
    r = _router("", success=False)
    out = await is_behavioral_directive(r, task_type="x", principle="p", steps=["s"])
    assert out is False  # keep


@pytest.mark.asyncio
async def test_fail_open_on_unparseable_content():
    r = _router("<html>Error 500</html>")
    out = await is_behavioral_directive(r, task_type="x", principle="p", steps=["s"])
    assert out is False  # keep


@pytest.mark.asyncio
async def test_uses_extraction_call_site():
    r = _router('{"procedure_type": "task_procedure"}')
    await is_behavioral_directive(r, task_type="x", principle="p", steps=["s"])
    _, kwargs = r.route_call.call_args
    assert kwargs["call_site_id"] == "38_procedure_extraction"


# ── Integration: directive is actually suppressed at each storage path ────────

def _valid_payload() -> dict:
    return {
        "task_type": "honest-confidence-gate",
        "principle": "always state confidence before planning",
        "steps": ["assess", "state confidence"],
        "tools_used": ["Bash"],
        "context_tags": ["meta"],
        "tool_trigger": None,
    }


@pytest.mark.asyncio
async def test_extract_procedure_suppresses_directive(db):
    """Path 3 (extract_procedure): a directive verdict blocks storage."""
    router = _router_seq(
        _Result(content=json.dumps(_valid_payload())),          # extraction
        _Result(content='{"procedure_type": "behavioral_directive"}'),  # scoping
    )
    proc_id = await extract_procedure(
        db, summary_text="discussed confidence", outcome="success",
        router=router, embedding_provider=_embedder(),
    )
    assert proc_id is None  # suppressed as a behavioral directive


@pytest.mark.asyncio
async def test_extract_procedure_keeps_real_procedure(db):
    """Path 3 control: a task_procedure verdict stores normally."""
    payload = {
        "task_type": "docker-layer-cache-bust",
        "principle": "rebuild with no-cache when COPY layer is stale",
        "steps": ["docker build --no-cache"],
        "tools_used": ["Bash"],
        "context_tags": ["docker"],
        "tool_trigger": None,
    }
    router = _router_seq(
        _Result(content=json.dumps(payload)),
        _Result(content='{"procedure_type": "task_procedure"}'),
    )
    proc_id = await extract_procedure(
        db, summary_text="busted a docker cache", outcome="success",
        router=router, embedding_provider=_embedder(),
    )
    assert proc_id is not None


@pytest.mark.asyncio
async def test_judge_struggle_suppresses_directive(db):
    """Judge path (struggle stream): a directive verdict blocks storage."""
    judge_json = (
        '```json\n{"worth_storing": true, "task_type": "confidence-gate", '
        '"principle": "always state confidence", "steps": ["1"], '
        '"tools_used": ["Bash"], "context_tags": ["meta"]}\n```'
    )
    router = _router_seq(
        _Result(content=judge_json),                                    # judge
        _Result(content='{"procedure_type": "behavioral_directive"}'),  # scoping
    )
    spine = [{
        "turn": 1, "type": "tool", "tool": "Bash",
        "args_summary": "x", "outcome": "ok", "error_text": "",
    }]
    result = await judge_struggle_procedure(db, spine, 0.5, Path("/tmp/x.jsonl"), router)
    assert result is None  # suppressed as a behavioral directive


@pytest.mark.asyncio
async def test_judge_extraction_candidate_suppresses_directive(db):
    """Judge path (extraction-candidate stream): a directive verdict blocks storage.

    Covers the second judge entry point so the router threading through
    _store_judged_procedure is caught by a dedicated test, not only structurally.
    """
    judge_json = (
        '```json\n{"worth_storing": true, "task_type": "pre-plan-confidence", '
        '"principle": "investigate before planning", "steps": ["1"], '
        '"tools_used": ["Bash"], "context_tags": ["meta"]}\n```'
    )
    router = _router_seq(
        _Result(content=judge_json),                                    # judge
        _Result(content='{"procedure_type": "behavioral_directive"}'),  # scoping
    )
    candidate = {
        "principle": "investigate before planning",
        "scenario": "before planning",
        "tools_used": ["Bash"],
    }
    result = await judge_extraction_candidate(db, candidate, "chunk context", router)
    assert result is None  # suppressed as a behavioral directive
