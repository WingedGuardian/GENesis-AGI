"""Invariants on the SHIPPED routing config (config/model_routing.yaml).

Guards the 2026-08 NIM repoint (after NVIDIA NIM EOL'd deepseek-v4-pro and made
kimi-k2.6 404-for-account): no re-introduction of the dead NIM model slugs, every
call-site keeps a non-NIM fallback (NIM's free tier churns silently), and judge
stays deepseek-family so the eval baseline doesn't shift under a model swap.
"""

from __future__ import annotations

from genesis.env import repo_root
from genesis.routing.config import load_config

_NIM_PROVIDERS = {"nvidia-nim-glm", "nvidia-nim-deepseek", "nvidia-nim-minimax"}
# NIM model slugs verified dead 2026-08 (410 EOL / 404-for-account) — never repoint here.
_DEAD_NIM_SLUGS = {
    "deepseek-ai/deepseek-v4-pro",
    "moonshotai/kimi-k2.6",
    "moonshotai/kimi-k2.5",
    "deepseek-ai/deepseek-coder-6.7b-instruct",
}


def _cfg():
    return load_config(str(repo_root() / "config" / "model_routing.yaml"))


def test_no_dead_nim_model_slugs():
    """No nvidia_nim provider may point at a slug NIM has EOL'd / 404s for us."""
    cfg = _cfg()
    for name, p in cfg.providers.items():
        if p.provider_type == "nvidia_nim":
            assert p.model_id not in _DEAD_NIM_SLUGS, (
                f"provider {name} points at dead NIM slug {p.model_id}"
            )


def test_every_chain_keeps_a_non_nim_fallback():
    """NIM's free tier churns silently; no call-site may depend on NIM alone."""
    offenders = [
        name
        for name, cs in _cfg().call_sites.items()
        if cs.chain and all(p in _NIM_PROVIDERS for p in cs.chain)
    ]
    assert not offenders, f"NIM-only chains (no non-NIM fallback): {offenders}"


def test_adversarial_challenge_sites_are_model_independent():
    """A `_challenge` site adversarially re-checks its base site, so it MUST lead
    with a different MODEL — otherwise the two-model agreement gate degenerates to
    one model agreeing with itself (a data-corruption risk for entity_adjudication,
    which gates a destructive merge_entity)."""
    cfg = _cfg()
    pairs = [
        ("dream_cycle_synthesis", "dream_cycle_synthesis_challenge"),
        ("dream_cycle_entity_check", "dream_cycle_entity_challenge"),
        ("entity_adjudication", "entity_adjudication_challenge"),
    ]
    for base, chal in pairs:
        bc, cc = cfg.call_sites[base].chain, cfg.call_sites[chal].chain
        assert bc and cc, f"{base}/{chal} must have non-empty chains"
        base_model = cfg.providers[bc[0]].model_id
        chal_model = cfg.providers[cc[0]].model_id
        assert base_model != chal_model, (
            f"{chal} leads with the same model as {base} ({base_model}) — "
            "adversarial model independence collapsed"
        )


def test_judge_stays_deepseek_family():
    """judge scores evals — keep it deepseek-family so a model swap can't shift
    the longitudinal eval baseline (glm/other must not appear here)."""
    cfg = _cfg()
    for p in cfg.call_sites["judge"].chain:
        model = cfg.providers[p].model_id.lower()
        assert "deepseek" in model, (
            f"judge uses non-deepseek provider {p} ({model}) — breaks the eval baseline"
        )
