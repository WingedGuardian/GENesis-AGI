"""WS-2 Cognitive Ledger — the metric registry (falsifiability gate 1 of 3).

A prediction can only be written for a metric registered here, and every
registered metric carries a ``resolver_fn`` — pure SQL/Python over evidence
tables, zero LLM calls. The resolver IS the falsifiability proof: adding a
metric means implementing (and reviewing) the mechanical check that grades it.

Resolvers are implemented and unit-tested in P1a; the P2 grader executes them
on schedule. Each resolver takes the shared DB connection, the prediction row
(dict), and ``now``, and returns a :class:`Resolution`:

- ``outcome_value`` 1/0 — mechanically graded; ``None`` — not resolvable yet
  (lane ``open``), premise absent (lane ``void:*``), needs the LLM fallback
  (lane ``fuzzy_pending``), or broken evidence (lane ``unresolvable:*``).
- ``lane`` names the mechanical rule that fired — returned to the grader as
  grading provenance (the P2 grader persists it with the grade so calibration
  can be audited per rule).

Evidence sources (locked in the design doc §2.3.1): outreach metrics resolve
off ``outreach_history.engagement_signal`` (exists today, measured 99.5%
mechanical on the live DB); task/ego metrics resolve off their state tables
until the T1 bus lane (P1b emits) matures. Migrating a resolver to
``outcome_events`` later changes no registry contract.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from genesis.outreach.types import POSITIVE_ENGAGEMENT_OUTCOMES

# Writer-side global cap: no prediction may set a deadline beyond now + cap.
# Headroom above the longest per-metric horizon; the §2.6 specificity audit
# watches for deadlines drifting toward this ceiling ("calibration by
# cowardice" via unfalsifiably-distant deadlines).
HORIZON_CAP = timedelta(days=30)

# Task predictions: no task-level timeout exists (only per-step bounds), so
# the horizon is a constant bounded-runtime allowance plus this grace.
TASK_GRACE = timedelta(hours=24)


class Resolution(NamedTuple):
    """One resolver verdict: outcome (or None), evidence pointer, rule lane."""

    outcome_value: int | None
    evidence_ref: str | None
    lane: str


Resolver = Callable[..., Awaitable[Resolution]]


@dataclass(frozen=True)
class MetricSpec:
    """Registry entry — the falsifiability contract for one metric."""

    action_class: str  # the only action_class this metric may attach to
    comparator_domain: frozenset[str]  # allowed comparators ('is_true' | 'le' | 'ge')
    resolver_fn: Resolver
    absence_semantics: str  # 'zero' | 'void' | 'fuzzy_pending' — no evidence at deadline means...
    default_horizon: timedelta
    fuzzy: bool = False  # True = grader routes to the bounded LLM fallback lane


# ── Outreach lane maps (spike-validated 2026-07-16, §2.3.1 locked semantics) ──

# Signals that mechanically mean "an actual reply was captured".
_REPLY_SIGNALS = frozenset({"user_reply"})
# Signals that mechanically mean "no direct reply": timeout is the 24h
# verdict; the rest mean the user was active or engaged *elsewhere* — the
# separate positive_engagement metric owns that softer signal.
_NO_REPLY_SIGNALS = frozenset(
    {"timeout", "implicit_activity", "auto_digest", "acted_on", "acknowledged"}
)
# 'useful' is what record_reply writes — a signal-less 'useful' is a legacy
# reply row from before the engagement_signal column existed.
_REPLY_OUTCOMES = frozenset({"useful"})
_NO_REPLY_OUTCOMES = frozenset(
    {"ignored", "ambivalent", "not_useful", "engaged", "acted_on", "acknowledged"}
)

# Terminal task phases that conclusively negate 'completed'.
_TASK_FAILED_PHASES = frozenset({"failed", "cancelled"})
# Ego proposal statuses that conclusively negate 'approved_and_executes'.
_EGO_NEGATIVE_STATUSES = frozenset({"rejected", "expired", "failed", "tabled", "withdrawn"})


def _past_deadline(prediction_row: dict, now: datetime) -> bool | None:
    """True/False vs the row's deadline; None if the stored deadline is garbage."""
    raw = prediction_row.get("deadline_at") or ""
    try:
        deadline = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    return now >= deadline


async def _fetch_one(db: Any, sql: str, params: tuple) -> dict | None:
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    return dict(row) if row is not None else None


def _absence(prediction_row: dict, now: datetime) -> Resolution:
    """Shared zero-absence tail: silence past the deadline IS the answer."""
    past = _past_deadline(prediction_row, now)
    if past is None:
        return Resolution(None, None, "unresolvable:bad_deadline")
    if past:
        return Resolution(0, None, "mechanical_absence")
    return Resolution(None, None, "open")


# ── outreach_send ─────────────────────────────────────────────────────────────


async def _resolve_reply_received(db: Any, prediction_row: dict, *, now: datetime) -> Resolution:
    """Spike-validated lane map: did an actual reply arrive by deadline?"""
    row = await _fetch_one(
        db,
        "SELECT id, engagement_signal, engagement_outcome, user_response "
        "FROM outreach_history WHERE id = ?",
        (prediction_row["subject_ref_id"],),
    )
    if row is None:
        return Resolution(None, None, "unresolvable:subject_missing")
    evidence = f"outreach_history:{row['id']}"
    signal = row["engagement_signal"] or ""
    outcome = row["engagement_outcome"] or ""

    # Affirmative: a captured reply always wins, regardless of outcome label.
    if signal in _REPLY_SIGNALS or row["user_response"]:
        return Resolution(1, evidence, "user_reply")
    if not signal and outcome in _REPLY_OUTCOMES:
        return Resolution(1, evidence, f"legacy_outcome:{outcome}")

    if signal in _NO_REPLY_SIGNALS:
        return Resolution(0, evidence, f"signal:{signal}")
    # Signal missing but a mechanical outcome label exists (legacy rows from
    # pre-signal-column writers).
    if not signal and outcome in _NO_REPLY_OUTCOMES:
        return Resolution(0, evidence, f"outcome:{outcome}")

    return _absence(prediction_row, now)


async def _resolve_positive_engagement(
    db: Any, prediction_row: dict, *, now: datetime
) -> Resolution:
    """Softer signal than a reply: any canonical positive engagement by deadline."""
    row = await _fetch_one(
        db,
        "SELECT id, engagement_signal, engagement_outcome, user_response "
        "FROM outreach_history WHERE id = ?",
        (prediction_row["subject_ref_id"],),
    )
    if row is None:
        return Resolution(None, None, "unresolvable:subject_missing")
    evidence = f"outreach_history:{row['id']}"
    signal = row["engagement_signal"] or ""
    outcome = row["engagement_outcome"] or ""
    if signal in _REPLY_SIGNALS or row["user_response"]:
        return Resolution(1, evidence, "user_reply")
    if outcome in POSITIVE_ENGAGEMENT_OUTCOMES:
        return Resolution(1, evidence, f"positive_outcome:{outcome}")
    # Negative labels before the deadline stay open — record_reply upgrades an
    # earlier 'ignored'/'ambivalent' unconditionally, so only the deadline
    # closes the question.
    return _absence(prediction_row, now)


# ── task_execution ────────────────────────────────────────────────────────────


async def _resolve_completed(db: Any, prediction_row: dict, *, now: datetime) -> Resolution:
    row = await _fetch_one(
        db,
        "SELECT task_id, current_phase FROM task_states WHERE task_id = ?",
        (prediction_row["subject_ref_id"],),
    )
    if row is None:
        return Resolution(None, None, "unresolvable:subject_missing")
    evidence = f"task_states:{row['task_id']}"
    phase = row["current_phase"] or ""
    if phase == "completed":
        return Resolution(1, evidence, "completed")
    if phase in _TASK_FAILED_PHASES:
        return Resolution(0, evidence, f"phase:{phase}")
    return _absence(prediction_row, now)


async def _resolve_completed_first_attempt(
    db: Any, prediction_row: dict, *, now: datetime
) -> Resolution:
    """Completed AND no negative execution outcome recorded for the task.

    Until the P1b executor emits land, failed attempts are only visible via
    ``outcome_events`` rows — so pre-P1b this degrades to ``completed``
    (documented limitation: ``task_states`` keeps no transition history).
    """
    base = await _resolve_completed(db, prediction_row, now=now)
    if base.outcome_value != 1:
        return base
    negative = await _fetch_one(
        db,
        "SELECT id FROM outcome_events WHERE ref_type = 'task' AND ref_id = ? "
        "AND signal_type = 'execution_outcome' AND polarity = 'negative' LIMIT 1",
        (prediction_row["subject_ref_id"],),
    )
    if negative is not None:
        return Resolution(0, f"outcome_events:{negative['id']}", "failed_attempt_evidence")
    return Resolution(1, base.evidence_ref, "completed_first_attempt")


async def _resolve_acceptance_pass(db: Any, prediction_row: dict, *, now: datetime) -> Resolution:
    """Fuzzy by design (v1's only fuzzy metric) — the P2 grader queues it for
    the bounded LLM fallback lane; there is no mechanical evidence source yet."""
    return Resolution(None, None, "fuzzy_pending")


# ── scheduled_job (subject_ref_id = '<job_name>:<YYYY-MM-DD>') ───────────────


def _split_job_day(subject_ref_id: str) -> tuple[str, str] | None:
    job_name, sep, day = subject_ref_id.rpartition(":")
    if not sep or len(day) != 10:
        return None
    return job_name, day


async def _resolve_runs_clean_day(db: Any, prediction_row: dict, *, now: datetime) -> Resolution:
    from genesis.db.crud.job_run_events import list_runs_for_day

    parsed = _split_job_day(prediction_row["subject_ref_id"])
    if parsed is None:
        return Resolution(None, None, "unresolvable:bad_subject_ref")
    job_name, day = parsed
    runs = await list_runs_for_day(db, job_name, day)
    failed = [r for r in runs if r["status"] == "failed"]
    if failed:
        return Resolution(0, f"job_run_events:{failed[0]['id']}", "failed_runs")
    past = _past_deadline(prediction_row, now)
    if past is None:
        return Resolution(None, None, "unresolvable:bad_deadline")
    if not past:
        return Resolution(None, None, "open")
    if not runs:
        # Premise absent: the job never fired that day — void, and itself a
        # separate alarm (the grader counts voids per class).
        return Resolution(None, None, "void:no_runs")
    return Resolution(1, f"job_run_events:{runs[0]['id']}", "clean_day")


async def _resolve_runtime_ms_le(db: Any, prediction_row: dict, *, now: datetime) -> Resolution:
    from genesis.db.crud.job_run_events import list_runs_for_day

    parsed = _split_job_day(prediction_row["subject_ref_id"])
    if parsed is None:
        return Resolution(None, None, "unresolvable:bad_subject_ref")
    threshold = prediction_row.get("threshold")
    if threshold is None:
        return Resolution(None, None, "unresolvable:missing_threshold")
    job_name, day = parsed
    runs = [r for r in await list_runs_for_day(db, job_name, day) if r["duration_ms"] is not None]
    exceeded = [r for r in runs if r["duration_ms"] > threshold]
    if exceeded:
        return Resolution(0, f"job_run_events:{exceeded[0]['id']}", "exceeded_threshold")
    past = _past_deadline(prediction_row, now)
    if past is None:
        return Resolution(None, None, "unresolvable:bad_deadline")
    if not past:
        return Resolution(None, None, "open")
    if not runs:
        # duration_ms is only real where record_job_start marked the run —
        # honest instrument: no measured runs means void, never a free pass.
        return Resolution(None, None, "void:no_duration_data")
    return Resolution(1, f"job_run_events:{runs[0]['id']}", "within_threshold")


# ── build_verdict ─────────────────────────────────────────────────────────────


async def _resolve_user_greenlights(db: Any, prediction_row: dict, *, now: datetime) -> Resolution:
    row = await _fetch_one(
        db,
        "SELECT id, user_decision FROM build_candidates WHERE id = ?",
        (prediction_row["subject_ref_id"],),
    )
    if row is None:
        return Resolution(None, None, "unresolvable:subject_missing")
    evidence = f"build_candidates:{row['id']}"
    decision = row["user_decision"] or ""
    if decision == "approved":
        return Resolution(1, evidence, "approved")
    if decision in ("rejected", "discussed"):
        return Resolution(0, evidence, f"decision:{decision}")
    return _absence(prediction_row, now)


# ── ego_proposal ──────────────────────────────────────────────────────────────


async def _resolve_approved_and_executes(
    db: Any, prediction_row: dict, *, now: datetime
) -> Resolution:
    row = await _fetch_one(
        db,
        "SELECT id, status FROM ego_proposals WHERE id = ?",
        (prediction_row["subject_ref_id"],),
    )
    if row is None:
        return Resolution(None, None, "unresolvable:subject_missing")
    evidence = f"ego_proposals:{row['id']}"
    status = row["status"] or ""
    if status == "executed":
        return Resolution(1, evidence, "executed")
    if status in _EGO_NEGATIVE_STATUSES:
        return Resolution(0, evidence, f"status:{status}")
    # 'pending'/'approved' without execution: the deadline decides — the
    # metric is approved AND executes, not approved alone.
    return _absence(prediction_row, now)


# ── The v1 registry (complete list — design doc §2.3) ────────────────────────

_IS_TRUE = frozenset({"is_true"})

REGISTRY: dict[str, MetricSpec] = {
    "reply_received": MetricSpec(
        action_class="outreach_send",
        comparator_domain=_IS_TRUE,
        resolver_fn=_resolve_reply_received,
        absence_semantics="zero",
        default_horizon=timedelta(hours=72),
    ),
    "positive_engagement": MetricSpec(
        action_class="outreach_send",
        comparator_domain=_IS_TRUE,
        resolver_fn=_resolve_positive_engagement,
        absence_semantics="zero",
        default_horizon=timedelta(hours=72),
    ),
    "completed": MetricSpec(
        action_class="task_execution",
        comparator_domain=_IS_TRUE,
        resolver_fn=_resolve_completed,
        absence_semantics="zero",
        default_horizon=timedelta(hours=24) + TASK_GRACE,
    ),
    "completed_first_attempt": MetricSpec(
        action_class="task_execution",
        comparator_domain=_IS_TRUE,
        resolver_fn=_resolve_completed_first_attempt,
        absence_semantics="zero",
        default_horizon=timedelta(hours=24) + TASK_GRACE,
    ),
    "acceptance_pass": MetricSpec(
        action_class="task_execution",
        comparator_domain=_IS_TRUE,
        resolver_fn=_resolve_acceptance_pass,
        absence_semantics="fuzzy_pending",
        default_horizon=timedelta(hours=24) + TASK_GRACE,
        fuzzy=True,
    ),
    "runs_clean_day": MetricSpec(
        action_class="scheduled_job",
        comparator_domain=_IS_TRUE,
        resolver_fn=_resolve_runs_clean_day,
        absence_semantics="void",
        default_horizon=timedelta(hours=24),
    ),
    "runtime_ms_le": MetricSpec(
        action_class="scheduled_job",
        comparator_domain=frozenset({"le"}),
        resolver_fn=_resolve_runtime_ms_le,
        absence_semantics="void",
        default_horizon=timedelta(hours=24),
    ),
    "user_greenlights": MetricSpec(
        action_class="build_verdict",
        comparator_domain=_IS_TRUE,
        resolver_fn=_resolve_user_greenlights,
        absence_semantics="zero",
        default_horizon=timedelta(days=7),
    ),
    "approved_and_executes": MetricSpec(
        action_class="ego_proposal",
        comparator_domain=_IS_TRUE,
        resolver_fn=_resolve_approved_and_executes,
        absence_semantics="zero",
        default_horizon=timedelta(days=7),
    ),
}
