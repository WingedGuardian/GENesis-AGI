"""Tests for deliberate() — the chorus. The HTTP layer (_consume_stream) is mocked; no live calls."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx

from genesis.deliberation import DeliberationResult, deliberate
from genesis.deliberation.backends.fusion import (
    _PRESET_PANELS,
    FusionBackend,
    _normalize,
    _parse_content,
    _parse_sse,
    _resolve_preset,
    _resolve_stakes,
)

# ── fixtures: real-shaped content ────────────────────────────────────────────
# PROSE = the actual probe output (2026-06-24): the fusion model-slug returns synthesized
# MARKDOWN, no structured fields.
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
# ANALYSIS mode: the orchestrator returns clean structured JSON (real probe output, 2026-06-24).
ANALYSIS_JSON = (
    '{"answer": "Choose A — harden reliability; reserve ~20-30% for one high-impact feature.", '
    '"consensus": "All three models agreed churn is the biggest risk, usually from reliability.", '
    '"dissent": ["If churn is a missing-feature problem, one feature beats reliability.", '
    '"A pure tech-debt quarter can unsettle investors and demotivate the team.", '
    '"Not all debt causes churn; target paydown surgically."], '
    '"blind_spots": ["Concrete churn-reason data.", "Runway/burn and fundraise timing."], '
    '"confidence": 0.81}'
)


def _http_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError(f"status {status}", request=req, response=resp)


# ── SSE parser ───────────────────────────────────────────────────────────────


def test_parse_sse_assembles_content_and_cost():
    lines = [
        ": OPENROUTER PROCESSING",  # keep-alive comment
        "",
        'data: {"choices":[{"delta":{"content":"Hello "}}]}',
        'data: {"choices":[{"delta":{"content":"world"}}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":10,"cost":0.1839}}',
        "data: [DONE]",
    ]
    content, cost, err = _parse_sse(lines)
    assert content == "Hello world"
    assert cost == 0.1839
    assert err is None


def test_parse_sse_skips_comments_and_malformed():
    lines = [": keep-alive", "", "data: not-json", 'data: {"choices":[{"delta":{"content":"ok"}}]}']
    content, cost, _ = _parse_sse(lines)
    assert content == "ok" and cost is None


def test_parse_sse_stops_at_done():
    lines = [
        'data: {"choices":[{"delta":{"content":"keep"}}]}',
        "data: [DONE]",
        'data: {"choices":[{"delta":{"content":"DROP"}}]}',
    ]
    content, _, _ = _parse_sse(lines)
    assert content == "keep"


def test_parse_sse_cost_bool_rejected():
    content, cost, _ = _parse_sse(['data: {"choices":[],"usage":{"cost":true}}'])
    assert content == "" and cost is None


def test_parse_sse_captures_in_stream_error():
    lines = [
        ": OPENROUTER PROCESSING",
        'data: {"choices":[],"error":{"code":502,"message":"panel upstream failed"}}',
    ]
    content, cost, err = _parse_sse(lines)
    assert content == "" and cost is None
    assert err == "panel upstream failed"


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


def test_parse_blind_spots():
    assert _parse_content('{"answer":"x","blind_spots":["b1","b2"]}')["blind_spots"] == ["b1", "b2"]


def test_normalize_prose_uses_content_as_answer():
    r = _normalize(PROSE, 0.3211, None, 80.0, "synthesis", "budget")
    assert r.ok
    assert r.answer.startswith("# Recommendation")
    assert r.dissent == ()
    assert r.cost_usd == 0.3211 and r.cost_known
    assert r.latency_s == 80.0
    assert r.backend_used == "fusion/synthesis" and r.preset_used == "budget"


def test_normalize_json_structured():
    r = _normalize(JSON_OK, 0.05, None, 70.0, "synthesis", "budget")
    assert r.answer.startswith("Cut now")
    assert r.consensus and len(r.dissent) == 1 and r.confidence == 0.82


def test_normalize_analysis_structured():
    r = _normalize(ANALYSIS_JSON, 0.12, None, 173.0, "analysis", "strong")
    assert r.answer.startswith("Choose A")
    assert len(r.dissent) == 3
    assert len(r.blind_spots) == 2
    assert r.confidence == 0.81
    assert r.backend_used == "fusion/analysis" and r.preset_used == "strong"
    assert r.cost_usd == 0.12 and r.cost_known


def test_normalize_cost_unknown():
    r = _normalize(PROSE, None, None, 80.0, "synthesis", "budget")
    assert r.ok and r.cost_usd == 0.0 and not r.cost_known


def test_normalize_empty_content_errors():
    r = _normalize("", 0.01, None, 1.0, "synthesis", "budget")
    assert not r.ok and "no content" in r.error


def test_normalize_empty_surfaces_stream_error():
    r = _normalize("", None, "panel exploded", 1.0, "analysis", "strong")
    assert not r.ok and "no content" in r.error and "panel exploded" in r.error


def test_normalize_content_with_trailing_error_stays_ok():
    # a complete/usable verdict alongside a late in-stream error is NOT false-failed (error is logged)
    r = _normalize("a real verdict", 0.1, "late panel error", 50.0, "synthesis", "strong")
    assert r.ok and r.answer == "a real verdict"


def test_resolve_preset_returns_panels_and_defaults():
    # explicit synthesis presets pick the matching custom panel
    assert _resolve_preset("strong", "synthesis") == ("strong", _PRESET_PANELS["strong"])
    assert _resolve_preset("budget", "synthesis") == ("budget", _PRESET_PANELS["budget"])
    # synthesis defaults to budget when preset is None/unknown
    assert _resolve_preset(None, "synthesis") == ("budget", _PRESET_PANELS["budget"])
    assert _resolve_preset("bogus", "synthesis") == ("budget", _PRESET_PANELS["budget"])
    # analysis is ALWAYS strong, ignoring any requested preset
    assert _resolve_preset(None, "analysis") == ("strong", _PRESET_PANELS["strong"])
    assert _resolve_preset("budget", "analysis") == ("strong", _PRESET_PANELS["strong"])


def test_resolve_stakes_auto_couples():
    # an explicit normal/high always wins
    assert _resolve_stakes("normal", "analysis", "strong") == "normal"
    assert _resolve_stakes("high", "synthesis", "budget") == "high"
    # auto-couple when stakes is None/unknown: analysis→high, synthesis strong→high, budget→normal
    assert _resolve_stakes(None, "analysis", "strong") == "high"
    assert _resolve_stakes(None, "synthesis", "strong") == "high"
    assert _resolve_stakes(None, "synthesis", "budget") == "normal"
    assert _resolve_stakes("", "synthesis", "budget") == "normal"
    assert _resolve_stakes("garbage", "synthesis", "strong") == "high"


# ── FusionBackend.run (HTTP layer mocked) ────────────────────────────────────
# Patch _consume_stream → (content, cost); assert the request body shape + error mapping.
_STREAM = "genesis.deliberation.backends.fusion._consume_stream"


async def test_fusion_happy(monkeypatch):
    monkeypatch.setenv("API_KEY_OPENROUTER", "test-key")
    with patch(_STREAM, new=AsyncMock(return_value=(JSON_OK, 0.05, None))) as m:
        r = await FusionBackend().run("q", stakes="high")
    assert r.ok and r.answer.startswith("Cut now")
    assert r.cost_usd == 0.05 and r.cost_known and isinstance(r.latency_s, float)
    assert r.preset_used == "budget"  # synthesis default preset
    body = m.call_args.args[0]
    assert "HIGH-STAKES" in body["messages"][0]["content"]  # stakes strengthens system prompt
    assert body["max_tokens"] == 2000  # explicit max_tokens (the 402 guard)
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["model"] == "openrouter/fusion"
    plugin = body["plugins"][0]
    assert plugin["id"] == "fusion"  # synthesis default preset → budget panel
    assert plugin["analysis_models"] == _PRESET_PANELS["budget"]["analysis_models"]
    assert plugin["model"] == _PRESET_PANELS["budget"]["model"]


async def test_fusion_analysis_mode_request(monkeypatch):
    monkeypatch.setenv("API_KEY_OPENROUTER", "test-key")
    with patch(_STREAM, new=AsyncMock(return_value=(ANALYSIS_JSON, 0.12, None))) as m:
        r = await FusionBackend().run("q", mode="analysis")
    assert r.ok and len(r.dissent) == 3 and r.backend_used == "fusion/analysis"
    assert r.preset_used == "strong"  # analysis default preset
    body = m.call_args.args[0]
    assert body["model"] == "openai/gpt-oss-120b:free"
    assert body["tools"][0]["type"] == "openrouter:fusion"
    params = body["tools"][0]["parameters"]  # analysis default preset → strong panel
    assert params["analysis_models"] == _PRESET_PANELS["strong"]["analysis_models"]
    assert params["model"] == _PRESET_PANELS["strong"]["model"]
    assert body["tool_choice"] == "required"


async def test_fusion_analysis_always_strong(monkeypatch):
    """analysis pins the strong panel even when budget is requested."""
    monkeypatch.setenv("API_KEY_OPENROUTER", "test-key")
    with patch(_STREAM, new=AsyncMock(return_value=(ANALYSIS_JSON, 0.12, None))) as m:
        r = await FusionBackend().run("q", mode="analysis", preset="budget")
    assert r.preset_used == "strong"
    params = m.call_args.args[0]["tools"][0]["parameters"]
    assert params["analysis_models"] == _PRESET_PANELS["strong"]["analysis_models"]
    assert params["model"] == _PRESET_PANELS["strong"]["model"]


async def test_fusion_synthesis_preset_override(monkeypatch):
    """synthesis honors an explicit strong preset → the frontier panel."""
    monkeypatch.setenv("API_KEY_OPENROUTER", "test-key")
    with patch(_STREAM, new=AsyncMock(return_value=(JSON_OK, 0.05, None))) as m:
        r = await FusionBackend().run("q", preset="strong")
    assert r.preset_used == "strong"
    plugin = m.call_args.args[0]["plugins"][0]
    assert plugin["analysis_models"] == _PRESET_PANELS["strong"]["analysis_models"]
    assert plugin["model"] == _PRESET_PANELS["strong"]["model"]


async def test_fusion_stakes_auto_couples(monkeypatch):
    """Default stakes (None) auto-couples to the system prompt: budget synthesis stays normal,
    strong synthesis and analysis go high; an explicit value overrides."""
    monkeypatch.setenv("API_KEY_OPENROUTER", "test-key")

    async def _system_prompt(**kw):
        with patch(_STREAM, new=AsyncMock(return_value=(JSON_OK, 0.05, None))) as m:
            await FusionBackend().run("q", **kw)
        return m.call_args.args[0]["messages"][0]["content"]

    assert "HIGH-STAKES" not in await _system_prompt()  # synthesis + budget default → normal
    assert "HIGH-STAKES" in await _system_prompt(preset="strong")  # synthesis + strong → high
    assert "HIGH-STAKES" in await _system_prompt(mode="analysis")  # analysis → high
    assert "HIGH-STAKES" not in await _system_prompt(mode="analysis", stakes="normal")  # override


async def test_fusion_no_key(monkeypatch):
    for var in ("API_KEY_OPENROUTER", "OPENROUTER_API_KEY", "OPENROUTER_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    r = await FusionBackend().run("q")
    assert not r.ok and "API key" in r.error


async def test_fusion_error_graceful(monkeypatch):
    monkeypatch.setenv("API_KEY_OPENROUTER", "test-key")
    with patch(_STREAM, new=AsyncMock(side_effect=RuntimeError("boom"))):
        r = await FusionBackend().run("q")
    assert not r.ok and "call failed" in r.error


async def test_fusion_timeout_graceful(monkeypatch):
    monkeypatch.setenv("API_KEY_OPENROUTER", "test-key")
    with patch(_STREAM, new=AsyncMock(side_effect=httpx.ReadTimeout("slow"))):
        r = await FusionBackend().run("q", timeout_s=1.0)
    assert not r.ok and "timed out" in r.error


async def test_fusion_http_400_no_retry(monkeypatch):
    monkeypatch.setenv("API_KEY_OPENROUTER", "test-key")
    mock = AsyncMock(side_effect=_http_error(400))
    with patch(_STREAM, new=mock):
        r = await FusionBackend().run("q")
    assert not r.ok and "call failed" in r.error and "status=400" in r.error
    assert mock.call_count == 1  # 4xx is not retried


async def test_fusion_http_429_retries(monkeypatch):
    monkeypatch.setenv("API_KEY_OPENROUTER", "test-key")
    monkeypatch.setattr("genesis.deliberation.backends.fusion._BACKOFF_S", 0.0)
    mock = AsyncMock(side_effect=[_http_error(429), _http_error(429), (JSON_OK, 0.05, None)])
    with patch(_STREAM, new=mock):
        r = await FusionBackend().run("q")
    assert r.ok and r.answer.startswith("Cut now")
    assert mock.call_count == 3  # 1 + 2 retries


async def test_fusion_transport_error_retries(monkeypatch):
    monkeypatch.setenv("API_KEY_OPENROUTER", "test-key")
    monkeypatch.setattr("genesis.deliberation.backends.fusion._BACKOFF_S", 0.0)
    mock = AsyncMock(side_effect=[httpx.ConnectError("reset"), (JSON_OK, 0.05, None)])
    with patch(_STREAM, new=mock):
        r = await FusionBackend().run("q")
    assert r.ok and mock.call_count == 2


async def test_fusion_retries_on_empty_stream(monkeypatch):
    """A transient empty / in-stream-error result gets one more shot, then succeeds."""
    monkeypatch.setenv("API_KEY_OPENROUTER", "test-key")
    monkeypatch.setattr("genesis.deliberation.backends.fusion._BACKOFF_S", 0.0)
    mock = AsyncMock(side_effect=[("", None, "panel hiccup"), (JSON_OK, 0.05, None)])
    with patch(_STREAM, new=mock):
        r = await FusionBackend().run("q")
    assert r.ok and r.answer.startswith("Cut now")
    assert mock.call_count == 2


async def test_fusion_empty_stream_surfaces_error(monkeypatch):
    """An empty stream that never recovers surfaces the real in-stream error, not a generic message."""
    monkeypatch.setenv("API_KEY_OPENROUTER", "test-key")
    monkeypatch.setattr("genesis.deliberation.backends.fusion._BACKOFF_S", 0.0)
    mock = AsyncMock(return_value=("", None, "panel exploded"))
    with patch(_STREAM, new=mock):
        r = await FusionBackend().run("q")
    assert not r.ok and "no content" in r.error and "panel exploded" in r.error
    assert mock.call_count == 3  # empty retried to exhaustion


async def test_fusion_http_429_exhausted(monkeypatch):
    monkeypatch.setenv("API_KEY_OPENROUTER", "test-key")
    monkeypatch.setattr("genesis.deliberation.backends.fusion._BACKOFF_S", 0.0)
    mock = AsyncMock(side_effect=_http_error(429))
    with patch(_STREAM, new=mock):
        r = await FusionBackend().run("q")
    assert not r.ok and "call failed" in r.error and "status=429" in r.error
    assert mock.call_count == 3  # exhausts all attempts


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

        async def run(self, q, *, context, stakes, mode, preset, timeout_s, models):
            seen.update(q=q, context=context, stakes=stakes, mode=mode, preset=preset)
            return DeliberationResult(answer="ok")

    monkeypatch.setattr("genesis.deliberation.core.get_backend", lambda n: Stub())
    await deliberate("decide", context="bg", stakes="high", mode="analysis", preset="budget", backend="stub")
    assert seen == {
        "q": "decide", "context": "bg", "stakes": "high", "mode": "analysis", "preset": "budget"
    }


async def test_deliberate_default_stakes_is_none(monkeypatch):
    """Unspecified stakes reaches the backend as None so it can auto-couple."""
    seen = {}

    class Stub:
        name = "stub"

        async def run(self, q, *, context, stakes, mode, preset, timeout_s, models):
            seen["stakes"] = stakes
            return DeliberationResult(answer="ok")

    monkeypatch.setattr("genesis.deliberation.core.get_backend", lambda n: Stub())
    await deliberate("q", backend="stub")
    assert seen["stakes"] is None


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


async def test_mcp_impl_stakes_passthrough(monkeypatch):
    """The tool's "" default and unknown values pass None (auto-couple); normal/high pass through."""
    seen = {}
    import genesis.deliberation as dl

    async def fake(q, **kw):
        seen["stakes"] = kw.get("stakes")
        return DeliberationResult(answer="A")

    monkeypatch.setattr(dl, "deliberate", fake)
    from genesis.mcp.health import deliberation_tools as dt

    await dt._impl_deliberate("q")  # "" default → None
    assert seen["stakes"] is None
    await dt._impl_deliberate("q", stakes="high")
    assert seen["stakes"] == "high"
    await dt._impl_deliberate("q", stakes="garbage")  # unknown → None
    assert seen["stakes"] is None
