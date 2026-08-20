"""Invariants on the SHIPPED routing config (config/model_routing.yaml).

Guards the 2026-08 NIM repoint (after NVIDIA NIM EOL'd deepseek-v4-pro (HTTP 410)
and made kimi-k2.6 404-for-account): no re-introduction of those dead NIM model
slugs, every call-site keeps a non-NIM fallback (NIM's free tier churns silently),
the adversarial `_challenge` sites stay model-independent from their base, and the
judge stays deepseek-family so the eval baseline doesn't shift under a model swap.
"""

from __future__ import annotations

from pathlib import Path

from genesis.routing.config import load_config

# NIM model slugs this repoint removed because they are dead for our account —
# deepseek-v4-pro (HTTP 410 EOL) and kimi-k2.6 (404-for-account), both verified by
# live probe 2026-08-19. A regression that repoints a NIM provider back to either
# silently reopens the free->paid fallback leak this PR closed.
_DEAD_NIM_SLUGS = {
    "deepseek-ai/deepseek-v4-pro",
    "moonshotai/kimi-k2.6",
}


def _cfg():
    # Resolve via __file__ (not genesis.env.repo_root(), which returns the install
    # location) so a worktree run validates its OWN config, not the main tree's —
    # matching the sibling test_config.py convention.
    path = Path(__file__).resolve().parents[2] / "config" / "model_routing.yaml"
    return load_config(str(path))


def _nim_providers(cfg) -> set[str]:
    """Provider names whose type is nvidia_nim (derived, so the guard tracks the
    live provider set instead of a stale hardcoded list)."""
    return {name for name, p in cfg.providers.items() if p.provider_type == "nvidia_nim"}


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
    cfg = _cfg()
    nim = _nim_providers(cfg)
    offenders = [
        name for name, cs in cfg.call_sites.items() if cs.chain and all(p in nim for p in cs.chain)
    ]
    assert not offenders, f"NIM-only chains (no non-NIM fallback): {offenders}"


def test_adversarial_challenge_sites_are_model_independent():
    """A `_challenge` site adversarially re-checks its base site, so their chains
    MUST resolve to DISJOINT model sets across the WHOLE chain — not just the
    primary. Neither dream_cycle.py nor adversarial_review.py compares
    provider_used, so if a base and its challenge can BOTH fall through to the
    same model (e.g. both to groq-free under a DeepSeek outage), one model
    silently approves its own output — collapsing the two-model agreement gate
    that guards entity_adjudication's destructive merge_entity."""
    cfg = _cfg()
    pairs = [
        ("dream_cycle_synthesis", "dream_cycle_synthesis_challenge"),
        ("dream_cycle_entity_check", "dream_cycle_entity_challenge"),
        ("entity_adjudication", "entity_adjudication_challenge"),
    ]
    for base, chal in pairs:
        bc, cc = cfg.call_sites[base].chain, cfg.call_sites[chal].chain
        assert bc and cc, f"{base}/{chal} must have non-empty chains"
        base_models = {cfg.providers[p].model_id for p in bc}
        chal_models = {cfg.providers[p].model_id for p in cc}
        shared = base_models & chal_models
        assert not shared, (
            f"{base} and {chal} share model(s) {shared} across their fallback "
            "chains — an outage could route both to the same model, so the "
            "challenge could approve its own base output"
        )


def test_default_judge_chain_has_no_duplicates_and_mirrors_config():
    """The offline judge chain (bench / skill-replay) must not duplicate a
    provider — a duplicate makes StandaloneLiteLLMRouter re-attempt the same
    failed provider (a second timeout) — and must mirror the runtime judge."""
    from genesis.experimentation.standalone_router import default_judge_chain

    chain = default_judge_chain()
    assert len(chain) == len(set(chain)), f"duplicate provider in judge chain: {chain}"
    assert chain == list(_cfg().call_sites["judge"].chain), (
        "offline judge chain drifted from the runtime judge call site"
    )
    # the known-down-primary lever reorders without duplicating
    led = default_judge_chain("openrouter-deepseek-v4-flash")
    assert led[0] == "openrouter-deepseek-v4-flash"
    assert len(led) == len(set(led)), f"duplicate after reorder: {led}"


def test_judge_stays_deepseek_family():
    """judge scores evals — keep it deepseek-family so a model swap can't shift
    the longitudinal eval baseline (glm/other must not appear here)."""
    cfg = _cfg()
    for p in cfg.call_sites["judge"].chain:
        model = cfg.providers[p].model_id.lower()
        assert "deepseek" in model, (
            f"judge uses non-deepseek provider {p} ({model}) — breaks the eval baseline"
        )
