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
    from genesis.campaigns.runner import RESERVED_CAMPAIGN_NAMES
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
        if clean in RESERVED_CAMPAIGN_NAMES:
            # e.g. "pending_reaper​" normalizes to "pending_reaper", which
            # need not collide with any CAMPAIGN and so would pass the check
            # above — but the runner registers its own reaper under that derived
            # scheduler id with replace_existing=True, and it registers AFTER the
            # campaigns. Healing into this name would evict the campaign's job at
            # the next start() with no error and no log: it would simply stop
            # ticking. Leaving the name dirty is strictly better — a dirty name
            # still runs.
            logger.warning(
                "Campaign %s: normalized name %r is reserved by the scheduler "
                "— left as-is (rename it manually to resolve)",
                row.get("id"),
                clean,
            )
            continue
        try:
            await crud.update_campaign(db, row["id"], name=clean)
        except Exception:
            logger.exception("Campaign %s: name heal failed", row.get("id"))
            continue
        # The rename is already COMMITTED at this point (`crud.update_campaign`
        # commits), so a swallowed failure here would be permanent rather than
        # transient: on the next startup `clean == name`, the row is skipped
        # before ever reaching this line, and the old job keys stay orphaned
        # forever — which is precisely the state this module exists to remove.
        #
        # So the rename is undone when its dependent migration fails, restoring
        # the pair to "both un-migrated" and leaving the row eligible for a
        # retry next startup. A dirty name still runs; a permanently orphaned
        # history does not repair itself.
        if not await _migrate_job_health(db, name, clean):
            try:
                # Raw UPDATE, deliberately NOT `crud.update_campaign`. That
                # helper sanitizes at the write boundary, so handing it the
                # original dirty name writes the CLEAN one straight back and the
                # revert becomes a silent no-op — the row stays renamed and
                # still never retries. Restoring a pre-boundary value is the one
                # case that has to go around the boundary, which is the same
                # reason this module's tests insert their fixtures with raw SQL.
                await db.execute(
                    "UPDATE campaigns SET name = ? WHERE id = ?", (name, row["id"])
                )
                await db.commit()
                logger.warning(
                    "Campaign %s: job-state migration failed — rename reverted so "
                    "the next startup retries it",
                    row.get("id"),
                )
            except Exception:
                # Both halves failed. Nothing left to do but say so loudly: the
                # campaign is renamed and its history is not, which is the
                # orphan case, and it will not self-heal.
                logger.exception(
                    "Campaign %s: job-state migration failed AND the rename could "
                    "not be reverted — job history is orphaned under %r and will "
                    "not be retried; migrate or delete it by hand",
                    row.get("id"),
                    f"campaign_{name}",
                )
            continue
        taken.discard(name)
        taken.add(clean)
        healed += 1
        logger.info("Campaign %s: name normalized", row.get("id"))

    return healed


async def _migrate_job_health(db: Any, old_name: str, new_name: str) -> bool:
    """Carry a campaign's durable job state across a rename.

    Returns True if the state moved (or there was none to move), False if it
    could not. The return value is load-bearing: the caller undoes the rename on
    False, because the rename is already committed by then and a silent failure
    would never be retried.

    Two tables are keyed by the derived scheduler name ``campaign_{name}``
    (``campaigns/runner.py``), and BOTH must move or the rename splits the
    campaign's history in half:

    * ``job_health`` — PRIMARY KEY on ``job_name``. Holds last_run/last_success
      and ``job_never_succeeded`` state.
    * ``job_run_events`` — the per-run series. The scheduled-job ledger grader
      looks runs up by EXACT name (``db/crud/job_run_events.py``,
      ``ledger/metrics.py``), so a prediction spanning this startup would be
      graded from a truncated series, or voided as ``no_runs``, if the events
      stayed behind under the dirty name.

    A LEFT-BEHIND ROW IS NOT INERT, which is what makes this worth doing rather
    than tolerating: ``get_stale_jobs`` filters neither against the live
    scheduler registry nor on recency (``db/crud/job_health.py``), so an orphan
    whose ``last_run``/``last_success`` gap exceeds the threshold emits a
    ``job_stale`` warning on every health sweep, forever, and enters the ego
    reconciliation snapshot. Nothing ever purges it.

    Non-fatal — a heal that cannot carry the history must never prevent
    campaigns from starting.
    """
    old_job, new_job = f"campaign_{old_name}", f"campaign_{new_name}"
    try:
        cur = await db.execute("SELECT 1 FROM job_health WHERE job_name = ?", (new_job,))
        if await cur.fetchone():
            # A row already sits under the name we are moving INTO. It cannot be
            # this campaign's — the campaign answered to `old_name` until a
            # moment ago, `campaigns.name` is UNIQUE so no other campaign holds
            # `new_name`, and this heal runs BEFORE runner.start(), so nothing
            # has yet written health under `new_job` in this process. Only a
            # campaign that previously held the name and was deleted leaves a row
            # there: it is a fossil, and the LIVE history is the one under
            # `old_job`.
            #
            # The earlier behaviour kept the fossil and orphaned the real
            # history, which is the inversion worth naming: it preserved the row
            # that can never be written again and abandoned the one that will be.
            # Replace it, and say plainly what was discarded.
            logger.warning(
                "job_health: discarding an unreachable row under %s (no campaign "
                "has held that name since it was written) so the live history "
                "from %s can move into it",
                new_job,
                old_job,
            )
            await db.execute("DELETE FROM job_health WHERE job_name = ?", (new_job,))
        await db.execute(
            "UPDATE job_health SET job_name = ? WHERE job_name = ?",
            (new_job, old_job),
        )
        # Unconstrained TEXT column with no uniqueness, so this cannot collide —
        # any events already under `new_job` belong to the same fossil campaign
        # and are re-pointed alongside the live series rather than deleted. That
        # direction is deliberate: an over-complete run series grades; a
        # truncated one voids.
        await db.execute(
            "UPDATE job_run_events SET job_name = ? WHERE job_name = ?",
            (new_job, old_job),
        )
        await db.commit()
        return True
    except Exception:
        logger.exception("job state migration failed for campaign rename")
        return False
