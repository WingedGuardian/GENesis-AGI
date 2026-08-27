"""One-time heal of campaign names written before the write-boundary strip.

``create_campaign``/``update_campaign`` sanitize the name at the write boundary
(they call :func:`genesis.security.sanitizer.strip_control_chars`), so nothing
NEW can land dirty. Rows written before that shipped were never healed: the
migration that would have done it (``d0012``) was reverted, for a real reason —
data migrations run at ``_core.py`` ~:558, AFTER ``_init_campaigns`` (~:472) has
already registered each campaign's APScheduler job as ``campaign_{name}``, so
renaming a row there desynchronizes the live job id from the DB row.

This runs INSIDE campaign init, BEFORE ``runner.start()``, which is what makes it
safe: the scheduler only ever sees already-clean names, so no job id can be
orphaned. Idempotent, and a no-op on an install with no dirty rows (measured:
zero dirty rows on both installs checked — this closes the mechanism, it is not
a response to live corruption).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("genesis.campaigns")


async def heal_campaign_names(db: Any) -> int:
    """Normalize control characters out of stored campaign names.

    Returns the number of rows rewritten. Fail-open: a heal failure must never
    prevent campaigns from starting, so errors are logged and skipped per-row.

    ``campaigns.name`` is UNIQUE, so two dirty names that normalize to the SAME
    clean name would collide. The colliding row is left untouched and logged
    rather than renamed or deleted — silently merging two campaigns' identities
    would be worse than leaving one un-normalized.
    """
    from genesis.db.crud import campaigns as crud
    from genesis.security.sanitizer import strip_control_chars

    try:
        rows = await crud.list_campaigns(db)
    except Exception:
        logger.exception("Campaign name heal skipped — could not list campaigns")
        return 0

    taken = {r["name"] for r in rows}
    healed = 0
    for row in rows:
        name = row.get("name")
        if not isinstance(name, str):
            continue
        clean = strip_control_chars(name)
        if clean == name:
            continue
        if not clean:
            logger.warning(
                "Campaign %s: name normalizes to empty — left as-is", row.get("id")
            )
            continue
        if clean in taken:
            logger.warning(
                "Campaign %s: normalized name collides with an existing campaign "
                "— left as-is (rename it manually to resolve)",
                row.get("id"),
            )
            continue
        try:
            await crud.update_campaign(db, row["id"], name=clean)
        except Exception:
            logger.exception("Campaign %s: name heal failed", row.get("id"))
            continue
        await _migrate_job_health(db, name, clean)
        taken.discard(name)
        taken.add(clean)
        healed += 1
        logger.info("Campaign %s: name normalized", row.get("id"))

    return healed


async def _migrate_job_health(db: Any, old_name: str, new_name: str) -> None:
    """Carry a campaign's durable job-health row across a rename.

    ``job_health.job_name`` is the PRIMARY KEY and the runner derives it as
    ``campaign_{name}`` (``campaigns/runner.py``), so a rename would otherwise
    orphan the row: the campaign's success/failure history and its
    ``job_never_succeeded`` state would silently reset, and a dead row would
    linger under the old name.

    Best-effort and non-fatal — a heal that cannot carry the history must still
    leave the (correctly renamed) campaign running. If a row already exists under
    the new name, the old one is left alone rather than clobbering live history.
    """
    old_job, new_job = f"campaign_{old_name}", f"campaign_{new_name}"
    try:
        cur = await db.execute(
            "SELECT 1 FROM job_health WHERE job_name = ?", (new_job,)
        )
        if await cur.fetchone():
            logger.warning(
                "job_health row already exists for %s — leaving %s in place",
                new_job,
                old_job,
            )
            return
        await db.execute(
            "UPDATE job_health SET job_name = ? WHERE job_name = ?",
            (new_job, old_job),
        )
        await db.commit()
    except Exception:
        logger.exception("job_health migration failed for campaign rename")
