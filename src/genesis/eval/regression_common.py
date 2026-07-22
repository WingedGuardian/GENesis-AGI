"""Shared helpers for eval-sourced informational proposals (j9 / gauntlet).

Both J-9 subsystem-grade regressions and model-roster gauntlet regressions file
acknowledge-only ``ego_proposals`` rows and later auto-clear them when the
subject recovers. This module owns the single clear implementation so the two
writers cannot drift (their sibling alert+propose halves already did — see the
``gauntlet_regression`` module docstring).

Subject identity is the queryable ``action_category`` column (set at file time),
NOT free-text ``content`` — a reword of the alert copy can never silently break
recovery-clearing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from genesis.db.crud import ego as ego_crud

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


async def withdraw_recovered_proposals(
    db: aiosqlite.Connection,
    *,
    action_type: str,
    recovered_subjects: set[str],
    reason: str,
) -> int:
    """Withdraw pending *action_type* rows whose subject has recovered.

    A row's subject is its ``action_category`` (e.g. the subsystem name for j9,
    the model id for gauntlet). Rows whose subject is in *recovered_subjects* are
    moved to ``withdrawn`` with ``user_response = "auto-cleared: <subject>
    <reason>"``. Iterates the UNBOUNDED, oldest-first ``list_pending_proposals``
    (stale rows awaiting clear are the oldest — a capped newest-first scan would
    drop exactly them). Best-effort: never raises into the caller.

    Legacy rows filed before this column was populated have an empty
    ``action_category`` and are intentionally left for the 14-day auto-table —
    never matched by free-text content here.

    Returns the number of rows withdrawn.
    """
    if not recovered_subjects:
        return 0
    try:
        pending = await ego_crud.list_pending_proposals(db)
    except Exception:
        logger.warning("%s auto-clear: pending read failed", action_type, exc_info=True)
        return 0
    cleared = 0
    for p in pending:
        if p.get("action_type") != action_type:
            continue
        subject = p.get("action_category") or ""
        if subject not in recovered_subjects:
            continue
        try:
            ok = await ego_crud.resolve_proposal(
                db,
                p["id"],
                status="withdrawn",
                user_response=f"auto-cleared: {subject} {reason}",
            )
            if ok:
                cleared += 1
        except Exception:
            logger.warning(
                "%s auto-clear: withdraw failed for %s",
                action_type,
                p.get("id"),
                exc_info=True,
            )
    return cleared
