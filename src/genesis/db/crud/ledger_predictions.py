"""CRUD for ``ledger_predictions`` — gate 1 of the WS-2 falsifiability design.

The writer refuses (``ValueError``) any prediction that cannot be mechanically
graded: unknown metric, action_class not matching the metric's spec,
comparator outside the metric's comparator domain, broken comparator/threshold
pairing, confidence outside [0.01, 0.99], or a deadline outside
``(now, now + HORIZON_CAP]``. There is no free-text prediction that gets
graded — ``rationale`` is prose for humans and is never scored.

Convention notes: functions commit their own writes (never call these inside a
migration ``up()``); ALL validation happens before the first execute so a
raise can never strand an uncommitted row on the shared connection (the
no-rollback rule); timestamps go through ``canonical_iso`` (the single
write-path gate). Dedupe violations of ``UNIQUE (action_class, subject_ref_id,
metric)`` surface as ``sqlite3.IntegrityError`` — deliberately not swallowed
here; the P1b writer hooks decide (they debug-log legitimate re-entries).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import aiosqlite

from genesis.db.timeutil import canonical_iso
from genesis.ledger.metrics import HORIZON_CAP, REGISTRY

_PROVENANCES = frozenset({"stated", "policy_prior"})
# Target statuses resolve() may set; 'open' is birth-only.
_TERMINAL_STATUSES = frozenset({"resolved", "fuzzy_resolved", "void", "unresolvable"})
_QUEUE_STATUSES = frozenset({"fuzzy_pending"})
_RESOLVERS = frozenset({"mechanical", "mechanical_absence", "llm_fallback", "user"})

_COLUMNS = (
    "id, created_at, action_class, subject_ref_type, subject_ref_id, domain, "
    "metric, comparator, threshold, confidence, deadline_at, provenance, "
    "predictor, source_session, rationale, status, outcome_value, resolved_at, "
    "resolver, evidence_ref, brier, metadata"
)


async def create(
    db: aiosqlite.Connection,
    *,
    action_class: str,
    subject_ref_type: str,
    subject_ref_id: str,
    domain: str,
    metric: str,
    confidence: float,
    deadline_at: str,
    provenance: str,
    predictor: str,
    comparator: str = "is_true",
    threshold: float | None = None,
    source_session: str | None = None,
    rationale: str | None = None,
    metadata: dict | None = None,
    id: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Write one prediction row, or raise ``ValueError`` (falsifiability gate 1)."""
    now = now or datetime.now(UTC)

    spec = REGISTRY.get(metric)
    if spec is None:
        raise ValueError(f"unknown metric {metric!r} — not in the ledger registry")
    if action_class != spec.action_class:
        raise ValueError(
            f"metric {metric!r} belongs to action_class {spec.action_class!r}, got {action_class!r}"
        )
    if comparator not in spec.comparator_domain:
        raise ValueError(
            f"comparator {comparator!r} not allowed for metric {metric!r} "
            f"(allowed: {sorted(spec.comparator_domain)})"
        )
    if (comparator == "is_true") != (threshold is None):
        raise ValueError(
            "threshold is required for 'le'/'ge' comparators and forbidden for 'is_true'"
        )
    if threshold is not None and not isinstance(threshold, (int, float)):
        raise ValueError(f"threshold must be numeric, got {type(threshold).__name__}")
    if not isinstance(confidence, (int, float)) or not 0.01 <= confidence <= 0.99:
        raise ValueError(f"confidence must be in [0.01, 0.99], got {confidence!r}")
    if provenance not in _PROVENANCES:
        raise ValueError(f"provenance must be one of {sorted(_PROVENANCES)}, got {provenance!r}")

    canonical_deadline = canonical_iso(deadline_at)
    if canonical_deadline is None:
        raise ValueError(f"unparseable deadline_at {deadline_at!r}")
    deadline = datetime.fromisoformat(canonical_deadline)
    if not now < deadline <= now + HORIZON_CAP:
        raise ValueError(
            f"deadline_at must be in (now, now + {HORIZON_CAP}], got {canonical_deadline}"
        )

    row_id = id or uuid.uuid4().hex[:16]
    await db.execute(
        """INSERT INTO ledger_predictions
           (id, created_at, action_class, subject_ref_type, subject_ref_id,
            domain, metric, comparator, threshold, confidence, deadline_at,
            provenance, predictor, source_session, rationale, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row_id,
            canonical_iso(now.isoformat()),
            action_class,
            subject_ref_type,
            subject_ref_id,
            domain,
            metric,
            comparator,
            threshold,
            float(confidence),
            canonical_deadline,
            provenance,
            predictor,
            source_session,
            rationale,
            json.dumps(metadata) if metadata is not None else None,
        ),
    )
    await db.commit()
    created = await get_by_id(db, row_id)
    if created is None:  # pragma: no cover — the row was committed one statement ago
        raise RuntimeError(f"ledger prediction {row_id} vanished after insert")
    return created


async def get_by_id(db: aiosqlite.Connection, prediction_id: str) -> dict | None:
    cursor = await db.execute(
        f"SELECT {_COLUMNS} FROM ledger_predictions WHERE id = ?",  # noqa: S608 — column list is a module constant
        (prediction_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row is not None else None


async def list_due_open(
    db: aiosqlite.Connection, *, now: datetime | None = None, limit: int = 500
) -> list[dict]:
    """Open rows whose deadline has passed — the grader's work queue.

    Rides the partial index ``idx_lp_open_deadline`` (status='open').
    """
    now = now or datetime.now(UTC)
    cursor = await db.execute(
        f"SELECT {_COLUMNS} FROM ledger_predictions "  # noqa: S608
        "WHERE status = 'open' AND deadline_at <= ? ORDER BY deadline_at LIMIT ?",
        (canonical_iso(now.isoformat()), limit),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def list_by_subject(
    db: aiosqlite.Connection, *, action_class: str, subject_ref_id: str
) -> list[dict]:
    cursor = await db.execute(
        f"SELECT {_COLUMNS} FROM ledger_predictions "  # noqa: S608
        "WHERE action_class = ? AND subject_ref_id = ? ORDER BY metric",
        (action_class, subject_ref_id),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def resolve(
    db: aiosqlite.Connection,
    prediction_id: str,
    *,
    status: str,
    outcome_value: int | None = None,
    resolver: str | None = None,
    evidence_ref: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Grade one row. Returns False if the row is missing or already terminal.

    ``brier`` is computed in SQL from the stored confidence when an outcome is
    set. The guarded WHERE makes double-grading a no-op (idempotent grader).
    """
    if status not in _TERMINAL_STATUSES | _QUEUE_STATUSES:
        raise ValueError(f"invalid target status {status!r}")
    if outcome_value not in (None, 0, 1):
        raise ValueError(f"outcome_value must be 0, 1 or None, got {outcome_value!r}")
    if resolver is not None and resolver not in _RESOLVERS:
        raise ValueError(f"invalid resolver {resolver!r}")
    if outcome_value is not None and resolver is None:
        raise ValueError("resolver is required when outcome_value is set")

    now = now or datetime.now(UTC)
    cursor = await db.execute(
        """UPDATE ledger_predictions
           SET status = ?,
               outcome_value = ?,
               resolver = ?,
               evidence_ref = ?,
               resolved_at = ?,
               brier = CASE WHEN ? IS NOT NULL
                            THEN (confidence - ?) * (confidence - ?)
                            ELSE brier END
           WHERE id = ? AND status IN ('open', 'fuzzy_pending')""",
        (
            status,
            outcome_value,
            resolver,
            evidence_ref,
            canonical_iso(now.isoformat()),
            outcome_value,
            outcome_value,
            outcome_value,
            prediction_id,
        ),
    )
    await db.commit()
    return cursor.rowcount > 0
