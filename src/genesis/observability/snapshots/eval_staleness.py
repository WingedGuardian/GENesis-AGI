"""Eval benchmark staleness snapshot.

Reports freshness of benchmark data per provider — days since last run,
providers with no data, and high skip rates. Used by the ego to decide
when benchmarks are warranted.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


async def eval_staleness(db: aiosqlite.Connection | None) -> dict:
    """Return eval benchmark freshness data.

    Returns:
        dict with keys: providers (list of per-provider stats),
        stale_count, missing_count, unreliable_count
    """
    if db is None:
        return {"status": "no_db", "providers": []}

    try:
        # Latest run per provider with age in days
        cursor = await db.execute("""
            SELECT
                model_id,
                model_profile,
                MAX(created_at) AS last_run,
                CAST(julianday('now') - julianday(MAX(created_at)) AS INTEGER) AS days_ago,
                SUM(passed_cases) AS total_passed,
                SUM(failed_cases) AS total_failed,
                SUM(skipped_cases) AS total_skipped
            FROM eval_runs
            GROUP BY model_id
            ORDER BY last_run DESC
        """)
        rows = await cursor.fetchall()
    except Exception:
        logger.debug("eval_runs table not available (expected before first benchmark)")
        return {"status": "no_data", "providers": []}

    if not rows:
        return {"status": "no_data", "providers": []}

    providers = []
    stale_count = 0
    unreliable_count = 0

    for row in rows:
        total_attempted = (row[4] or 0) + (row[5] or 0)
        total_skipped = row[6] or 0
        skip_rate = total_skipped / (total_attempted + total_skipped) if (total_attempted + total_skipped) > 0 else 0
        days_ago = row[3] or 0

        entry = {
            "provider": row[0],
            "profile": row[1],
            "last_run": row[2],
            "days_ago": days_ago,
            "total_passed": row[4] or 0,
            "total_failed": row[5] or 0,
            "total_skipped": total_skipped,
            "skip_rate": round(skip_rate, 2),
            "stale": days_ago > 30,
            "unreliable": skip_rate > 0.5,
        }
        providers.append(entry)

        if days_ago > 30:
            stale_count += 1
        if skip_rate > 0.5:
            unreliable_count += 1

    return {
        "status": "ok",
        "providers": providers,
        "total_benchmarked": len(providers),
        "stale_count": stale_count,
        "unreliable_count": unreliable_count,
    }
