from __future__ import annotations

import json

import pytest

from genesis.db.crud import observations, user_model
from genesis.memory.user_model import UserModelEvolver


async def _create_delta(
    db, *, id: str, field: str, value: str, confidence: float
) -> None:
    await observations.create(
        db,
        id=id,
        source="reflection",
        type="user_model_delta",
        content=json.dumps({
            "field": field,
            "value": value,
            "evidence": "test evidence",
            "confidence": confidence,
        }),
        priority="medium",
        created_at="2026-03-08T00:00:00",
    )


@pytest.mark.asyncio
async def test_process_no_deltas(db):
    evolver = UserModelEvolver(db=db)
    result = await evolver.process_pending_deltas()
    assert result is None


@pytest.mark.asyncio
async def test_process_high_confidence_auto_accepts(db):
    await _create_delta(db, id="d1", field="preferred_language", value="Python", confidence=0.8)
    evolver = UserModelEvolver(db=db)
    result = await evolver.process_pending_deltas()
    assert result is not None
    assert result.model["preferred_language"] == "Python"
    assert result.version == 1
    assert result.evidence_count == 1


@pytest.mark.asyncio
async def test_process_low_confidence_not_accepted(db):
    await _create_delta(db, id="d1", field="timezone", value="UTC", confidence=0.4)
    evolver = UserModelEvolver(db=db)
    result = await evolver.process_pending_deltas()
    assert result is None


@pytest.mark.asyncio
async def test_process_accumulation_accepts(db):
    for i in range(3):
        await _create_delta(
            db, id=f"d{i}", field="editor", value="vim", confidence=0.4
        )
    evolver = UserModelEvolver(db=db)
    result = await evolver.process_pending_deltas()
    assert result is not None
    assert result.model["editor"] == "vim"
    assert result.evidence_count == 3


@pytest.mark.asyncio
async def test_process_marks_resolved(db):
    await _create_delta(db, id="d1", field="os", value="Linux", confidence=0.9)
    evolver = UserModelEvolver(db=db)
    await evolver.process_pending_deltas()
    row = await observations.get_by_id(db, "d1")
    assert row["resolved"] == 1


@pytest.mark.asyncio
async def test_get_current_model_empty(db):
    evolver = UserModelEvolver(db=db)
    assert await evolver.get_current_model() is None


@pytest.mark.asyncio
async def test_get_current_model_exists(db):
    await user_model.upsert(
        db,
        model_json={"lang": "Python"},
        synthesized_at="2026-03-08T00:00:00",
        synthesized_by="test",
        evidence_count=5,
    )
    evolver = UserModelEvolver(db=db)
    snapshot = await evolver.get_current_model()
    assert snapshot is not None
    assert snapshot.model == {"lang": "Python"}
    assert snapshot.version == 1
    assert snapshot.evidence_count == 5


@pytest.mark.asyncio
async def test_get_model_summary_empty(db):
    evolver = UserModelEvolver(db=db)
    assert await evolver.get_model_summary() == "No user model established yet."


# ── synthesize_narrative tests ────────────────────────────────────────────


class _FakeRouter:
    """Minimal router stand-in for synthesize_narrative tests."""

    def __init__(self, *, content=None, success=True, error=None, raise_exc=None):
        self._content = content
        self._success = success
        self._error = error
        self._raise = raise_exc
        self.last_call_site_id = None
        self.last_messages = None
        self.call_count = 0

    async def route_call(self, call_site_id, messages):
        self.call_count += 1
        self.last_call_site_id = call_site_id
        self.last_messages = messages
        if self._raise is not None:
            raise self._raise
        from genesis.routing.types import RoutingResult

        return RoutingResult(
            success=self._success,
            call_site_id=call_site_id,
            content=self._content,
            error=self._error,
            input_tokens=42,
            output_tokens=128,
            cost_usd=0.0,
            provider_used="mistral-small-free",
        )


@pytest.mark.asyncio
async def test_synthesize_narrative_returns_none_when_model_empty(db):
    """No model in cache → no synthesis attempt, return None gracefully."""
    evolver = UserModelEvolver(db=db)
    router = _FakeRouter(content="never called")
    result = await evolver.synthesize_narrative(router)
    assert result is None
    assert router.call_count == 0


@pytest.mark.asyncio
async def test_synthesize_narrative_calls_router_with_call_site_11(db):
    """Synthesis must use call site 11_user_model_synthesis by default."""
    # Seed a model
    await user_model.upsert(
        db,
        model_json={"preferred_language": "Python", "timezone": "EST"},
        synthesized_at="2026-04-09T00:00:00",
        synthesized_by="test",
        evidence_count=5,
        last_change_type="seed",
    )
    evolver = UserModelEvolver(db=db)
    router = _FakeRouter(content="## Languages\n\nPython is the preferred language.")
    narrative = await evolver.synthesize_narrative(router, evidence_count=5)
    assert narrative is not None
    assert "Python" in narrative
    assert router.call_count == 1
    assert router.last_call_site_id == "11_user_model_synthesis"
    # The prompt should embed model fields and the evidence count
    msg_content = router.last_messages[0]["content"]
    assert "preferred language" in msg_content.lower()
    assert "5 evidence points" in msg_content


@pytest.mark.asyncio
async def test_synthesize_narrative_returns_none_on_failure(db):
    """Router failure → return None (callers fall back to rules-based)."""
    await user_model.upsert(
        db,
        model_json={"x": "y"},
        synthesized_at="2026-04-09T00:00:00",
        synthesized_by="test",
        evidence_count=1,
        last_change_type="seed",
    )
    evolver = UserModelEvolver(db=db)
    router = _FakeRouter(success=False, error="all providers exhausted")
    result = await evolver.synthesize_narrative(router)
    assert result is None


@pytest.mark.asyncio
async def test_synthesize_narrative_returns_none_on_empty_content(db):
    """Empty/whitespace content → return None (graceful fallback)."""
    await user_model.upsert(
        db,
        model_json={"x": "y"},
        synthesized_at="2026-04-09T00:00:00",
        synthesized_by="test",
        evidence_count=1,
        last_change_type="seed",
    )
    evolver = UserModelEvolver(db=db)
    router = _FakeRouter(content="   \n  \n")
    result = await evolver.synthesize_narrative(router)
    assert result is None


@pytest.mark.asyncio
async def test_synthesize_narrative_handles_router_exception(db):
    """Router raises → return None, do NOT propagate (caller-safe)."""
    await user_model.upsert(
        db,
        model_json={"x": "y"},
        synthesized_at="2026-04-09T00:00:00",
        synthesized_by="test",
        evidence_count=1,
        last_change_type="seed",
    )
    evolver = UserModelEvolver(db=db)
    router = _FakeRouter(raise_exc=RuntimeError("simulated network failure"))
    result = await evolver.synthesize_narrative(router)
    assert result is None


@pytest.mark.asyncio
async def test_synthesize_narrative_includes_evidence_in_prompt(db):
    """Recent resolved deltas must be threaded into the prompt as evidence."""
    # Seed deltas, then resolve them so they appear in the evidence query
    await _create_delta(db, id="d1", field="editor", value="vim", confidence=0.9)
    await _create_delta(db, id="d2", field="shell", value="zsh", confidence=0.9)
    evolver = UserModelEvolver(db=db)
    snapshot = await evolver.process_pending_deltas()
    assert snapshot is not None  # both should auto-accept

    router = _FakeRouter(content="## Tools\n\nUses vim and zsh.")
    narrative = await evolver.synthesize_narrative(router)
    assert narrative is not None

    msg_content = router.last_messages[0]["content"]
    # Both fields and their values must appear in evidence section
    assert "editor" in msg_content
    assert "vim" in msg_content
    assert "shell" in msg_content
    assert "zsh" in msg_content


def test_build_synthesis_prompt_truncates_long_values():
    """Very long field values must be truncated in the prompt to keep tokens bounded."""
    big_value = "x" * 5000
    prompt = UserModelEvolver._build_synthesis_prompt(
        {"big_field": big_value},
        recent_deltas=[],
        evidence_count=1,
    )
    # Should be much shorter than the raw value
    assert len(prompt) < 4000
    assert "..." in prompt


def test_build_synthesis_prompt_caps_evidence_count():
    """No more than _NARRATIVE_EVIDENCE_LIMIT delta entries in the prompt."""
    from genesis.memory.user_model import _NARRATIVE_EVIDENCE_LIMIT

    fake_deltas = [
        {
            "content": json.dumps({
                "field": f"field_{i}",
                "value": f"value_{i}",
                "evidence": f"evidence_{i}",
            }),
        }
        for i in range(_NARRATIVE_EVIDENCE_LIMIT * 3)
    ]
    prompt = UserModelEvolver._build_synthesis_prompt(
        {"x": "y"}, recent_deltas=fake_deltas, evidence_count=10,
    )
    # Should only contain the first _NARRATIVE_EVIDENCE_LIMIT entries
    assert prompt.count("field_") == _NARRATIVE_EVIDENCE_LIMIT
    assert "field_0" in prompt
    assert f"field_{_NARRATIVE_EVIDENCE_LIMIT - 1}" in prompt
    assert f"field_{_NARRATIVE_EVIDENCE_LIMIT}" not in prompt
