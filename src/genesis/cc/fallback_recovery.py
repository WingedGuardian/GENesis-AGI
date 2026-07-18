"""Account-wide CC fallback recovery — shared across every home-success path.

When Claude is rate-limited account-wide, conversation turns fail over to a
roster peer and ``fallback_state`` records the degraded condition. Recovery
(Claude is back) CANNOT be inferred from the resilience state machine or the CC
budget tracker — both track Genesis's *self-imposed* throttle (session-spawn
rate) and latched invoker errors, NOT Anthropic's real rate-limit state. The
only proof the home model is back is a home-model call that actually succeeds.

So every home-model success funnels through :func:`note_home_recovery`:
- foreground conversation turns (``conversation._maybe_clear_fallback``),
- background DirectSession runs (``direct_session`` success path),
- the slow safety probe for a totally-idle system
  (``resilience.cc_fallback_probe``).

All functions here are fire-and-forget and never raise — they sit on success
paths that must not be broken by recovery bookkeeping.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def fire_fallback_alert(*, topic: str, context: str) -> None:
    """Fire-and-forget CC-fallback ALERT via the outreach pipeline.

    Reaches the pipeline through the runtime singleton (call sites here hold no
    outreach ref). No-ops cleanly when the runtime/pipeline is absent (tests,
    early startup). Never raises.
    """
    try:
        from genesis.runtime import GenesisRuntime

        pipeline = getattr(GenesisRuntime.instance(), "_outreach_pipeline", None)
        if pipeline is None:
            return
        from genesis.outreach.types import OutreachCategory, OutreachRequest

        await pipeline.submit(OutreachRequest(
            category=OutreachCategory.ALERT,
            topic=topic,
            context=context,
            salience_score=0.9,
            verbatim=True,  # pre-composed recovery alert — never reword
        ))
    except Exception:
        logger.debug("fallback ALERT dispatch failed", exc_info=True)


async def note_home_recovery() -> bool:
    """Clear account-wide fallback after a confirmed home-model success.

    Idempotent: ``fallback_state.clear()`` only transitions active→inactive once,
    so the recovery ALERT fires exactly once per outage no matter how many home
    successes race in. Returns True only on that transition. Never raises.

    Note: this clears only the ACCOUNT-WIDE flag. The per-conversation sticky peer
    session (``cc_sessions.metadata.fallback_session``) is foreground-specific and
    is cleared by ``conversation._maybe_clear_fallback`` around this call.
    """
    try:
        from genesis.cc import fallback_state

        if fallback_state.clear():
            await fire_fallback_alert(
                topic="cc_fallback_recovery",
                context=(
                    "<b>CC recovered</b>\n\nThe home model is back — replies are "
                    "running on it again."
                ),
            )
            return True
    except Exception:
        logger.warning("fallback recovery handling failed", exc_info=True)
    return False
