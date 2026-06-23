"""evo_run MCP tool — run one Evo generation and recommend the winner.

Generates N diverse variants of the current deep-reflection prompt, tests each
vs the current prompt over the golden set (Bonferroni alpha/N gate + held-out
re-validation), and RETURNS the best confirmed variant as a recommendation.

RECOMMEND-ONLY: it measures and recommends; it does NOT promote/apply anything
(no overlay write, no proposal). ``autonomous_action`` is always False. The
promotion path (a human-gated cognitive_variant_promotion proposal) is separate
future work.
"""

from __future__ import annotations

import logging
from pathlib import Path

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

_DEFAULT_GEN_PROVIDER = "groq-free"
_DEFAULT_JUDGE_PROVIDER = "groq-free"


def _make_router(provider: str, config, delegate):
    """A ``cc-haiku``/``cc-sonnet`` provider → CC-CLI (subscription); otherwise a
    litellm provider from model_routing.yaml."""
    if provider.startswith("cc-"):
        from genesis.experimentation.cc_router import CCCliRouter

        return CCCliRouter(provider)
    from genesis.experimentation.standalone_router import StandaloneLiteLLMRouter

    return StandaloneLiteLLMRouter(provider, config=config, delegate=delegate)


def _build_routers(gen_provider: str, judge_provider: str):
    """Build (gen_router, judge_router, judge). Isolated so tests can stub it.

    Supports CC-CLI providers (`cc-haiku`/`cc-sonnet`) for when the litellm
    Claude providers (OpenRouter) are unavailable.
    """
    from genesis.eval.scorers import LLMJudgeScorer

    config = delegate = None
    # litellm config/delegate only needed if a non-CC provider is used
    if not (gen_provider.startswith("cc-") and judge_provider.startswith("cc-")):
        from genesis.experimentation.standalone_router import _default_config_path
        from genesis.routing.config import load_config
        from genesis.routing.litellm_delegate import LiteLLMDelegate

        config = load_config(_default_config_path())
        delegate = LiteLLMDelegate(config)

    gen_router = _make_router(gen_provider, config, delegate)
    judge_router = _make_router(judge_provider, config, delegate)
    return gen_router, judge_router, LLMJudgeScorer(router=judge_router)


def _resolve_base_prompt() -> str:
    """The current EFFECTIVE deep-reflection prompt (overlay-aware)."""
    from genesis.awareness.types import Depth
    from genesis.cc.reflection_bridge._bridge import _DEFAULT_PROMPT_DIR
    from genesis.cc.reflection_bridge._prompts import system_prompt_for_depth

    return system_prompt_for_depth(Depth.DEEP, _DEFAULT_PROMPT_DIR)


async def _impl_evo_run(
    *,
    base_prompt: str | None = None,
    n_variants: int = 6,
    eval_limit: int = 30,
    holdout_limit: int = 20,
    gen_provider: str = _DEFAULT_GEN_PROVIDER,
    judge_provider: str = _DEFAULT_JUDGE_PROVIDER,
    golden_set_path: str | None = None,
) -> dict:
    if golden_set_path:
        gs = Path(golden_set_path)
    else:
        from genesis.eval.reflection_golden_set import DEFAULT_OUTPUT

        gs = DEFAULT_OUTPUT
    if not gs.exists():
        return {
            "status": "error",
            "message": (
                f"golden set not found: {gs}. Generate it with: "
                "python -m genesis.eval.reflection_golden_set --count 150"
            ),
        }

    if not base_prompt:
        try:
            base_prompt = _resolve_base_prompt()
        except Exception as exc:  # noqa: BLE001
            logger.warning("evo_run: could not resolve base prompt", exc_info=True)
            return {"status": "error", "message": f"base prompt unavailable: {exc}"}

    n_variants = max(1, min(n_variants, 12))  # hard fan-out cap (cost bound)

    from genesis.experimentation import evo as _evo
    from genesis.experimentation.types import CognitiveVariant

    # Deterministic directive-append — no generation LLM call, no truncation.
    candidates = _evo.build_directive_variants(base_prompt, n_variants)
    if not candidates:
        return {"status": "error", "message": "no variants to evaluate (n_variants must be >= 1)"}

    import genesis.mcp.health_mcp as health_mcp_mod

    _service = getattr(health_mcp_mod, "_service", None)
    db = getattr(_service, "_db", None) if _service is not None else None

    gen_router, judge_router, judge = _build_routers(gen_provider, judge_provider)
    try:
        base = CognitiveVariant(
            name="current", description="current effective deep-reflection prompt",
            system_prompt=base_prompt,
        )
        result = await _evo.run_evo(
            base=base,
            candidates=candidates,
            golden_set_path=gs,
            config=_evo.EvoConfig(
                eval_limit=eval_limit, holdout_limit=holdout_limit,
                gen_provider=gen_provider, judge_provider=judge_provider,
            ),
            gen_router=gen_router,
            judge=judge,
        )
    except Exception as exc:  # noqa: BLE001 — never crash the tool
        logger.warning("evo_run failed", exc_info=True)
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"}
    finally:
        import contextlib

        for r in (gen_router, judge_router):
            with contextlib.suppress(Exception):
                await r.close()

    # Best-effort one-row summary for history (recommend-only — no cognition write).
    persisted_run_id = None
    if db is not None:
        import uuid as _uuid

        try:
            persisted_run_id = await _evo.persist_evo_summary(
                db, result, gen_provider=gen_provider, judge_provider=judge_provider,
                label=_uuid.uuid4().hex[:8],
            )
        except Exception:  # noqa: BLE001 — persistence is non-fatal
            logger.warning("evo_run: summary persistence failed", exc_info=True)

    w = result.winner
    return {
        "status": "ok",
        "autonomous_action": False,
        "winner": (
            {
                "name": w.name,
                "approach": w.description,
                "prompt_preview": w.system_prompt[:300],
                "full_prompt": w.system_prompt,
            }
            if w else None
        ),
        "winner_winrate": result.winner_winrate,
        "holdout_winrate": result.holdout_winrate,
        "candidates_evaluated": result.candidates_evaluated,
        "survivors": result.survivors,
        "holdout_disjoint": result.holdout_disjoint,
        "per_candidate": result.per_candidate,
        "note": result.note,
        "persisted_run_id": persisted_run_id,
        "recommend_only": (
            "Evo measured the variants and recommends the winner (if any). It does "
            "NOT promote — apply manually by writing the prompt to "
            "~/.genesis/config/reflection/REFLECTION_DEEP.md, or via the future "
            "human-gated cognitive_variant_promotion proposal."
        ),
    }


@mcp.tool()
async def evo_run(
    base_prompt: str = "",
    n_variants: int = 6,
    eval_limit: int = 30,
    holdout_limit: int = 20,
    gen_provider: str = _DEFAULT_GEN_PROVIDER,
    judge_provider: str = _DEFAULT_JUDGE_PROVIDER,
    golden_set_path: str = "",
) -> dict:
    """Evolve the deep-reflection prompt: generate N variants, A/B each vs the
    current prompt, and recommend the best confirmed winner.

    RECOMMEND-ONLY — measures + recommends, never promotes (autonomous_action
    False). Bounded cost: <= n_variants*eval_limit + holdout_limit generations
    (x2 for judging), on free providers, on demand.

    Args:
        base_prompt: prompt to evolve (default: the current effective deep
            reflection prompt, overlay-aware).
        n_variants: fan-out (capped 1..12).
        eval_limit / holdout_limit: golden cases for fan-out / held-out
            re-validation (disjoint slices).
        gen_provider / judge_provider: routing-config providers (default free).
    """
    return await _impl_evo_run(
        base_prompt=base_prompt or None,
        n_variants=n_variants,
        eval_limit=eval_limit,
        holdout_limit=holdout_limit,
        gen_provider=gen_provider,
        judge_provider=judge_provider,
        golden_set_path=golden_set_path or None,
    )
