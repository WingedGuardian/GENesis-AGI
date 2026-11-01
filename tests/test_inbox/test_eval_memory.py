"""Tests for deterministic inbox-eval memory persistence (inbox.eval_memory)."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from genesis.inbox.eval_memory import (
    MAX_EVAL_MEMORIES,
    EvalMemory,
    EvalMemoryOutcome,
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

    outcome = await extract_and_store_eval_memories(
        db=AsyncMock(),
        store=store,
        router=router,
        evaluation_text="... User is into RevOps automation ...",
        source_files=["/inbox/New Genesis Capabilities.md"],
        session_id="sess-9",
    )
    assert outcome.stored == 1
    assert outcome.status == "stored"
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
    outcome = await extract_and_store_eval_memories(
        db=AsyncMock(),
        store=store,
        router=router,
        evaluation_text="dup insight text",
        source_files=["/inbox/x.md"],
    )
    assert outcome.stored == 0
    assert outcome.status == "empty"
    assert outcome.reason == "all_duplicate"
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
async def test_outcome_router_unsuccessful(monkeypatch):
    store = AsyncMock()
    router = AsyncMock()
    router.route_call = AsyncMock(return_value=_routing(success=False, error="chain exhausted"))
    outcome = await extract_and_store_eval_memories(
        db=AsyncMock(),
        store=store,
        router=router,
        evaluation_text="body",
        source_files=["/inbox/x.md"],
    )
    assert outcome == EvalMemoryOutcome("failed", "router_unsuccessful", 0, 0)
    store.store.assert_not_called()


@pytest.mark.asyncio
async def test_outcome_route_exception(monkeypatch):
    store = AsyncMock()
    router = AsyncMock()
    router.route_call = AsyncMock(side_effect=RuntimeError("network"))
    outcome = await extract_and_store_eval_memories(
        db=AsyncMock(),
        store=store,
        router=router,
        evaluation_text="body",
        source_files=["/inbox/x.md"],
    )
    assert outcome == EvalMemoryOutcome("failed", "route_exception", 0, 0)


@pytest.mark.asyncio
async def test_outcome_empty_input():
    router = AsyncMock()
    outcome = await extract_and_store_eval_memories(
        db=AsyncMock(),
        store=AsyncMock(),
        router=router,
        evaluation_text="   ",
        source_files=["/inbox/x.md"],
    )
    assert outcome == EvalMemoryOutcome("skipped", "empty_input", 0, 0)
    router.route_call.assert_not_called()


@pytest.mark.asyncio
async def test_outcome_kill_switch(monkeypatch):
    monkeypatch.setenv("GENESIS_INBOX_EVAL_MEMORY_DISABLED", "1")
    router = AsyncMock()
    outcome = await extract_and_store_eval_memories(
        db=AsyncMock(),
        store=AsyncMock(),
        router=router,
        evaluation_text="body",
        source_files=["/inbox/x.md"],
    )
    assert outcome == EvalMemoryOutcome("skipped", "kill_switch", 0, 0)
    router.route_call.assert_not_called()


@pytest.mark.asyncio
async def test_outcome_parse_failure(_patch_deps, caplog):
    store = AsyncMock()
    router = AsyncMock()
    router.route_call = AsyncMock(return_value=_routing(content="not json at all"))
    with caplog.at_level(logging.WARNING):
        outcome = await extract_and_store_eval_memories(
            db=AsyncMock(),
            store=store,
            router=router,
            evaluation_text="body",
            source_files=["/inbox/x.md"],
        )
    assert outcome == EvalMemoryOutcome("failed", "parse", 0, 0)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("unparseable" in r.getMessage() for r in warnings)
    assert all("not json" not in r.getMessage() for r in warnings)
    store.store.assert_not_called()


@pytest.mark.asyncio
async def test_outcome_valid_empty_array(_patch_deps):
    router = AsyncMock()
    router.route_call = AsyncMock(return_value=_routing(content="[]"))
    outcome = await extract_and_store_eval_memories(
        db=AsyncMock(),
        store=AsyncMock(),
        router=router,
        evaluation_text="body",
        source_files=["/inbox/x.md"],
    )
    assert outcome == EvalMemoryOutcome("empty", "", 0, 0)


@pytest.mark.asyncio
async def test_outcome_all_duplicate(monkeypatch):
    monkeypatch.setattr(
        "genesis.memory.extraction_job.check_claim_duplicate",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "genesis.memory.source_verification.verify_source_overlap",
        lambda *a, **k: SimpleNamespace(verified=True, overlap=1.0),
    )
    router = AsyncMock()
    router.route_call = AsyncMock(
        return_value=_routing(
            content=_json_array(
                {"content": "dup one", "kind": "user_signal", "tags": [], "confidence": 0.7},
                {"content": "dup two", "kind": "user_signal", "tags": [], "confidence": 0.7},
            ),
        )
    )
    outcome = await extract_and_store_eval_memories(
        db=AsyncMock(),
        store=AsyncMock(),
        router=router,
        evaluation_text="dup text",
        source_files=["/inbox/x.md"],
    )
    assert outcome == EvalMemoryOutcome("empty", "all_duplicate", 2, 0)


@pytest.mark.asyncio
async def test_outcome_store_failure(_patch_deps):
    store = AsyncMock()
    store.store = AsyncMock(side_effect=RuntimeError("db down"))
    router = AsyncMock()
    router.route_call = AsyncMock(
        return_value=_routing(
            content=_json_array(
                {"content": "an insight", "kind": "user_signal", "tags": [], "confidence": 0.7},
            ),
        )
    )
    outcome = await extract_and_store_eval_memories(
        db=AsyncMock(),
        store=store,
        router=router,
        evaluation_text="an insight",
        source_files=["/inbox/x.md"],
    )
    assert outcome == EvalMemoryOutcome("failed", "store", 1, 0)


@pytest.mark.asyncio
async def test_outcome_stored(_patch_deps):
    store = AsyncMock()
    store.store = AsyncMock(return_value="m")
    router = AsyncMock()
    router.route_call = AsyncMock(
        return_value=_routing(
            content=_json_array(
                {"content": "insight one", "kind": "user_signal", "tags": [], "confidence": 0.7},
                {"content": "insight two", "kind": "user_signal", "tags": [], "confidence": 0.7},
            ),
        )
    )
    outcome = await extract_and_store_eval_memories(
        db=AsyncMock(),
        store=store,
        router=router,
        evaluation_text="insight one insight two",
        source_files=["/inbox/x.md"],
    )
    assert outcome == EvalMemoryOutcome("stored", "", 2, 2)


def test_parse_eval_memory_response_unchanged_on_garbage():
    assert parse_eval_memory_response("not json at all") == []
    assert parse_eval_memory_response("") == []


# ── Monitor emission ──────────────────────────────────────────────────────


def _monitor(event_bus):
    from genesis.inbox.monitor import InboxMonitor

    mon = InboxMonitor.__new__(InboxMonitor)
    mon._db = AsyncMock()
    mon._memory_store = AsyncMock()
    mon._router = AsyncMock()
    mon._event_bus = event_bus
    return mon


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "event_type", "severity"),
    [
        (EvalMemoryOutcome("stored", "", 2, 2), "memory.persisted", "INFO"),
        (EvalMemoryOutcome("empty", "all_duplicate", 2, 0), "memory.extraction_outcome", "INFO"),
        (EvalMemoryOutcome("skipped", "kill_switch", 0, 0), "memory.extraction_outcome", "INFO"),
        (EvalMemoryOutcome("failed", "parse", 0, 0), "memory.extraction_outcome", "WARNING"),
    ],
)
async def test_persist_emits_one_event_per_outcome(monkeypatch, outcome, event_type, severity):
    from genesis.observability.types import Severity

    monkeypatch.setattr(
        "genesis.inbox.eval_memory.extract_and_store_eval_memories",
        AsyncMock(return_value=outcome),
    )
    event_bus = AsyncMock()
    await _monitor(event_bus)._persist_eval_memories(
        "eval text", "batch-1234", "sess-1", ["/inbox/x.md"]
    )
    event_bus.emit.assert_awaited_once()
    args, kwargs = event_bus.emit.call_args
    assert args[2] == event_type
    assert args[1] == getattr(Severity, severity)
    assert kwargs["batch_id"] == "batch-1234"
    assert kwargs["status"] == outcome.status
    assert kwargs["reason"] == outcome.reason
    assert kwargs["extracted"] == outcome.extracted
    assert kwargs["stored"] == outcome.stored
    assert kwargs["count"] == outcome.stored


@pytest.mark.asyncio
async def test_persist_never_raises(monkeypatch):
    monkeypatch.setattr(
        "genesis.inbox.eval_memory.extract_and_store_eval_memories",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    event_bus = AsyncMock()
    await _monitor(event_bus)._persist_eval_memories(
        "eval text", "batch-1234", "sess-1", ["/inbox/x.md"]
    )
    event_bus.emit.assert_not_called()
