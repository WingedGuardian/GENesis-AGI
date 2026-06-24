"""MCP tool for `deliberate()` — "the chorus". On the genesis-health server.

On-demand surface: lets a CC/chat session consult a panel of models for a high-stakes
or contested decision and get back a synthesized verdict plus the explicit dissent.
"""

from __future__ import annotations

import logging

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)


async def _impl_deliberate(
    question: str, context: str = "", stakes: str = "normal", mode: str = "synthesis", preset: str = ""
) -> dict:
    from genesis.deliberation import deliberate

    if not question or not question.strip():
        return {"error": "question is required"}
    if stakes not in ("normal", "high"):
        stakes = "normal"
    if mode not in ("synthesis", "analysis"):
        mode = "synthesis"
    preset_arg = preset if preset in ("strong", "budget") else None
    result = await deliberate(
        question, context=(context or None), stakes=stakes, mode=mode, preset=preset_arg, backend="fusion"
    )
    return {
        "answer": result.answer,
        "consensus": result.consensus,
        "dissent": list(result.dissent),
        "blind_spots": list(result.blind_spots),
        "confidence": result.confidence,
        "per_model": [
            {"model": pm.model, "answer": pm.answer, "stance": pm.stance} for pm in result.per_model
        ],
        "backend_used": result.backend_used,
        "preset_used": result.preset_used,
        "latency_s": round(result.latency_s, 1) if result.latency_s is not None else None,
        "cost_usd": result.cost_usd,
        "cost_known": result.cost_known,
        "error": result.error,
    }


@mcp.tool()
async def deliberate(
    question: str, context: str = "", stakes: str = "normal", mode: str = "synthesis", preset: str = ""
) -> dict:
    """Consult a chorus of models (a server-side panel + judge) for a verdict PLUS the dissent —
    for genuinely high-stakes or contested decisions.

    PAID + opt-in: routes to OpenRouter Fusion (non-free). Use ONLY for high-stakes/explicit calls —
    never as a default judgment path. Recursion-blocked.

    Args:
      question: the decision or question to deliberate.
      context:  optional background to ground the panel.
      stakes:   "normal" (default) or "high" (weights dissent more heavily).
      mode:     "synthesis" (default) = fast prose verdict; "analysis" = deeper (~2-3min)
                machine-structured consensus + dissent[] + blind_spots[].
      preset:   "" = mode default (synthesis→budget, analysis→strong); "strong" = general-high panel
                (frontier models); "budget" = general-budget panel (mid-tier, cheaper/faster).

    Returns {answer, consensus, dissent[], blind_spots[], confidence, per_model[], backend_used,
    preset_used, cost_usd, cost_known, latency_s, error}. On failure, answer is null and error is set
    (never raises).
    """
    return await _impl_deliberate(question, context, stakes, mode, preset)
