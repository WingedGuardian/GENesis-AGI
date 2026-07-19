"""WS-2 Cognitive Ledger — the mechanical grader (P2).

Reads open predictions whose deadline has passed, runs the registered
``resolver_fn`` for each (pure SQL/Python over evidence tables — ZERO LLM
calls, enforced by ``test_no_llm_import_path``), and writes the mechanical
grade through ``ledger_predictions.resolve``. This closes the loop the
proto-ledger never did: predictions stop rotting ungraded.

Mapping is keyed on ``outcome_value`` FIRST — the invariant across all nine
resolvers (``metrics.py``) is that ``outcome_value`` is non-None **iff** the
row is ``resolved``; every None-outcome return is ``open`` / ``unresolvable:*``
/ ``void:*`` / ``fuzzy_pending``. Keying on the lane instead would risk calling
``resolve('resolved', outcome_value=None)``, which the CRUD's scored-status
guard rejects.

The one fuzzy metric (``acceptance_pass``) is parked as ``fuzzy_pending`` here;
the bounded LLM fallback that drains it is a separate module/job, deferred
until any ``acceptance_pass`` prediction is actually written (no writer emits
one in v1). Keeping the fallback out of this module is what lets the no-LLM
import lock hold.

Alarm surface mirrors ``ledger/writers.py``: registry-vanished metrics
(schema-vs-code drift) and resolver exceptions bump per-class process counters
read by ``mcp/health/errors._compute_alerts`` → the M10 awareness-tick
``alert_events`` writer. Resolver-returned ``void:*`` / ``unresolvable:*`` are
recorded in the report but do NOT alarm — they are normal grading outcomes
(premise absent / subject row deleted), not code faults.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from genesis.db.crud import ledger_predictions as lp_crud
from genesis.ledger import cells as _cells
from genesis.ledger.metrics import REGISTRY
from genesis.ledger.ws2_ledger_config import autonomy_feed_mode

logger = logging.getLogger(__name__)

# ── Per-class grader alarm counters (process-global, mirror writers.py:60) ────
# Nonzero in the runtime process surfaces as ledger:metric_vanished:<class> /
# ledger:grade_failed:<class> via _compute_alerts. Restart resets them; the
# durable trail is the alert_events row the awareness tick persisted while the
# counter was nonzero. The health-MCP process always reads zero (read-only),
# consistent with the single-designated-writer rule.
_metric_vanished: Counter[str] = Counter()
_grade_failed: Counter[str] = Counter()
# WS-2 P2b: live autonomy-feed side-effect failures (record_success/correction
# raised). Non-critical — the grade itself landed; only the earn-back evidence
# didn't propagate — so log+count, no health alert (matches the removed
# pipeline block's non-fatal posture).
_autonomy_feed_failures: Counter[str] = Counter()


def grade_failure_counts() -> dict[str, dict[str, int]]:
    """Grader alarm counters since process start.

    ``{'metric_vanished': {<action_class>: n}, 'grade_failed': {<action_class>: n}}``.
    """
    return {
        "metric_vanished": dict(_metric_vanished),
        "grade_failed": dict(_grade_failed),
    }


def autonomy_feed_failure_counts() -> dict[str, int]:
    """Live autonomy-feed side-effect failures since process start (observability)."""
    return dict(_autonomy_feed_failures)


def _reset_grade_failure_counts_for_tests() -> None:
    _metric_vanished.clear()
    _grade_failed.clear()
    _autonomy_feed_failures.clear()


@dataclass
class GradeReport:
    """One grading pass, tallied for the job log + the coverage falsifier."""

    scanned: int = 0
    resolved: int = 0
    mechanical: int = 0  # resolved via a mechanical rule (evidence present)
    absence: int = 0  # resolved via mechanical_absence (silence past deadline)
    void: int = 0
    unresolvable: int = 0
    fuzzy_pending: int = 0
    left_open: int = 0
    per_class: dict[str, Counter[str]] = field(default_factory=dict)
    # WS-2 P2b autonomy feed, keyed "<mode>:<kind>" (e.g. "shadow:success",
    # "live:correction") — what the grader fired (live) or would have (shadow).
    autonomy: Counter[str] = field(default_factory=Counter)
    # WS-2 P3: calibration cells recomputed at the end of this pass (0 when
    # the recompute failed — see cells.cell_recompute_failure_counts).
    cells_written: int = 0

    def _bump(self, action_class: str, kind: str) -> None:
        self.per_class.setdefault(action_class, Counter())[kind] += 1

    def mechanical_share(self) -> float | None:
        """Fraction of graded (non-void) rows resolved mechanically — the §7
        falsifier watches this (<70% over 2 weeks ⇒ redesign the registry)."""
        graded = self.mechanical + self.absence
        denom = graded + self.fuzzy_pending
        return graded / denom if denom else None

    def summary(self) -> str:
        share = self.mechanical_share()
        share_s = f"{share:.2f}" if share is not None else "n/a"
        return (
            f"scanned={self.scanned} resolved={self.resolved} "
            f"(mechanical={self.mechanical} absence={self.absence}) "
            f"void={self.void} unresolvable={self.unresolvable} "
            f"fuzzy_pending={self.fuzzy_pending} left_open={self.left_open} "
            f"mechanical_share={share_s}"
            + (f" autonomy={dict(self.autonomy)}" if self.autonomy else "")
            + (f" cells={self.cells_written}" if self.cells_written else "")
        )


async def _feed_autonomy(
    row: dict,
    res,
    report: GradeReport,
    *,
    autonomy_manager: Any,
    autonomy_feed: str,
    now: datetime,
) -> None:
    """FAILURE-ONLY autonomy earn-back evidence from a graded task row (P2b).

    Replaces the removed pipeline self-grade feed. Fires ONLY on genuine
    completion (lane ``completed`` → success) or genuine execution failure
    (lane ``phase:failed`` → correction). A task that merely missed its
    deadline while still running (``mechanical_absence``) or was cancelled
    (``phase:cancelled``) is NOT a competence signal — a correction there would
    spuriously demote direct_session. SHADOW-FIRST: shadow logs the intent and
    writes nothing; live fires the manager seam fire-and-forget (a raise never
    touches the grade, which already landed).
    """
    if res.lane == "completed":
        kind = "success"
    elif res.lane == "phase:failed":
        kind = "correction"
    else:
        return  # slowness / cancellation / anything else — no autonomy signal
    if autonomy_feed == "off":
        return
    if autonomy_feed == "shadow":
        report.autonomy[f"shadow:{kind}"] += 1  # what live WOULD fire
        logger.info(
            "[ledger-autonomy shadow] WOULD record_%s(direct_session) for task %s (lane=%s)",
            kind,
            row["subject_ref_id"],
            res.lane,
        )
        return
    if autonomy_manager is None:  # live but no manager wired — nothing to fire
        return
    # Count only once we know a manager exists to fire into — otherwise the
    # report would claim a live fire that never happened (hiding a wiring
    # regression). A raise below still counts as fired-then-failed via
    # _autonomy_feed_failures, which is the honest record.
    report.autonomy[f"live:{kind}"] += 1
    try:
        if kind == "success":
            await autonomy_manager.record_success("direct_session")
        else:
            await autonomy_manager.record_correction("direct_session", corrected_at=now.isoformat())
    except Exception:
        _autonomy_feed_failures[kind] += 1
        logger.error(
            "ledger autonomy feed (%s) failed for task %s — grade unaffected",
            kind,
            row["subject_ref_id"],
            exc_info=True,
        )


async def _apply(
    db: Any,
    row: dict,
    res,
    report: GradeReport,
    *,
    now: datetime,
    autonomy_manager: Any = None,
    autonomy_feed: str = "off",
) -> None:
    """Translate one :class:`Resolution` into a ``resolve()`` call.

    Keyed on ``outcome_value`` first (the resolved-iff-non-None invariant),
    then the lane for the None-outcome dispositions. A resolved
    task_execution/completed row additionally feeds autonomy earn-back evidence
    (failure-only, shadow-first — P2b).
    """
    action_class = row["action_class"]
    if res.outcome_value is not None:
        resolver = "mechanical_absence" if res.lane == "mechanical_absence" else "mechanical"
        changed = await lp_crud.resolve(
            db,
            row["id"],
            status="resolved",
            outcome_value=res.outcome_value,
            resolver=resolver,
            evidence_ref=res.evidence_ref,
            now=now,
        )
        if changed:
            report.resolved += 1
            if resolver == "mechanical_absence":
                report.absence += 1
            else:
                report.mechanical += 1
            report._bump(action_class, f"resolved:{res.lane}")
            # Exactly-once: only a genuine open→resolved transition (changed)
            # feeds autonomy — the idempotent resolve() guard makes re-grades
            # no-ops, so no double-fire across the twice-daily passes.
            if action_class == "task_execution" and row["metric"] == "completed":
                await _feed_autonomy(
                    row,
                    res,
                    report,
                    autonomy_manager=autonomy_manager,
                    autonomy_feed=autonomy_feed,
                    now=now,
                )
        return

    lane = res.lane
    if lane.startswith("void"):
        if await lp_crud.resolve(db, row["id"], status="void", now=now):
            report.void += 1
            report._bump(action_class, lane)
    elif lane.startswith("unresolvable"):
        if await lp_crud.resolve(db, row["id"], status="unresolvable", now=now):
            report.unresolvable += 1
            report._bump(action_class, lane)
    elif lane == "fuzzy_pending":
        if await lp_crud.resolve(db, row["id"], status="fuzzy_pending", now=now):
            report.fuzzy_pending += 1
            report._bump(action_class, lane)
    else:
        # lane == "open": the resolver judged the deadline not actually past —
        # a canonicalization edge vs list_due_open's filter that should not
        # occur. Leave the row open, count it (a persistent nonzero here is a
        # clock/canonicalization bug worth noticing in the report).
        report.left_open += 1
        report._bump(action_class, "left_open")


async def grade_due_predictions(
    db: Any, *, now: datetime | None = None, limit: int = 500, autonomy_manager: Any = None
) -> GradeReport:
    """Grade every open prediction whose deadline has passed. Idempotent.

    ``now`` is injectable for tests (also threaded into ``list_due_open`` and
    each resolver so a single frozen clock drives the whole pass). Default it
    HERE — the production call site passes none, and the resolvers'
    ``_past_deadline`` check does ``now >= deadline`` with no fallback, so a
    ``None`` would TypeError every absence-path row into ``grade_failed``
    instead of resolving it.

    ``autonomy_manager`` (the runtime ``AutonomyManager``) enables the P2b
    earn-back feed; the mode (off/shadow/live) is read LIVE once per pass from
    the ws2_ledger settings domain. Omitting it (tests, or a caller with no
    runtime) simply skips the feed.
    """
    now = now or datetime.now(UTC)
    autonomy_feed = autonomy_feed_mode()
    report = GradeReport()
    rows = await lp_crud.list_due_open(db, now=now, limit=limit)
    report.scanned = len(rows)
    for row in rows:
        action_class = row["action_class"]
        metric = row["metric"]
        spec = REGISTRY.get(metric)
        if spec is None:
            # Gate 3: the metric vanished from the registry (a code rollback).
            # Never silently skip — mark unresolvable and alarm (schema-vs-code
            # drift sensor).
            _metric_vanished[action_class] += 1
            if await lp_crud.resolve(db, row["id"], status="unresolvable", now=now):
                report.unresolvable += 1
                report._bump(action_class, "unresolvable:metric_vanished")
            continue
        try:
            res = await spec.resolver_fn(db, row, now=now)
        except Exception:
            # A resolver must never abort the batch: count it, leave the row
            # open, move on. logger.error (not debug) — a raising resolver is a
            # real bug, surfaced as ledger:grade_failed:<class>.
            _grade_failed[action_class] += 1
            logger.error(
                "ledger resolver raised for %s/%s (row %s) — left open",
                action_class,
                metric,
                row["id"],
                exc_info=True,
            )
            report.left_open += 1
            report._bump(action_class, "grade_failed")
            continue
        await _apply(
            db,
            row,
            res,
            report,
            now=now,
            autonomy_manager=autonomy_manager,
            autonomy_feed=autonomy_feed,
        )

    # WS-2 P3: refresh the calibration cells from the graded record. Never
    # allowed to break grading — the grades above are committed row-by-row;
    # a recompute failure only leaves derived data stale (counted + logged).
    try:
        cell_report = await _cells.recompute_calibration_cells(db, now=now)
        report.cells_written = cell_report.cells_written
    except Exception:
        _cells._recompute_failed["recompute"] += 1
        logger.error(
            "calibration-cell recompute failed — grades landed, cells stale",
            exc_info=True,
        )
    return report
