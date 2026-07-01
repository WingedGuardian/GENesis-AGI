"""L1.5 sampler: fail-closed verdict parsing + injected-router integration.

The sampler is the ONLY attention module that calls an LLM. It must never raise (a
failing sample just leaves ``l15_verdict`` absent) and must send NO junk downstream —
so the parser is fail-closed and every router failure mode collapses to ``None``.
"""
from dataclasses import dataclass

import pytest

from genesis.attention.config import AttentionConfig, default_config_dict
from genesis.attention.sampler import AttentionSampler, _build_prompt, _parse_verdict
from genesis.attention.types import AmbientUtterance

CFG = AttentionConfig.from_dict(default_config_dict())


def _u(text, is_user=None, **kw) -> AmbientUtterance:
    d = dict(id=1, ts=0.0, duration_s=1.0, is_user=is_user, speaker_total=None,
             n_tokens=5, frac_lt_1=0.0, rms=0.1)
    d.update(kw)
    return AmbientUtterance(text=text, **d)


# ── _parse_verdict: fail-closed float extraction ──

def test_parse_happy():
    assert _parse_verdict('{"real": 0.8, "perk": 0.3}') == {"real": 0.8, "perk": 0.3}


def test_parse_fenced_json_block():
    assert _parse_verdict('```json\n{"real": 0.9, "perk": 0.1}\n```') == {"real": 0.9, "perk": 0.1}


def test_parse_prose_wrapped_object():
    assert _parse_verdict('Sure! {"real": 0.5, "perk": 0.5} hope that helps') == {"real": 0.5, "perk": 0.5}


def test_parse_clamps_out_of_range_to_unit_interval():
    assert _parse_verdict('{"real": 1.5, "perk": -0.2}') == {"real": 1.0, "perk": 0.0}


def test_parse_integer_values_coerce_to_float():
    assert _parse_verdict('{"real": 1, "perk": 0}') == {"real": 1.0, "perk": 0.0}


def test_parse_malformed_json_returns_none():
    assert _parse_verdict('{real: 0.8, perk: 0.3') is None


def test_parse_missing_key_returns_none():
    assert _parse_verdict('{"real": 0.8}') is None


def test_parse_nan_returns_none():
    # json.loads accepts a bare NaN token; the finite-check must reject it (fail-closed).
    assert _parse_verdict('{"real": NaN, "perk": 0.3}') is None


def test_parse_non_numeric_returns_none():
    assert _parse_verdict('{"real": "high", "perk": 0.3}') is None


def test_parse_non_dict_json_returns_none():
    assert _parse_verdict('[0.8, 0.3]') is None


def test_parse_no_object_returns_none():
    assert _parse_verdict("real 0.8 perk 0.3") is None


def test_parse_empty_or_none_returns_none():
    assert _parse_verdict("") is None
    assert _parse_verdict(None) is None


# ── _build_prompt: window text + speaker labels reach the model ──

def test_prompt_carries_window_text_and_speaker_labels():
    p = _build_prompt([_u("did we ship it?", is_user=1), _u("not yet", is_user=0)])
    assert "did we ship it?" in p and "not yet" in p
    assert "[user]" in p and "[other]" in p
    assert "real" in p and "perk" in p          # asks for BOTH scores


def test_prompt_unknown_speaker_label():
    p = _build_prompt([_u("mumble", is_user=None)])
    assert "[?]" in p


# ── AttentionSampler.sample: injected fake router ──

@dataclass
class _Result:
    success: bool
    content: str | None


class _FakeRouter:
    def __init__(self, *, content=None, success=True, raises=False):
        self._content = content
        self._success = success
        self._raises = raises
        self.calls: list = []

    async def route_call(self, call_site_id, messages, **kwargs):
        self.calls.append((call_site_id, messages))
        if self._raises:
            raise RuntimeError("boom")
        return _Result(self._success, self._content)


@pytest.mark.asyncio
async def test_sample_happy_hits_attention_salience_call_site():
    r = _FakeRouter(content='{"real": 0.7, "perk": 0.6}')
    out = await AttentionSampler(r).sample([_u("what's the plan?", is_user=1)], CFG)
    assert out == {"real": 0.7, "perk": 0.6}
    assert r.calls and r.calls[0][0] == "attention_salience"


@pytest.mark.asyncio
async def test_sample_route_failure_returns_none():
    assert await AttentionSampler(_FakeRouter(success=False, content=None)).sample(
        [_u("hi", is_user=1)], CFG) is None


@pytest.mark.asyncio
async def test_sample_router_exception_never_propagates():
    assert await AttentionSampler(_FakeRouter(raises=True)).sample([_u("hi", is_user=1)], CFG) is None


@pytest.mark.asyncio
async def test_sample_empty_window_skips_router_call():
    r = _FakeRouter(content='{"real": 1, "perk": 1}')
    assert await AttentionSampler(r).sample([], CFG) is None
    assert r.calls == []            # no wasted egress on an empty window


@pytest.mark.asyncio
async def test_sample_malformed_content_returns_none():
    assert await AttentionSampler(_FakeRouter(content="not json at all")).sample(
        [_u("hi", is_user=1)], CFG) is None
