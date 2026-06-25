"""evo_run MCP tool — run one Evo generation and recommend the winner.

Generates N diverse variants of the CANONICAL deep-reflection prompt, tests each
vs that prompt over the golden set (Bonferroni alpha/N gate + held-out
re-validation), and RETURNS the best confirmed variant as a recommendation.

RECOMMEND-ONLY: it measures and recommends; it NEVER mutates cognition itself.
When ``propose`` is set (default), a confirmed winner is FILED as a human-gated
``cognitive_variant_promotion`` proposal (dashboard-delivered) — nothing is
applied until the user approves it, and approval writes the overlay reversibly.
``autonomous_action`` is always False.

Variants are built by appending a directive to the CANONICAL repo prompt (NOT
the overlay), and the winner that gets filed is exactly that ``canonical +
directive`` — so what is measured equals what is applied and repeated
promotions never stack directives.
"""

from __future__ import annotations

import logging
from pathlib import Path

from genesis.ego.cognitive_variant import _MIN_PROMOTE_CONFIDENCE as _PROMOTE_FLOOR
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


def _resolve_canonical_base_prompt() -> str:
    """The CANONICAL repo deep-reflection prompt, IGNORING the overlay.

    Evo measures variants against this and the promoted overlay is written as
    ``canonical + winning_directive`` — so measured == applied and repeated
    promotions never stack directives. Using the overlay-aware resolver here
    (``system_prompt_for_depth``) would re-read a prior promotion and compound.
    """
    from genesis.awareness.types import Depth
    from genesis.cc.reflection_bridge._bridge import _DEFAULT_PROMPT_DIR
    from genesis.cc.reflection_bridge._prompts import (
        _FALLBACK_PROMPTS,
        _resolve_prompt_in_dir,
    )

    resolved = _resolve_prompt_in_dir(Depth.DEEP, _DEFAULT_PROMPT_DIR)
    if resolved is not None:
        return resolved
    return _FALLBACK_PROMPTS[Depth.DEEP]


async def promote_evo_winner(
    db: object,
    result,
    *,
    gen_provider: str,
    judge_provider: str,
) -> str | None:
    """File a recommend-only ``cognitive_variant_promotion`` proposal for a
    confirmed Evo winner. Returns the proposal id, or ``None`` if there is no
    winner or its confidence is below the floor.

    Stores the proposal in ``ego_proposals`` (dashboard-delivered + approvable);
    nothing is applied until the user approves it. The 13KB winner prompt rides
    in ``expected_outputs`` (NOT rendered in the digest); ``content`` carries a
    human summary.
    """
    winner = result.winner
    if winner is None:
        return None

    hwr = result.holdout_winrate or {}
    holdout_p = hwr.get("p_value")
    if holdout_p is None:
        # No held-out p-value → can't establish confidence → don't file.
        logger.info("evo promote: no held-out p_value — not filing")
        return None
    confidence = round(min(0.99, max(0.0, 1.0 - float(holdout_p))), 3)
    if confidence < _PROMOTE_FLOOR:
        logger.info(
            "evo promote: winner confidence %.2f below floor %.2f — not filing",
            confidence, _PROMOTE_FLOOR,
        )
        return None

    t_mean = hwr.get("treatment_mean_score")
    c_mean = hwr.get("control_mean_score")
    effect = (
        t_mean - c_mean
        if isinstance(t_mean, int | float) and isinstance(c_mean, int | float)
        else None
    )

    content = f"Promote reflection-prompt variant — {winner.description}"
    rationale = (
        f"Evo measured this directive against the canonical deep-reflection prompt "
        f"and it survived held-out re-validation (disjoint={result.holdout_disjoint}, "
        f"p={float(holdout_p):.4f}"
        + (
            f", held-out mean {t_mean:.3f} vs control {c_mean:.3f}, +{effect:.3f}"
            if effect is not None else ""
        )
        + f"). It beat control as 1 of {result.survivors}/"
        f"{result.candidates_evaluated} survivors at the Bonferroni alpha/N gate. "
        f"Judged via {judge_provider}; generated via {gen_provider}."
    )
    if gen_provider == judge_provider:
        # Self-scoring caveat (architect): same provider generates AND judges, so
        # scores may be optimistic. Surface it so the user can discount.
        rationale += (
            f" CAVEAT: generator and judge share a provider ({gen_provider}) — "
            "scores may be optimistic; a cross-provider re-run would strengthen this."
        )
    execution_plan = (
        "On approval: writes this prompt to the deep-reflection overlay "
        "(~/.genesis/config/reflection/REFLECTION_DEEP.md) via the cognitive "
        "ledger; reversible with cognitive_modification_rollback."
    )
    proposal = {
        "action_type": "cognitive_variant_promotion",
        "content": content,
        "rationale": rationale,
        "execution_plan": execution_plan,
        "confidence": confidence,
        "urgency": "normal",
        "expected_outputs": {
            "full_prompt": winner.system_prompt,
            "approach": winner.description,
            "evidence": rationale,
        },
    }

    from genesis.ego.proposals import ProposalWorkflow

    wf = ProposalWorkflow(db=db)
    _batch_id, ids, _created = await wf.create_batch([proposal], ego_source="evo")
    if not ids:
        # create_batch dedup skipped it (an identical promotion is already pending).
        logger.info("evo promote: proposal de-duplicated (identical one pending)")
        return None
    logger.info("evo promote: filed cognitive_variant_promotion proposal %s", ids[0])
    return ids[0]


async def _impl_evo_run(
    *,
    base_prompt: str | None = None,
    n_variants: int = 6,
    eval_limit: int = 30,
    holdout_limit: int = 20,
    gen_provider: str = _DEFAULT_GEN_PROVIDER,
    judge_provider: str = _DEFAULT_JUDGE_PROVIDER,
    golden_set_path: str | None = None,
    propose: bool = True,
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
            base_prompt = _resolve_canonical_base_prompt()
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

    # Auto-file the winner as a human-gated proposal (recommend-only): stores it
    # in ego_proposals (dashboard-delivered + approvable). Nothing is applied
    # until the user approves. Gated by `propose`, a confirmed winner, and the
    # confidence floor (inside promote_evo_winner). Never crashes the tool.
    proposal_id = None
    if propose and db is not None and result.winner is not None:
        try:
            proposal_id = await promote_evo_winner(
                db, result, gen_provider=gen_provider, judge_provider=judge_provider,
            )
        except Exception:  # noqa: BLE001 — filing is non-fatal to the measurement
            logger.warning("evo_run: winner proposal filing failed", exc_info=True)

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
        "proposal_id": proposal_id,
        "recommend_only": (
            (
                f"Filed cognitive_variant_promotion proposal {proposal_id} — "
                "review and approve it on the dashboard Ego tab to apply (reversible). "
                "Nothing is applied until you approve."
            )
            if proposal_id else
            (
                "Evo measured the variants and recommends the winner (if any). No "
                "proposal was filed (no confirmed winner, propose=False, below the "
                "confidence floor, or a duplicate is already pending)."
            )
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
    propose: bool = True,
) -> dict:
    """Evolve the deep-reflection prompt: generate N variants, A/B each vs the
    canonical prompt, recommend the best confirmed winner, and (by default) FILE
    it as a human-gated proposal.

    RECOMMEND-ONLY — measures, recommends, and on a confirmed winner files a
    ``cognitive_variant_promotion`` proposal you approve on the dashboard to
    apply (reversibly). It never mutates cognition itself (autonomous_action
    False). Bounded cost: <= n_variants*eval_limit + holdout_limit generations
    (x2 for judging), on free providers, on demand.

    Args:
        base_prompt: prompt to evolve (default: the CANONICAL repo deep-reflection
            prompt — NOT the overlay, so promotions never stack).
        n_variants: fan-out (capped 1..12).
        eval_limit / holdout_limit: golden cases for fan-out / held-out
            re-validation (disjoint slices).
        gen_provider / judge_provider: routing-config providers (default free).
        propose: when True (default), a confirmed winner above the confidence
            floor is filed as a proposal. Set False for pure measurement.
    """
    return await _impl_evo_run(
        base_prompt=base_prompt or None,
        n_variants=n_variants,
        eval_limit=eval_limit,
        holdout_limit=holdout_limit,
        gen_provider=gen_provider,
        judge_provider=judge_provider,
        golden_set_path=golden_set_path or None,
        propose=propose,
    )
