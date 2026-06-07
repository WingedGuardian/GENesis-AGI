"""Campaign pre-check registry — programmatic gates that run before each tick.

Each pre-check is a lightweight Python function (no LLM calls). Returns
``(pass, reason)`` where ``pass=True`` means proceed and ``reason`` is
only set on failure.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Minimum interval between runs (seconds). Prevents rapid re-dispatch
# even if APScheduler misfires or the user manually triggers.
_MIN_INTERVAL_SECONDS = 300  # 5 minutes


async def check_rate_limit(
    campaign: dict, *, ctx: dict[str, Any]
) -> tuple[bool, str | None]:
    """Fail if last run was too recent."""
    last_run = campaign.get("last_run_at")
    if not last_run:
        return True, None

    try:
        last_dt = datetime.fromisoformat(last_run)
        elapsed = (datetime.now(UTC) - last_dt).total_seconds()
        if elapsed < _MIN_INTERVAL_SECONDS:
            return False, f"rate_limit: last run {elapsed:.0f}s ago (min {_MIN_INTERVAL_SECONDS}s)"
    except (ValueError, TypeError):
        pass  # Unparseable timestamp — let it through

    return True, None


async def check_budget(
    campaign: dict, *, ctx: dict[str, Any]
) -> tuple[bool, str | None]:
    """Fail if daily cost exceeds the campaign's budget cap."""
    max_cost = campaign.get("max_daily_cost_usd", 1.0)
    daily_cost = ctx.get("daily_cost", 0.0)
    if daily_cost >= max_cost:
        return False, f"budget_exceeded: ${daily_cost:.2f} >= ${max_cost:.2f} daily cap"
    return True, None


async def check_slots_available(
    campaign: dict, *, ctx: dict[str, Any]
) -> tuple[bool, str | None]:
    """Fail if DirectSessionRunner has no free slots.

    Campaigns are delay-tolerant — yield to ego/user dispatches rather
    than blocking behind the Semaphore(2).
    """
    runner = ctx.get("session_runner")
    if runner is None:
        return True, None  # No runner reference — can't check, let through

    active = runner.active_count()
    max_concurrent = getattr(runner, "_MAX_CONCURRENT", 2)
    if active >= max_concurrent:
        return False, f"session_slots_full: {active}/{max_concurrent} slots in use"
    return True, None


# ── Registry ────────────────────────────────────────────────────────────
PRECHECK_REGISTRY: dict[str, Any] = {
    "rate_limit": check_rate_limit,
    "budget": check_budget,
    "slots_available": check_slots_available,
}


async def run_prechecks(
    campaign: dict,
    ctx: dict[str, Any],
) -> tuple[bool, str | None]:
    """Run all pre-checks listed in the campaign's pre_checks field.

    Returns ``(True, None)`` if all pass, or ``(False, reason)`` on the
    first failure. Unknown check names are logged and skipped.
    """
    check_names_raw = campaign.get("pre_checks", "[]")
    try:
        check_names = json.loads(check_names_raw) if isinstance(check_names_raw, str) else check_names_raw
    except (json.JSONDecodeError, TypeError):
        check_names = []

    for name in check_names:
        fn = PRECHECK_REGISTRY.get(name)
        if fn is None:
            logger.warning("Unknown pre-check '%s' — skipping", name)
            continue
        ok, reason = await fn(campaign, ctx=ctx)
        if not ok:
            return False, reason

    return True, None
