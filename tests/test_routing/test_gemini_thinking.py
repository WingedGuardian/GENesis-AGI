"""Gemini thinking suppression reaches the wire as thinkingBudget=0 (#3674).

Each Gemini call site suppresses thinking. litellm 1.93.2 turns
``reasoning_effort: disable`` into ``thinkingLevel: minimal`` for any model named
gemini-3*-flash, which gemini-3.8-flash rejects with HTTP 400, and
``thinking: {type: disabled}`` sends only ``includeThoughts: false``, which leaves
thinking on. So these tests read the body litellm actually builds, with its HTTP
post stubbed out: a test of the params alone would pass for every one of those.
"""

from __future__ import annotations

from pathlib import Path

import litellm
import pytest
from litellm.llms.custom_httpx import http_handler

from genesis.contribution import version_gate
from genesis.eval import reflection_golden_set
from genesis.routing.config import load_config_from_string
from genesis.routing.litellm_delegate import LiteLLMDelegate

_SHIPPED = Path(__file__).resolve().parents[2] / "config" / "model_routing.yaml"
_SUPPRESSED = {"thinkingBudget": 0}


class _Sent(Exception):
    """Raised in place of the HTTP post once the body is captured."""


@pytest.fixture
def sent(monkeypatch):
    bodies: list[dict] = []

    async def capture(self, url, data=None, json=None, headers=None, **kwargs):
        bodies.append(json)
        raise _Sent

    monkeypatch.setattr(http_handler.AsyncHTTPHandler, "post", capture)
    return bodies


def _gemini_providers():
    cfg = load_config_from_string(_SHIPPED.read_text(), check_api_keys=False)
    return cfg, sorted(n for n, p in cfg.providers.items() if p.provider_type == "google")


def test_the_shipped_config_has_gemini_providers():
    # Guards the parametrisation below against silently testing nothing.
    assert {"gemini-free", "gemini-free-latest"} <= set(_gemini_providers()[1])


@pytest.mark.parametrize("provider", _gemini_providers()[1])
async def test_a_routed_gemini_call_sends_thinking_budget_zero(provider, sent, monkeypatch):
    monkeypatch.setenv("API_KEY_GOOGLE", "test-key")
    cfg, _ = _gemini_providers()
    result = await LiteLLMDelegate(cfg).call(
        provider,
        cfg.providers[provider].model_id,
        [{"role": "user", "content": "hi"}],
        max_tokens=50,
    )
    assert not result.success and len(sent) == 1
    assert sent[0]["generationConfig"]["thinkingConfig"] == _SUPPRESSED


def _direct_gemini_sites():
    sites = [
        ("version_gate fallback", model, params)
        for model, _service, params in version_gate._FALLBACK_MODELS
        if model.startswith("gemini/")
    ]
    sites += [
        ("golden-set judge", model, params)
        for model, params in reflection_golden_set._JUDGE_MODELS
        if model.startswith("gemini/")
    ]
    return sites


def test_both_direct_callers_have_a_gemini_entry():
    assert {name for name, *_ in _direct_gemini_sites()} == {
        "version_gate fallback",
        "golden-set judge",
    }


@pytest.mark.parametrize(("site", "model", "params"), _direct_gemini_sites())
async def test_a_direct_gemini_call_sends_thinking_budget_zero(site, model, params, sent):
    # Both direct callers pass their params straight to litellm.acompletion,
    # with temperature=0.0 and no drop_params.
    # litellm wraps the stub's exception in its own error type, so the capture,
    # not the exception, shows the request was built.
    with pytest.raises(litellm.exceptions.APIConnectionError):
        await litellm.acompletion(
            model=model,
            messages=[{"role": "user", "content": "hi"}],
            api_key="test-key",
            temperature=0.0,
            num_retries=0,
            **(params or {}),
        )
    assert len(sent) == 1, site
    assert sent[0]["generationConfig"]["thinkingConfig"] == _SUPPRESSED, site
