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
from datetime import datetime
from typing import Any

from genesis.db.crud import ledger_predictions as lp_crud
from genesis.ledger.metrics import REGISTRY

logger = logging.getLogger(__name__)

# ── Per-class grader alarm counters (process-global, mirror writers.py:60) ────
# Nonzero in the runtime process surfaces as ledger:metric_vanished:<class> /
# ledger:grade_failed:<class> via _compute_alerts. Restart resets them; the
# durable trail is the alert_events row the awareness tick persisted while the
# counter was nonzero. The health-MCP process always reads zero (read-only),
# consistent with the single-designated-writer rule.
_metric_vanished: Counter[str] = Counter()
_grade_failed: Counter[str] = Counter()


def grade_failure_counts() -> dict[str, dict[str, int]]:
    """Grader alarm counters since process start.

    ``{'metric_vanished': {<action_class>: n}, 'grade_failed': {<action_class>: n}}``.
    """
    return {
        "metric_vanished": dict(_metric_vanished),
        "grade_failed": dict(_grade_failed),
    }


def _reset_grade_failure_counts_for_tests() -> None:
    _metric_vanished.clear()
    _grade_failed.clear()


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
        )


async def _apply(db: Any, row: dict, res, report: GradeReport, *, now: datetime | None) -> None:
    """Translate one :class:`Resolution` into a ``resolve()`` call.

    Keyed on ``outcome_value`` first (the resolved-iff-non-None invariant),
    then the lane for the None-outcome dispositions.
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
    db: Any, *, now: datetime | None = None, limit: int = 500
) -> GradeReport:
    """Grade every open prediction whose deadline has passed. Idempotent.

    ``now`` is injectable for tests (also threaded into ``list_due_open`` and
    each resolver so a single frozen clock drives the whole pass).
    """
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
        await _apply(db, row, res, report, now=now)
    return report
