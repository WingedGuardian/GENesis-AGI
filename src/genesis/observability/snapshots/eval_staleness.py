"""Eval benchmark staleness snapshot.

Reports freshness and data quality of benchmark runs, one entry per
``(model_id, model_profile)`` pair. Served in the health snapshot and rendered
on the dashboard's surplus panel. No in-repo code reads the summary counts
(``stale_count`` etc.) yet; the per-profile rows are what the dashboard shows.

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


async def _table_exists(db: aiosqlite.Connection, name: str) -> bool | None:
    """Whether ``name`` exists; ``None`` when even that cannot be read.

    ``None`` means "could not establish absence", so the ``no_data`` branch
    requires ``False`` and a failed probe reports the read as an error.
    """
    try:
        cursor = await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        )
        return (await cursor.fetchone()) is not None
    except Exception:
        logger.warning("eval_staleness: could not probe for table %s", name, exc_info=True)
        return None


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
            WITH v AS (
                -- One parse per row. A timestamp julianday() cannot read is
                -- NULL here; it is counted (``unreadable_runs``) and kept out of
                -- every recency figure, so it can neither pose as the newest
                -- run (text MAX sorts 'not-a-date' above any ISO date) nor be
                -- silently aged as "today".
                -- julianday() alone also accepts 'now' and bare numbers,
                -- so only a date-shaped value is parsed.
                SELECT *,
                    CASE WHEN created_at GLOB
                              '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
                         THEN julianday(created_at) END AS jd
                FROM eval_runs
            ),
            g AS (
                SELECT
                    model_id,
                    model_profile,
                    MAX(CASE WHEN jd IS NOT NULL THEN created_at END) AS last_run,
                    MAX(jd) AS last_jd,
                    CAST(julianday('now') - MAX(jd) AS INTEGER) AS days_ago,
                    SUM(passed_cases) AS total_passed,
                    SUM(failed_cases) AS total_failed,
                    SUM(skipped_cases) AS total_skipped,
                    MAX(trigger IN (?, ?)) AS scheduled,
                    MAX(CASE WHEN trigger IN (?, ?) THEN jd END) AS last_sched_jd,
                    SUM(jd IS NULL) AS unreadable_runs
                FROM v
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
                FROM v r
                JOIN g ON g.model_id = r.model_id
                      AND g.model_profile IS r.model_profile
                WHERE r.jd >= g.last_jd - ?
                GROUP BY r.model_id, r.model_profile
            )
            SELECT
                g.model_id, g.model_profile, g.last_run, g.days_ago,
                g.total_passed, g.total_failed, g.total_skipped,
                g.scheduled, g.last_sched_jd,
                -- Same engine and same 'now' as days_ago, so the two ages are
                -- comparable; NULL when no recurring run has a readable time.
                CAST(julianday('now') - g.last_sched_jd AS INTEGER),
                g.unreadable_runs,
                w.runs, w.passed, w.failed, w.skipped, w.infra_failed
            FROM g
            -- LEFT: a profile none of whose timestamps can be read has no
            -- window; it is still reported (with unreadable_runs), not dropped.
            LEFT JOIN w ON w.model_id = g.model_id AND w.model_profile IS g.model_profile
            ORDER BY g.last_jd DESC
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
        if (
            isinstance(exc, sqlite3.OperationalError)
            and "no such table" in str(exc)
            and await _table_exists(db, "eval_runs") is False
            and await _table_exists(db, "eval_results") is False
        ):
            logger.debug("eval tables not available (expected before first benchmark)")
            return {"status": "no_data", "providers": []}
        # Includes HALF the schema (either table without the other): that is a
        # broken install, and "no data" would hide the broken read.
        logger.warning("eval_staleness query failed", exc_info=True)
        return {"status": "error", "providers": [], "error": type(exc).__name__}

    if not rows:
        return {"status": "no_data", "providers": []}

    # A scheduled profile is superseded when its provider has a scheduled run
    # under a DIFFERENT profile that is newer than this profile's last one.
    # Compared as julianday numbers: unreadable timestamps are already NULL.
    newest_scheduled: dict[str, float] = {}
    for row in rows:
        if row[8] is not None and row[8] > newest_scheduled.get(row[0], float("-inf")):
            newest_scheduled[row[0]] = row[8]

    providers = []
    stale_count = 0
    superseded_count = 0
    unreliable_count = 0

    for row in rows:
        (model_id, profile, last_run, days_ago, t_pass, t_fail, t_skip,
         scheduled, last_sched, days_since_scheduled, unreadable,
         w_runs, w_pass, w_fail, w_skip, w_infra) = row
        # ``days_ago`` stays None when no timestamp is readable: an unknown age
        # is not "today".
        unreadable = unreadable or 0
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
            scheduled
            and last_sched is not None
            and newest_scheduled.get(model_id, float("-inf")) > last_sched
        )
        # Overdue is measured from the last SCHEDULED run, so a manual run of
        # the same profile cannot mask a scheduler that has stopped. The age
        # the verdict was taken on is reported as ``days_since_scheduled`` so a
        # reader shows THAT number beside a stale flag, not ``days_ago`` (which
        # a manual run refreshes). An unparseable scheduled timestamp yields
        # NULL there: reported as unknown (``unreadable_runs``, rendered on the
        # dashboard), never silently replaced by ``days_ago``. Debug, not
        # warning: the dashboard route rebuilds this on every poll.
        if scheduled and days_since_scheduled is None:
            logger.debug(
                "eval_staleness: no readable scheduled-run timestamp for %s/%s",
                model_id, profile,
            )
        stale = bool(
            scheduled
            and not superseded
            and days_since_scheduled is not None
            and days_since_scheduled > STALE_AFTER_DAYS
        )
        unreliable = w_all > 0 and (w_skip + w_infra) / w_all > UNRELIABLE_INFRA_RATE

        all_time_cases = t_pass + t_fail + t_skip
        providers.append({
            "provider": model_id,
            "profile": profile,
            "last_run": last_run,
            "days_ago": days_ago,
            "days_since_scheduled": days_since_scheduled,
            # Runs whose created_at julianday() cannot read: counted in the
            # all-time totals, excluded from every age and from the window.
            "unreadable_runs": unreadable,
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
