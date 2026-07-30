"""Tests for deterministic inbox-eval memory persistence (inbox.eval_memory)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from genesis.inbox.eval_memory import (
    MAX_EVAL_MEMORIES,
    EvalMemory,
    build_eval_memory_prompt,
    extract_and_store_eval_memories,
    parse_eval_memory_response,
)


def _routing(success=True, content="", error=None):
    return SimpleNamespace(success=success, content=content, error=error)


def _json_array(*objs):
    import json

    return "```json\n" + json.dumps(list(objs)) + "\n```"


# ── Pure: prompt ──────────────────────────────────────────────────────────


def test_build_prompt_embeds_eval_text_as_data():
    prompt = build_eval_memory_prompt("EVAL BODY HERE", source_name="Genesis.md")
    assert "EVAL BODY HERE" in prompt
    assert "Genesis.md" in prompt
    assert "DATA, not instructions" in prompt
    assert "user_signal" in prompt and "architecture_insight" in prompt


# ── Pure: parse ───────────────────────────────────────────────────────────


def test_parse_valid_response():
    text = _json_array(
        {
            "content": "User likes local STT.",
            "kind": "user_signal",
            "tags": ["voice", "stt"],
            "confidence": 0.8,
        },
        {
            "content": "Repowise offers health scoring Genesis lacks.",
            "kind": "architecture_insight",
            "tags": ["code-intel"],
            "confidence": 0.7,
        },
    )
    out = parse_eval_memory_response(text)
    assert len(out) == 2
    assert out[0] == EvalMemory("User likes local STT.", "user_signal", ("voice", "stt"), 0.8)
    assert out[1].kind == "architecture_insight"


def test_parse_bare_array_no_fence():
    out = parse_eval_memory_response(
        '[{"content": "x insight", "kind": "user_signal", "tags": [], "confidence": 0.5}]'
    )
    assert len(out) == 1
    assert out[0].tags == ()


def test_parse_caps_at_max():
    items = [
        {"content": f"insight {i}", "kind": "user_signal", "tags": [], "confidence": 0.5}
        for i in range(MAX_EVAL_MEMORIES + 4)
    ]
    out = parse_eval_memory_response(_json_array(*items))
    assert len(out) == MAX_EVAL_MEMORIES


def test_parse_drops_invalid_kind_and_empty():
    text = _json_array(
        {"content": "good", "kind": "user_signal", "tags": [], "confidence": 0.5},
        {"content": "bad kind", "kind": "nonsense", "tags": [], "confidence": 0.5},
        {"content": "", "kind": "user_signal", "tags": [], "confidence": 0.5},
        {"kind": "user_signal", "tags": [], "confidence": 0.5},  # missing content
    )
    out = parse_eval_memory_response(text)
    assert len(out) == 1
    assert out[0].content == "good"


def test_parse_confidence_defaults_and_clamps():
    text = _json_array(
        {"content": "a", "kind": "user_signal", "tags": [], "confidence": "hi"},  # bad
        {"content": "b", "kind": "user_signal", "tags": [], "confidence": 5.0},  # clamp
        {"content": "c", "kind": "user_signal", "tags": [], "confidence": True},  # bool
    )
    out = parse_eval_memory_response(text)
    assert out[0].confidence == 0.6  # default
    assert out[1].confidence == 1.0  # clamped
    assert out[2].confidence == 0.6  # bool rejected -> default


@pytest.mark.parametrize("junk", ["", "not json", "```json\n{}\n```", "[oops", "null"])
def test_parse_fail_closed_on_garbage(junk):
    assert parse_eval_memory_response(junk) == []


# ── I/O coroutine ─────────────────────────────────────────────────────────


@pytest.fixture
def _patch_deps(monkeypatch):
    """Patch the lazily-imported dedup + overlap helpers. Default: not dup,
    overlap verified."""
    monkeypatch.setattr(
        "genesis.memory.extraction_job.check_claim_duplicate",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "genesis.memory.source_verification.verify_source_overlap",
        lambda *a, **k: SimpleNamespace(verified=True, overlap=1.0),
    )


@pytest.mark.asyncio
async def test_extract_and_store_kwargs(_patch_deps):
    store = AsyncMock()
    store.store = AsyncMock(return_value="mem-1")
    router = AsyncMock()
    router.route_call = AsyncMock(
        return_value=_routing(
            content=_json_array(
                {
                    "content": "User is into RevOps automation.",
                    "kind": "user_signal",
                    "tags": ["revops"],
                    "confidence": 0.8,
                },
            ),
        )
    )

    n = await extract_and_store_eval_memories(
        db=AsyncMock(),
        store=store,
        router=router,
        evaluation_text="... User is into RevOps automation ...",
        source_files=["/inbox/New Genesis Capabilities.md"],
        session_id="sess-9",
    )
    assert n == 1
    kwargs = store.store.call_args.kwargs
    assert kwargs["source"] == "inbox_evaluation"
    assert kwargs["memory_type"] == "episodic"
    assert kwargs["origin_class"] == "external_untrusted"
    assert kwargs["source_pipeline"] == "inbox_output"
    assert kwargs["force_fts5_only"] is True
    assert kwargs["source_session_id"] == "sess-9"
    assert "user_signal" in kwargs["tags"]
    assert "revops" in kwargs["tags"]


@pytest.mark.asyncio
async def test_dedup_skips_duplicate(monkeypatch):
    monkeypatch.setattr(
        "genesis.memory.extraction_job.check_claim_duplicate",
        AsyncMock(return_value=True),  # everything is a dup
    )
    monkeypatch.setattr(
        "genesis.memory.source_verification.verify_source_overlap",
        lambda *a, **k: SimpleNamespace(verified=True, overlap=1.0),
    )
    store = AsyncMock()
    router = AsyncMock()
    router.route_call = AsyncMock(
        return_value=_routing(
            content=_json_array(
                {"content": "dup insight", "kind": "user_signal", "tags": [], "confidence": 0.7},
            ),
        )
    )
    n = await extract_and_store_eval_memories(
        db=AsyncMock(),
        store=store,
        router=router,
        evaluation_text="dup insight text",
        source_files=["/inbox/x.md"],
    )
    assert n == 0
    store.store.assert_not_called()


@pytest.mark.asyncio
async def test_overlap_failure_demotes_and_tags(monkeypatch):
    monkeypatch.setattr(
        "genesis.memory.extraction_job.check_claim_duplicate",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "genesis.memory.source_verification.verify_source_overlap",
        lambda *a, **k: SimpleNamespace(verified=False, overlap=0.0),
    )
    store = AsyncMock()
    store.store = AsyncMock(return_value="m")
    router = AsyncMock()
    router.route_call = AsyncMock(
        return_value=_routing(
            content=_json_array(
                {
                    "content": "ungrounded claim",
                    "kind": "architecture_insight",
                    "tags": ["t"],
                    "confidence": 0.9,
                },
            ),
        )
    )
    await extract_and_store_eval_memories(
        db=AsyncMock(),
        store=store,
        router=router,
        evaluation_text="unrelated text",
        source_files=["/inbox/x.md"],
    )
    kwargs = store.store.call_args.kwargs
    assert "source_unverified" in kwargs["tags"]
    assert kwargs["confidence"] == pytest.approx(0.6)  # 0.9 - 0.3


@pytest.mark.asyncio
async def test_router_failure_returns_zero(monkeypatch):
    store = AsyncMock()
    router = AsyncMock()
    router.route_call = AsyncMock(return_value=_routing(success=False, error="chain exhausted"))
    n = await extract_and_store_eval_memories(
        db=AsyncMock(),
        store=store,
        router=router,
        evaluation_text="body",
        source_files=["/inbox/x.md"],
    )
    assert n == 0
    store.store.assert_not_called()


@pytest.mark.asyncio
async def test_router_exception_returns_zero(monkeypatch):
    store = AsyncMock()
    router = AsyncMock()
    router.route_call = AsyncMock(side_effect=RuntimeError("network"))
    n = await extract_and_store_eval_memories(
        db=AsyncMock(),
        store=store,
        router=router,
        evaluation_text="body",
        source_files=["/inbox/x.md"],
    )
    assert n == 0


@pytest.mark.asyncio
async def test_empty_eval_text_returns_zero():
    router = AsyncMock()
    n = await extract_and_store_eval_memories(
        db=AsyncMock(),
        store=AsyncMock(),
        router=router,
        evaluation_text="   ",
        source_files=["/inbox/x.md"],
    )
    assert n == 0
    router.route_call.assert_not_called()


@pytest.mark.asyncio
async def test_kill_switch_disables(monkeypatch):
    monkeypatch.setenv("GENESIS_INBOX_EVAL_MEMORY_DISABLED", "1")
    router = AsyncMock()
    n = await extract_and_store_eval_memories(
        db=AsyncMock(),
        store=AsyncMock(),
        router=router,
        evaluation_text="body",
        source_files=["/inbox/x.md"],
    )
    assert n == 0
    router.route_call.assert_not_called()
