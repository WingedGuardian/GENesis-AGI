"""Tests for the deep-reflection JSON salvage retry.

A deep-reflection CC session sometimes ends its final message as free-form
PROSE with no fenced JSON. Both parse sites then fail and the cycle's cognitive
output (cognitive_state_update, observations, memory ops, surplus) is silently
discarded. The salvage re-derives the structured JSON via one routed LLM call.
"""

import json
from types import SimpleNamespace

import pytest

from genesis.awareness.types import Depth
from genesis.cc.reflection_bridge._output import (
    _SALVAGE_PROMPT,
    _extract_json_obj,
    format_topic_summary,
    salvage_deep_reflection_json,
)
from genesis.reflection.output_router import parse_deep_reflection_output

# A real-shaped failing sample: coherent prose, zero JSON (mirrors the live
# reflection_corpus failures — the model wrote a human wrap-up, not the schema).
PROSE = (
    "Deep reflection complete. Summary of what I found and did:\n\n"
    "Headline finding: the groq-EOL question is resolved — both models were "
    "deprecated 2026-06-17. I verified against the deprecation docs and closed "
    "the escalation. Everything else is healthy or already has a next step."
)

VALID_JSON = {
    "cognitive_state_update": "Groq EOL resolved; systems healthy.",
    "observations": ["groq-EOL resolved", "all jobs green"],
    "confidence": 0.8,
    "focus_next": "pick a replacement model",
}


class _FakeRouter:
    def __init__(self, content, *, success=True, raises=False):
        self._content = content
        self._success = success
        self._raises = raises
        self.calls: list = []

    async def route_call(self, call_site_id, messages, **kwargs):
        self.calls.append((call_site_id, messages, kwargs))
        if self._raises:
            raise RuntimeError("provider chain exhausted")
        return SimpleNamespace(success=self._success, content=self._content, error=None)


# ── _extract_json_obj ──────────────────────────────────────────────────


def test_extract_json_obj_variants():
    obj = {"a": 1}
    assert _extract_json_obj(json.dumps(obj)) == obj  # bare
    assert _extract_json_obj(f"```json\n{json.dumps(obj)}\n```") == obj  # ```json fence
    assert _extract_json_obj(f"```\n{json.dumps(obj)}\n```") == obj  # bare fence
    assert _extract_json_obj(PROSE) is None  # genuine prose
    assert _extract_json_obj("") is None
    assert _extract_json_obj("[1, 2, 3]") is None  # a list is not a reflection dict


def test_multi_fence_json_block_found_by_gate_and_parser():
    """A deep reflection that quotes a ```diff block BEFORE its real ```json
    block must be parsed via the JSON-tagged block — by BOTH the salvage gate
    and the production parser. (Regression: the parser used to grab the FIRST
    fence, fail on the diff, and discard the whole reflection while the gate
    thought it was fine — so salvage was skipped AND routing failed.)"""
    payload = {"cognitive_state_update": "state", "observations": ["o1"]}
    multi_fence = (
        "Here is the diff I reviewed:\n"
        "```diff\n-old\n+new\n```\n\n"
        "Summary:\n"
        f"```json\n{json.dumps(payload)}\n```"
    )
    # Gate finds the json block (so salvage correctly skips)...
    assert _extract_json_obj(multi_fence) == payload
    # ...AND the production parser now recovers it instead of parse_failed.
    parsed = parse_deep_reflection_output(multi_fence)
    assert not parsed.parse_failed
    assert parsed.cognitive_state_update == "state"


# ── salvage_deep_reflection_json ───────────────────────────────────────


@pytest.mark.asyncio
async def test_salvage_noop_when_already_parseable():
    """Already-JSON output must NOT trigger a salvage call (happy path untouched)."""
    router = _FakeRouter(json.dumps(VALID_JSON))
    fenced = f"```json\n{json.dumps(VALID_JSON)}\n```"
    assert await salvage_deep_reflection_json(fenced, router) is None
    assert await salvage_deep_reflection_json(json.dumps(VALID_JSON), router) is None
    assert router.calls == []  # no LLM call made


@pytest.mark.asyncio
async def test_salvage_prose_produces_parseable_json():
    """Prose in + a router that returns valid JSON → a fenced string that BOTH
    the router parser and the topic-summary parser accept."""
    router = _FakeRouter(json.dumps(VALID_JSON))
    salvaged = await salvage_deep_reflection_json(PROSE, router)
    assert salvaged is not None
    assert len(router.calls) == 1
    assert router.calls[0][0] == "41_reflection_json_salvage"
    # Router parser accepts it and recovers the real content.
    parsed = parse_deep_reflection_output(salvaged)
    assert not parsed.parse_failed
    assert parsed.cognitive_state_update == VALID_JSON["cognitive_state_update"]
    assert parsed.observations == VALID_JSON["observations"]
    # Topic-summary parser (strict ```json) also accepts it.
    assert _extract_json_obj(salvaged) == VALID_JSON


@pytest.mark.asyncio
async def test_salvage_returns_none_when_router_unset():
    assert await salvage_deep_reflection_json(PROSE, None) is None


@pytest.mark.asyncio
async def test_salvage_returns_none_on_router_failure():
    assert await salvage_deep_reflection_json(PROSE, _FakeRouter(None, success=False)) is None


@pytest.mark.asyncio
async def test_salvage_returns_none_when_router_raises():
    assert await salvage_deep_reflection_json(PROSE, _FakeRouter(None, raises=True)) is None


@pytest.mark.asyncio
async def test_salvage_returns_none_when_output_still_prose():
    """If the salvage model ALSO returns prose, salvage fails closed → None
    (caller keeps the existing parse-failed behavior; strictly additive)."""
    assert await salvage_deep_reflection_json(PROSE, _FakeRouter("still just prose, sorry")) is None


# ── format_topic_summary text override ─────────────────────────────────


def test_topic_summary_falls_back_on_prose():
    out = SimpleNamespace(text=PROSE)
    summary = format_topic_summary(Depth.DEEP, out)
    assert "was not parseable" in summary  # the stub (pre-salvage behavior)


def test_topic_summary_uses_salvaged_text_override():
    """With the salvaged JSON passed as `text`, the topic shows a REAL summary
    instead of the not-parseable stub — even though output.text is still prose."""
    out = SimpleNamespace(text=PROSE)
    salvaged = f"```json\n{json.dumps(VALID_JSON)}\n```"
    summary = format_topic_summary(Depth.DEEP, out, text=salvaged)
    assert "was not parseable" not in summary
    assert "Groq EOL resolved" in summary


def test_topic_summary_handles_bare_untagged_fence():
    """A bare (untagged) ``` fence routes fine via the canonical extractor, so
    the topic must NOT fall back to the stub for it (it used to, because
    format_topic_summary had its own strict ```json-only parser)."""
    out = SimpleNamespace(text=f"```\n{json.dumps(VALID_JSON)}\n```")
    summary = format_topic_summary(Depth.DEEP, out)
    assert "was not parseable" not in summary
    assert "Groq EOL resolved" in summary


# ── salvage schema parity (Codex #1333 finding B) ──────────────────────


def test_salvage_prompt_exposes_route_consumed_action_fields():
    """The salvage prompt MUST expose every canonical field that route()
    actually acts on — else a prose-ending reflection that raised a user
    question or a procedure quarantine would have those actions silently
    dropped while salvage still reports success. user_question →
    Telegram notification; procedure_quarantines → a real DB quarantine."""
    for field in (
        "cognitive_state_update",
        "observations",
        "memory_operations",
        "surplus_task_requests",
        "user_question",
        "procedure_quarantines",
        "skill_triggers",
        "contradictions",
        "confidence",
    ):
        assert field in _SALVAGE_PROMPT, f"salvage prompt omits route-consumed field {field!r}"


def test_salvage_prompt_requires_confidence():
    """confidence is canonical-required (REFLECTION_DEEP.md) and gates output —
    a salvage that leaves it optional lets the model omit it, the parser then
    substitutes the 0.5 sentinel, and the confidence gate passes it uncalibrated.
    It must sit in the Required block, not Optional."""
    required_block = _SALVAGE_PROMPT.split("Optional")[0]
    assert "confidence" in required_block


@pytest.mark.asyncio
async def test_salvage_roundtrips_user_question_and_quarantines():
    """A salvaged object carrying a user_question and a procedure_quarantine
    must survive the router parser intact (the fields route() acts on)."""
    payload = {
        "cognitive_state_update": "A decision needs the owner; a procedure is failing.",
        "observations": ["proc X success rate collapsed"],
        "confidence": 0.7,
        "user_question": {
            "text": "Retire procedure X?",
            "context": "success rate dropped to 20%",
            "options": ["retire", "keep"],
        },
        "procedure_quarantines": [{"procedure_id": "proc-x", "reason": "declining effectiveness"}],
    }
    router = _FakeRouter(json.dumps(payload))
    salvaged = await salvage_deep_reflection_json(PROSE, router)
    assert salvaged is not None
    parsed = parse_deep_reflection_output(salvaged)
    assert not parsed.parse_failed
    assert parsed.user_question is not None
    assert parsed.user_question.text == "Retire procedure X?"
    assert parsed.procedure_quarantines == [
        {"procedure_id": "proc-x", "reason": "declining effectiveness"}
    ]
