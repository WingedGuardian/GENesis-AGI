"""Tests for deliberate() — the chorus. litellm is mocked; no live calls."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from genesis.deliberation import DeliberationResult, deliberate
from genesis.deliberation.backends.fusion import (
    FusionBackend,
    _extract_cost,
    _normalize,
    _parse_content,
)

# ── fixtures: real-shaped litellm responses ──────────────────────────────────
# PROSE = the actual probe output (2026-06-24): bare model-slug returns synthesized
# MARKDOWN in message.content, no structured fields.
PROSE = (
    "# Recommendation: Cut now — but cut surgically\n\n"
    "**B is a trap dressed up as ambition** and the math gives it away.\n"
    "## When B is actually defensible\nOnly if PMF is strong and investors are already warm."
)
JSON_OK = (
    '{"answer": "Cut now, surgically.", "consensus": "Time and runway beat headcount.", '
    '"dissent": ["B is right if PMF is strong and a Series A investor is already warm."], '
    '"confidence": 0.82}'
)
JSON_FENCED = "```json\n" + JSON_OK + "\n```"
JSON_IN_PROSE = "Here is the panel's verdict.\n\n" + JSON_OK + "\n\nHope that helps."


def _resp(content, cost=None):
    """Mimic the litellm ModelResponse shape the backend reads."""
    msg = SimpleNamespace(content=content)
    usage = SimpleNamespace(cost=cost) if cost is not None else SimpleNamespace()
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage, _hidden_params={})


# ── parser / normalizer ──────────────────────────────────────────────────────


def test_parse_prose_returns_empty():
    assert _parse_content(PROSE) == {}


def test_parse_json():
    p = _parse_content(JSON_OK)
    assert p["answer"].startswith("Cut now")
    assert p["consensus"]
    assert p["dissent"] == ["B is right if PMF is strong and a Series A investor is already warm."]
    assert p["confidence"] == 0.82


def test_parse_fenced_json():
    p = _parse_content(JSON_FENCED)
    assert p["answer"].startswith("Cut now")
    assert len(p["dissent"]) == 1


def test_parse_json_embedded_in_prose():
    p = _parse_content(JSON_IN_PROSE)
    assert p.get("answer", "").startswith("Cut now")


def test_confidence_clamped():
    assert _parse_content('{"answer":"x","confidence":1.7}')["confidence"] == 1.0


def test_dissent_string_coerced_to_list():
    assert _parse_content('{"answer":"x","dissent":"single point"}')["dissent"] == ["single point"]


def test_normalize_prose_uses_content_as_answer():
    r = _normalize(_resp(PROSE, cost=0.3211), latency=80.0)
    assert r.ok
    assert r.answer.startswith("# Recommendation")
    assert r.dissent == ()
    assert r.cost_usd == 0.3211 and r.cost_known
    assert r.latency_s == 80.0


def test_normalize_json_structured():
    r = _normalize(_resp(JSON_OK, cost=0.05), latency=70.0)
    assert r.answer.startswith("Cut now")
    assert r.consensus and len(r.dissent) == 1 and r.confidence == 0.82


def test_normalize_empty_content_errors():
    r = _normalize(_resp("", cost=0.01), latency=1.0)
    assert not r.ok and "empty" in r.error


def test_extract_cost_from_usage():
    cost, known = _extract_cost(_resp("x", cost=0.42))
    assert cost == 0.42 and known


def test_extract_cost_unknown():
    cost, known = _extract_cost(_resp("x"))
    assert cost == 0.0 and not known


# ── FusionBackend.run (litellm mocked) ───────────────────────────────────────


async def test_fusion_happy(monkeypatch):
    monkeypatch.setenv("API_KEY_OPENROUTER", "test-key")
    with patch(
        "genesis.deliberation.backends.fusion.litellm.acompletion",
        new=AsyncMock(return_value=_resp(JSON_OK, cost=0.05)),
    ) as m:
        r = await FusionBackend().run("q", stakes="high")
    assert r.ok and r.answer.startswith("Cut now")
    assert r.cost_usd == 0.05 and r.cost_known and isinstance(r.latency_s, float)
    kwargs = m.call_args.kwargs
    assert "HIGH-STAKES" in kwargs["messages"][0]["content"]  # stakes strengthens system prompt
    assert kwargs["max_tokens"] == 2000  # explicit max_tokens (the 402 guard)
    assert kwargs["model"] == "openrouter/openrouter/fusion"


async def test_fusion_no_key(monkeypatch):
    for var in ("API_KEY_OPENROUTER", "OPENROUTER_API_KEY", "OPENROUTER_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    r = await FusionBackend().run("q")
    assert not r.ok and "API key" in r.error


async def test_fusion_error_graceful(monkeypatch):
    monkeypatch.setenv("API_KEY_OPENROUTER", "test-key")
    with patch(
        "genesis.deliberation.backends.fusion.litellm.acompletion",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        r = await FusionBackend().run("q")
    assert not r.ok and "fusion call failed" in r.error


async def test_fusion_timeout_graceful(monkeypatch):
    monkeypatch.setenv("API_KEY_OPENROUTER", "test-key")
    with patch(
        "genesis.deliberation.backends.fusion.litellm.acompletion",
        new=AsyncMock(side_effect=TimeoutError()),
    ):
        r = await FusionBackend().run("q", timeout_s=1.0)
    assert not r.ok and "timed out" in r.error


# ── deliberate() core ────────────────────────────────────────────────────────


async def test_empty_question():
    r = await deliberate("   ")
    assert not r.ok and "required" in r.error


async def test_unknown_backend():
    r = await deliberate("q", backend="nope")
    assert not r.ok and "unknown backend" in r.error


async def test_recursion_blocked(monkeypatch):
    captured = {}

    class Stub:
        name = "stub"

        async def run(self, q, **kw):
            captured["inner"] = await deliberate("again", backend="stub")
            return DeliberationResult(answer="outer")

    monkeypatch.setattr("genesis.deliberation.core.get_backend", lambda n: Stub())
    out = await deliberate("x", backend="stub")
    assert out.answer == "outer"
    assert "recursion-blocked" in captured["inner"].error


async def test_recursion_resets_after_call(monkeypatch):
    class Stub:
        name = "stub"

        async def run(self, q, **kw):
            return DeliberationResult(answer="ok")

    monkeypatch.setattr("genesis.deliberation.core.get_backend", lambda n: Stub())
    await deliberate("x", backend="stub")
    r2 = await deliberate("y", backend="stub")
    assert r2.answer == "ok"


async def test_backend_receives_stakes_and_context(monkeypatch):
    seen = {}

    class Stub:
        name = "stub"

        async def run(self, q, *, context, stakes, timeout_s, models):
            seen.update(q=q, context=context, stakes=stakes)
            return DeliberationResult(answer="ok")

    monkeypatch.setattr("genesis.deliberation.core.get_backend", lambda n: Stub())
    await deliberate("decide", context="bg", stakes="high", backend="stub")
    assert seen == {"q": "decide", "context": "bg", "stakes": "high"}


# ── MCP tool ─────────────────────────────────────────────────────────────────


async def test_deliberate_tool_registered():
    from genesis.mcp.health_mcp import mcp

    tools = await mcp.get_tools()
    assert "deliberate" in tools


async def test_mcp_impl_graceful(monkeypatch):
    import genesis.deliberation as dl

    async def fake(q, **kw):
        return DeliberationResult(
            answer="A", consensus="C", dissent=("d1",), confidence=0.5,
            cost_usd=0.1, cost_known=True, latency_s=70.0,
        )

    monkeypatch.setattr(dl, "deliberate", fake)
    from genesis.mcp.health import deliberation_tools as dt

    out = await dt._impl_deliberate("q")
    assert out["answer"] == "A"
    assert out["dissent"] == ["d1"]
    assert out["cost_known"] is True
    assert out["latency_s"] == 70.0
