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


def test_parse_boolean_values_return_none():
    # bool subclasses int -> float(True)==1.0 would sneak a false verdict past the parser.
    assert _parse_verdict('{"real": true, "perk": false}') is None


def test_parse_object_then_prose_with_brace():
    # brace-depth scan must stop at the object's close, not extend to a later stray "}".
    assert _parse_verdict('{"real": 0.8, "perk": 0.3} (all set})') == {"real": 0.8, "perk": 0.3}


def test_parse_object_with_nested_extra_field():
    # a nested extra field must not break extraction; real/perk still read, extra ignored.
    assert _parse_verdict('{"real": 0.8, "perk": 0.3, "meta": {"k": 1}}') == {"real": 0.8, "perk": 0.3}


def test_parse_non_dict_json_returns_none():
    assert _parse_verdict('[0.8, 0.3]') is None


def test_parse_no_object_returns_none():
    assert _parse_verdict("real 0.8 perk 0.3") is None


def test_parse_empty_or_none_returns_none():
    assert _parse_verdict("") is None
    assert _parse_verdict(None) is None


# ── verdict v2: category + reason are additive + best-effort (real/perk stay required) ──

def test_parse_v2_full_verdict():
    out = _parse_verdict(
        '{"real": 0.8, "perk": 0.9, "category": "question", "reason": "they asked whether to ship"}'
    )
    assert out == {"real": 0.8, "perk": 0.9, "category": "question",
                   "reason": "they asked whether to ship"}


def test_parse_old_format_invents_no_v2_keys():
    # a bare {real, perk} (model ignored the v2 fields) parses cleanly with NO invented
    # category/reason — backward compatible, and the parser never fabricates a category.
    assert _parse_verdict('{"real": 0.8, "perk": 0.3}') == {"real": 0.8, "perk": 0.3}


def test_parse_unknown_category_coerces_to_other():
    out = _parse_verdict('{"real": 0.5, "perk": 0.5, "category": "banana"}')
    assert out["category"] == "other"


def test_parse_category_normalized_to_lowercase():
    out = _parse_verdict('{"real": 0.5, "perk": 0.5, "category": "Question"}')
    assert out["category"] == "question"


def test_parse_non_string_category_coerces_to_other():
    out = _parse_verdict('{"real": 0.5, "perk": 0.5, "category": 7}')
    assert out["category"] == "other"


def test_parse_reason_stripped_and_hard_capped():
    out = _parse_verdict('{"real": 0.5, "perk": 0.5, "reason": "  ' + "x" * 400 + '  "}')
    assert out["reason"] == "x" * 200          # whitespace-stripped, hard-capped to 200 chars


def test_parse_blank_reason_omitted():
    out = _parse_verdict('{"real": 0.5, "perk": 0.5, "reason": "   "}')
    assert "reason" not in out and out["real"] == 0.5


def test_parse_non_string_reason_omitted():
    out = _parse_verdict('{"real": 0.5, "perk": 0.5, "reason": {"x": 1}}')
    assert "reason" not in out


def test_parse_missing_real_still_none_with_v2_fields_present():
    # real/perk remain HARD-required even when category/reason are supplied.
    assert _parse_verdict('{"perk": 0.5, "category": "task", "reason": "do X"}') is None


def test_parse_reason_containing_brace_is_not_truncated():
    # v2 reason is FREE TEXT; a stray "}" (or "{") inside it must NOT prematurely close the
    # brace-balanced scan (the pre-v2 scanner ignored string context — fine when all fields
    # were numeric, a silent verdict-loss regression now that reason is prose).
    out = _parse_verdict('{"real": 0.8, "perk": 0.3, "category": "task", "reason": "fix the }close} bug"}')
    assert out["real"] == 0.8 and out["category"] == "task"
    assert out["reason"] == "fix the }close} bug"


# ── _build_prompt: window text + speaker labels reach the model ──

def test_prompt_carries_window_text_and_speaker_labels():
    p = _build_prompt([_u("did we ship it?", is_user=1), _u("not yet", is_user=0)])
    assert "did we ship it?" in p and "not yet" in p
    assert "[user]" in p and "[other]" in p
    assert "real" in p and "perk" in p          # asks for BOTH scores


def test_prompt_unknown_speaker_label():
    p = _build_prompt([_u("mumble", is_user=None)])
    assert "[?]" in p


def test_prompt_lists_category_enum_and_reason_field():
    p = _build_prompt([_u("can you book it?", is_user=1)])
    for cat in ("question", "task", "decision", "problem", "chatter", "garble"):
        assert cat in p
    assert "category" in p and "reason" in p


def test_prompt_reason_instructs_no_verbatim_quote():
    # firewall-soften safety: the reason is a CHARACTERIZATION, not a transcript quote —
    # the prompt must tell the model not to quote the window text back.
    p = _build_prompt([_u("hi", is_user=1)]).lower()
    assert "quote" in p or "verbatim" in p


# ── AttentionSampler.sample: injected fake router ──

@dataclass
class _Result:
    success: bool
    content: str | None
    model_id: str | None = None       # RoutingResult carries the serving model (may fall back)


class _FakeRouter:
    def __init__(self, *, content=None, success=True, raises=False, model_id=None):
        self._content = content
        self._success = success
        self._raises = raises
        self._model_id = model_id
        self.calls: list = []

    async def route_call(self, call_site_id, messages, **kwargs):
        self.calls.append((call_site_id, messages))
        if self._raises:
            raise RuntimeError("boom")
        return _Result(self._success, self._content, self._model_id)


@pytest.mark.asyncio
async def test_sample_happy_hits_attention_salience_call_site():
    r = _FakeRouter(content='{"real": 0.7, "perk": 0.6}')
    out = await AttentionSampler(r).sample([_u("what's the plan?", is_user=1)], CFG)
    assert out["real"] == 0.7 and out["perk"] == 0.6
    assert out["prompt_version"] == "v2"        # sample() stamps the prompt version
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


@pytest.mark.asyncio
async def test_sample_stamps_prompt_version_and_preserves_v2_fields():
    r = _FakeRouter(content='{"real": 0.7, "perk": 0.6, "category": "question", "reason": "asked a q"}')
    out = await AttentionSampler(r).sample([_u("what's the plan?", is_user=1)], CFG)
    assert out["prompt_version"] == "v2"
    assert out["category"] == "question" and out["reason"] == "asked a q"
    assert out["real"] == 0.7 and out["perk"] == 0.6


@pytest.mark.asyncio
async def test_sample_stamps_model_when_router_reports_it():
    r = _FakeRouter(content='{"real": 1, "perk": 1}', model_id="mistral-small-latest")
    out = await AttentionSampler(r).sample([_u("hi", is_user=1)], CFG)
    assert out["model"] == "mistral-small-latest"


@pytest.mark.asyncio
async def test_sample_omits_model_when_router_reports_none():
    r = _FakeRouter(content='{"real": 1, "perk": 1}')     # RoutingResult.model_id is None
    out = await AttentionSampler(r).sample([_u("hi", is_user=1)], CFG)
    assert "model" not in out


def test_attention_salience_call_site_is_configured():
    """The sampler's call-site must exist in the real routing config: free-only (so
    never_pays keeps a valid chain) and non-Groq (the L1.5 chain deliberately avoids
    Groq's EOL 8B / JSON-breaking gpt-oss-20b). Picked by the 2026-07-01 bake-off."""
    from genesis.attention.sampler import CALL_SITE
    from genesis.env import repo_root
    from genesis.routing.config import load_config

    cfg = load_config(repo_root() / "config" / "model_routing.yaml", check_api_keys=False)
    assert CALL_SITE in cfg.call_sites
    site = cfg.call_sites[CALL_SITE]
    assert site.never_pays is True
    assert site.chain
    for provider in site.chain:
        assert cfg.providers[provider].is_free            # never_pays needs a free chain
        assert cfg.providers[provider].provider_type != "groq"  # non-Groq by design
