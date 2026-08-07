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


async def check_github_activity_pending(
    campaign: dict, *, ctx: dict[str, Any]
) -> tuple[bool, str | None]:
    """Gate the GitHub-activity digest to ticks with NEW monitor activity.

    Passes (dispatch the LLM digest) only when the account-activity monitor
    (``recon/account_activity.py``) has recorded at least one *unresolved*
    ``github_account_activity`` observation since this campaign's last run.
    A quiet 6h window therefore spawns no session and costs nothing; the
    digest, once dispatched, marks the rows it consumes resolved so the next
    empty tick gates again.

    Fail-OPEN on missing plumbing: if ``db`` is absent from ctx (an older
    runner) or ``last_run_at`` is unset (the campaign's first run), let the
    tick through rather than silently gating on infrastructure. A spurious
    pass is cheap — the digest session no-ops on an empty read — whereas a
    wrong gate would silently swallow a real contributor signal.

    Timestamp note: ``last_run_at`` is ``datetime.now(UTC).isoformat()``
    (``+00:00`` suffix) while the monitor's ``created_at`` is a ``Z``-suffixed
    ``_now_z()``. The count does a lexical ``created_at > since`` compare; both
    share the ``YYYY-MM-DDTHH:MM:SS`` prefix, so lexical order matches
    chronological order across the boundary (the differing sub-second suffix
    only ever diverges *within* the same second, which the unresolved filter
    plus resolve-on-digest makes idempotent — no genuinely-new row is missed).
    """
    from genesis.db.crud import observations

    since = campaign.get("last_run_at")
    db = ctx.get("db")
    if db is None or since is None:
        return True, None  # fail-open: never gate on missing plumbing / first run

    n = await observations.count_recent_unresolved_by_type_and_source(
        db,
        type="github_account_activity",
        source="recon",
        since=since,
    )
    if n > 0:
        return True, None
    return False, "no new github activity since last run"


# ── Registry ────────────────────────────────────────────────────────────
PRECHECK_REGISTRY: dict[str, Any] = {
    "rate_limit": check_rate_limit,
    "budget": check_budget,
    "slots_available": check_slots_available,
    "github_activity_pending": check_github_activity_pending,
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
