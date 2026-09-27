"""Eval benchmark staleness snapshot.

Reports freshness and data quality of benchmark runs, one entry per
``(model_id, model_profile)`` pair. Used by the ego to decide when benchmarks
are warranted, and rendered on the dashboard.

Three derivations here are deliberate, and each answers a specific failure of
the earlier shape (which grouped by ``model_id`` alone):

* **One entry per profile, not per provider.** A provider id such as a routing
  alias can be repointed at a different model. Grouping by provider alone summed
  the old model's history into the new one's, and labelled the result with
  whichever profile happened to be latest — so the counts described neither.

* **Infrastructural vs substantive failure.** The runner already records WHY a
  case did not pass: a transient provider error (rate limit, auth, bad model
  id) is stored ``skipped``; any other provider error (a hard timeout, a 5xx)
  is stored as a failed case whose ``scorer_detail`` starts ``provider error``;
  everything else is a scored answer that disagreed with the expected output.
  ``unreliable`` means the MEASUREMENT cannot be trusted, so it is derived from
  the infrastructural share (skips plus provider-error failures). Scored wrong
  answers are reported as ``recent.failure_rate`` and do NOT make a run
  unreliable: they are a reproducible result about the model (or the golden
  set), not noise in the harness.

* **Staleness only where something is expected to re-run.** ``stale`` is only
  meaningful for a profile a recurring trigger keeps refreshing. A manual
  benchmark (run by hand, once) is reported with ``scheduled: false``, and a
  scheduled profile whose provider has since moved on to a newer profile is
  reported ``superseded: true``. Neither is ever ``stale`` — a finding that can
  never clear teaches readers to ignore the field.

Rates cover the ``RATE_WINDOW_DAYS`` before each profile's OWN last run, so a
profile's rate describes its recent behaviour (and can recover after a harness
fix) while a no-longer-running profile still reports its last active window.
The ``total_*`` counts remain all-time for that profile.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

# A scheduled profile with no run for this long is overdue. Unchanged from the
# original snapshot.
STALE_AFTER_DAYS = 30

# Rates are computed over this many days before the profile's own last run.
# Same horizon as staleness, so "recent" means the same thing in both fields;
# an all-time rate can never recover once a harness problem is fixed.
RATE_WINDOW_DAYS = 30

# Above this infrastructural share the run data is not a measurement of the
# model at all. Unchanged threshold from the original skip-rate rule; what
# changed is that provider-error failures now count toward it.
UNRELIABLE_INFRA_RATE = 0.5

# Triggers that re-run on their own. ``manual`` and ``experiment`` runs are
# one-off by construction (see genesis.eval.types.EvalTrigger).
# Known limit: this is inferred from the trigger LABEL, not from what the
# scheduler actually re-enqueues. A one-off eval dispatched through surplus
# also carries ``surplus`` and would read as scheduled.
RECURRING_TRIGGERS = ("surplus", "schedule")

# Prefix the eval runner writes on a non-transient provider error it records
# as a failed case (genesis.eval.runner, the ``not call_result.success``
# branch). Transient provider errors are stored ``skipped`` instead.
_PROVIDER_ERROR_PREFIX = "provider error"


def _days_since(iso_ts: str | None, fallback: int) -> int:
    """Whole days since ``iso_ts``; ``fallback`` when it cannot be parsed."""
    if not iso_ts:
        return fallback
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        logger.warning("eval_staleness: unparseable run timestamp %r", iso_ts)
        return fallback
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts).days


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 2) if denominator > 0 else 0.0


async def eval_staleness(db: aiosqlite.Connection | None) -> dict:
    """Return eval benchmark freshness and data-quality data.

    Returns:
        dict with keys: providers (one entry per model_id + profile),
        total_benchmarked, stale_count, superseded_count, unreliable_count.
        The counts cover only profiles still scheduled (not manual, not
        superseded); the per-profile flags are reported on every entry.
    """
    if db is None:
        return {"status": "no_db", "providers": []}

    try:
        # Per (model_id, profile): all-time totals, recency, and whether any
        # run came from a recurring trigger. ``IS`` keeps a NULL profile its
        # own group through the window join below.
        cursor = await db.execute("""
            WITH g AS (
                SELECT
                    model_id,
                    model_profile,
                    MAX(created_at) AS last_run,
                    CAST(julianday('now') - julianday(MAX(created_at)) AS INTEGER) AS days_ago,
                    SUM(passed_cases) AS total_passed,
                    SUM(failed_cases) AS total_failed,
                    SUM(skipped_cases) AS total_skipped,
                    MAX(trigger IN (?, ?)) AS scheduled,
                    MAX(CASE WHEN trigger IN (?, ?) THEN created_at END)
                        AS last_scheduled_run
                FROM eval_runs
                GROUP BY model_id, model_profile
            ),
            w AS (
                SELECT
                    r.model_id,
                    r.model_profile,
                    COUNT(*) AS runs,
                    SUM(r.passed_cases) AS passed,
                    SUM(r.failed_cases) AS failed,
                    SUM(r.skipped_cases) AS skipped,
                    SUM((
                        SELECT COUNT(*) FROM eval_results er
                        WHERE er.run_id = r.id
                          AND COALESCE(er.skipped, 0) = 0
                          AND er.passed = 0
                          AND er.scorer_detail LIKE ? || '%'
                    )) AS infra_failed
                FROM eval_runs r
                JOIN g ON g.model_id = r.model_id
                      AND g.model_profile IS r.model_profile
                WHERE julianday(r.created_at)
                      >= julianday(g.last_run) - ?
                GROUP BY r.model_id, r.model_profile
            )
            SELECT
                g.model_id, g.model_profile, g.last_run, g.days_ago,
                g.total_passed, g.total_failed, g.total_skipped,
                g.scheduled, g.last_scheduled_run,
                w.runs, w.passed, w.failed, w.skipped, w.infra_failed
            FROM g
            JOIN w ON w.model_id = g.model_id AND w.model_profile IS g.model_profile
            ORDER BY g.last_run DESC
        """, (
            *RECURRING_TRIGGERS,
            *RECURRING_TRIGGERS,
            _PROVIDER_ERROR_PREFIX,
            RATE_WINDOW_DAYS,
        ))
        rows = await cursor.fetchall()
    except Exception as exc:
        # Before the eval migrations have run the tables are simply absent,
        # which is an honest "no data". Anything else is a failed read and must
        # not masquerade as "nothing to report".
        if isinstance(exc, sqlite3.OperationalError) and "no such table" in str(exc):
            logger.debug("eval tables not available (expected before first benchmark)")
            return {"status": "no_data", "providers": []}
        logger.warning("eval_staleness query failed", exc_info=True)
        return {"status": "error", "providers": [], "error": type(exc).__name__}

    if not rows:
        return {"status": "no_data", "providers": []}

    # A scheduled profile is superseded when its provider has a scheduled run
    # under a DIFFERENT profile that is newer than this profile's last one.
    newest_scheduled: dict[str, str] = {}
    for row in rows:
        if row[8] and (row[0] not in newest_scheduled or row[8] > newest_scheduled[row[0]]):
            newest_scheduled[row[0]] = row[8]

    providers = []
    stale_count = 0
    superseded_count = 0
    unreliable_count = 0

    for row in rows:
        (model_id, profile, last_run, days_ago, t_pass, t_fail, t_skip,
         scheduled, last_sched, w_runs, w_pass, w_fail, w_skip, w_infra) = row
        days_ago = days_ago or 0
        t_pass, t_fail, t_skip = t_pass or 0, t_fail or 0, t_skip or 0
        w_pass, w_fail, w_skip, w_infra = w_pass or 0, w_fail or 0, w_skip or 0, w_infra or 0
        scheduled = bool(scheduled)

        # A failed case with no per-case row cannot be classified; it stays
        # substantive, which is what every failure was treated as before.
        w_infra = min(w_infra, w_fail)
        w_substantive = w_fail - w_infra
        w_scored = w_pass + w_substantive
        w_all = w_pass + w_fail + w_skip
        infra_rate = _rate(w_skip + w_infra, w_all)

        superseded = bool(
            scheduled and last_sched and newest_scheduled.get(model_id, "") > last_sched
        )
        # Overdue is measured from the last SCHEDULED run, so a manual run of
        # the same profile cannot mask a scheduler that has stopped.
        stale = (
            scheduled
            and not superseded
            and _days_since(last_sched, days_ago) > STALE_AFTER_DAYS
        )
        unreliable = w_all > 0 and (w_skip + w_infra) / w_all > UNRELIABLE_INFRA_RATE

        all_time_cases = t_pass + t_fail + t_skip
        providers.append({
            "provider": model_id,
            "profile": profile,
            "last_run": last_run,
            "days_ago": days_ago,
            "total_passed": t_pass,
            "total_failed": t_fail,
            "total_skipped": t_skip,
            "skip_rate": _rate(t_skip, all_time_cases),
            "scheduled": scheduled,
            "superseded": superseded,
            "stale": stale,
            "unreliable": unreliable,
            "recent": {
                "window_days": RATE_WINDOW_DAYS,
                "runs": w_runs or 0,
                "passed": w_pass,
                "failed": w_substantive,
                "infra_failed": w_infra,
                "skipped": w_skip,
                # Wrong answers among the cases that were actually scored.
                "failure_rate": _rate(w_substantive, w_scored),
                # Cases the harness could not measure, among all cases.
                "infra_rate": infra_rate,
            },
        })

        stale_count += stale
        superseded_count += superseded
        # The per-row flag stays on every profile, but the COUNT (the alarm)
        # only names profiles still running: a manual or superseded profile
        # can never clear its flag, and a finding that can never clear is
        # noise at the summary level.
        unreliable_count += unreliable and scheduled and not superseded

    return {
        "status": "ok",
        "providers": providers,
        "total_benchmarked": len(providers),
        "stale_count": stale_count,
        "superseded_count": superseded_count,
        "unreliable_count": unreliable_count,
    }
