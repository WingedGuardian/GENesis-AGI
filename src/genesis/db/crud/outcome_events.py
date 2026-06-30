"""CRUD for ``outcome_events`` — the Self-Improvement Outcome Bus ledger.

Thin, strict data layer. ``record`` raises ``ValueError`` on malformed input
(invalid tier/class/polarity, empty key fields) so bugs surface in tests; the
fire-and-forget leniency that protects production paths lives one layer up in
``feedback/bus.py``. Writes are idempotent on the unique key
``(source, ref_type, ref_id, signal_type)``.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import aiosqlite

logger = logging.getLogger(__name__)

# Enum domains enforced by the table CHECK constraints — validated here too so
# we raise a clear ValueError instead of relying on INSERT OR IGNORE silently
# swallowing a CHECK violation (OR IGNORE skips BOTH unique AND check failures).
_VALID_TIERS = frozenset({1, 2, 3})
_VALID_CLASSES = frozenset({"implicit", "explicit"})
_VALID_POLARITIES = frozenset({"positive", "negative", "neutral"})

_COLUMNS = (
    "id", "source", "ref_type", "ref_id", "domain", "signal_type",
    "signal_class", "signal_tier", "polarity", "value", "stated_confidence",
    "prediction_error", "reason", "reason_text", "metadata", "harvested_from",
    "occurred_at",
)


def _rows_to_dicts(cur: aiosqlite.Cursor, rows: list) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=False)) for r in rows]


async def record(
    db: aiosqlite.Connection,
    *,
    source: str,
    ref_type: str,
    ref_id: str,
    signal_type: str,
    signal_tier: int,
    domain: str | None = None,
    signal_class: str = "implicit",
    polarity: str | None = None,
    value: float | None = None,
    stated_confidence: float | None = None,
    prediction_error: float | None = None,
    reason: str | None = None,
    reason_text: str | None = None,
    metadata: dict | None = None,
    harvested_from: str | None = None,
    occurred_at: str | None = None,
) -> str | None:
    """Insert one outcome event, idempotently.

    Returns the new row id if inserted, or ``None`` if a row with the same
    ``(source, ref_type, ref_id, signal_type)`` already exists (OR IGNORE).

    Raises ``ValueError`` on invalid enum values or empty key fields.
    """
    if not source or not ref_type or not ref_id or not signal_type:
        raise ValueError(
            "outcome_events.record: source/ref_type/ref_id/signal_type are required"
        )
    if signal_tier not in _VALID_TIERS:
        raise ValueError(f"signal_tier must be one of {sorted(_VALID_TIERS)}, got {signal_tier!r}")
    if signal_class not in _VALID_CLASSES:
        raise ValueError(f"signal_class must be one of {sorted(_VALID_CLASSES)}, got {signal_class!r}")
    if polarity is not None and polarity not in _VALID_POLARITIES:
        raise ValueError(f"polarity must be None or one of {sorted(_VALID_POLARITIES)}, got {polarity!r}")

    eid = uuid.uuid4().hex[:16]
    occurred_at = occurred_at or datetime.now(UTC).isoformat()
    meta_json = json.dumps(metadata) if metadata is not None else None

    cur = await db.execute(
        """INSERT OR IGNORE INTO outcome_events
               (id, source, ref_type, ref_id, domain, signal_type, signal_class,
                signal_tier, polarity, value, stated_confidence, prediction_error,
                reason, reason_text, metadata, harvested_from, occurred_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            eid, source, ref_type, ref_id, domain, signal_type, signal_class,
            signal_tier, polarity, value, stated_confidence, prediction_error,
            reason, reason_text, meta_json, harvested_from, occurred_at,
        ),
    )
    await db.commit()
    return eid if cur.rowcount > 0 else None


async def exists(
    db: aiosqlite.Connection,
    *,
    source: str,
    ref_type: str,
    ref_id: str,
    signal_type: str,
) -> bool:
    """True if an event with this unique key already exists."""
    cur = await db.execute(
        "SELECT 1 FROM outcome_events "
        "WHERE source = ? AND ref_type = ? AND ref_id = ? AND signal_type = ? LIMIT 1",
        (source, ref_type, ref_id, signal_type),
    )
    return await cur.fetchone() is not None


async def count(db: aiosqlite.Connection) -> int:
    """Total number of recorded outcome events."""
    cur = await db.execute("SELECT COUNT(*) FROM outcome_events")
    row = await cur.fetchone()
    return row[0] if row else 0


async def count_by_signal_type(db: aiosqlite.Connection) -> dict[str, int]:
    """Coverage accounting: row counts grouped by signal_type."""
    cur = await db.execute(
        "SELECT signal_type, COUNT(*) FROM outcome_events GROUP BY signal_type"
    )
    rows = await cur.fetchall()
    return {r[0]: r[1] for r in rows}


async def count_by_tier(db: aiosqlite.Connection) -> dict[int, int]:
    """Coverage accounting: row counts grouped by signal_tier (1/2/3).

    signal_type does NOT map 1:1 to tier (``user_decision`` is T2 when it
    carries a rationale, else T3), so the tier breakdown cannot be derived
    from ``count_by_signal_type`` — it needs its own GROUP BY.
    """
    cur = await db.execute(
        "SELECT signal_tier, COUNT(*) FROM outcome_events GROUP BY signal_tier"
    )
    rows = await cur.fetchall()
    return {r[0]: r[1] for r in rows}


async def aggregate_by_domain(
    db: aiosqlite.Connection,
    *,
    days: int | None = 30,
    tier: int | None = None,
    source: str | None = None,
) -> list[dict]:
    """Per-domain outcome rollup.

    ``days`` bounds the window by ``occurred_at``; pass ``None`` for an
    all-time rollup (so the per-domain breakdown reconciles with the lifetime
    ``count_by_tier``/``count_by_signal_type`` totals). ``tier`` optionally
    restricts to one signal tier (e.g. tier=1 for the ground-truth view that
    downstream quality scoring should weight highest). ``source`` optionally
    restricts to a single producer (e.g. ``source='surplus'``) so a consumer can
    fold in one clean domain without double-counting producers it already
    aggregates by other means.

    Note on surplus "hollow" tasks: an insight task that ran but produced nothing
    useful emits TWO tier-1 rows — a positive EXECUTION_OUTCOME ("it ran") and a
    negative VERIFICATION_FAILED ("output was useless") — so it contributes ``n=2``
    (one positive, one negative) to its domain. For the ``source='surplus'`` feed,
    every ``value`` is 0.0/1.0 and aligned with ``polarity``, so ``avg_value`` and
    ``positive/n`` are IDENTICAL here; both already fold in the hollow penalty.
    Ordering still holds — a domain of U useful / H hollow / F failed scores
    ``(U+H)/(U+2H+F)``, i.e. all-useful=1.0 > hollow-mixed > all-failed=0.0. The
    n=2 weighting makes a hollow task count as ~1/3 effective credit, not 1/2; a
    consumer that needs exact per-task semantics must dedup by ``ref_id``.
    """
    sql = """
        SELECT domain,
               COUNT(*)                                                   AS n,
               SUM(CASE WHEN polarity = 'positive' THEN 1 ELSE 0 END)     AS positive,
               SUM(CASE WHEN polarity = 'negative' THEN 1 ELSE 0 END)     AS negative,
               AVG(value)                                                 AS avg_value,
               AVG(stated_confidence)                                     AS avg_confidence,
               AVG(prediction_error)                                      AS avg_prediction_error
        FROM outcome_events
    """
    clauses: list[str] = []
    params: list = []
    if days is not None:
        clauses.append("occurred_at >= ?")
        params.append((datetime.now(UTC) - timedelta(days=days)).isoformat())
    if tier is not None:
        clauses.append("signal_tier = ?")
        params.append(tier)
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " GROUP BY domain ORDER BY n DESC"
    cur = await db.execute(sql, params)
    rows = await cur.fetchall()
    return _rows_to_dicts(cur, rows)


async def calibration_by_domain(
    db: aiosqlite.Connection,
    *,
    days: int = 90,
    tier: int = 1,
) -> list[dict]:
    """Rows usable for calibration grouped/labelled by their per-row ``domain``,
    across ALL sources, within ``tier`` (ground truth by default).

    WARNING: does NOT filter by ``source``. Do NOT use this for ego calibration —
    it would mix ego rows with outreach/triage and contaminate the ECE, and the
    per-row ``domain`` is the action_type (e.g. 'dispatch'), not 'ego'. For an
    aggregate ego calibration use ``calibration_pairs(source='ego', tier=1)``.
    Retained for cross-source/diagnostic reads.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    cur = await db.execute(
        """
        SELECT domain, stated_confidence, value, polarity, signal_type, occurred_at
        FROM outcome_events
        WHERE signal_tier = ?
          AND stated_confidence IS NOT NULL
          AND value IS NOT NULL
          AND occurred_at >= ?
        ORDER BY domain, occurred_at
        """,
        (tier, cutoff),
    )
    rows = await cur.fetchall()
    return _rows_to_dicts(cur, rows)


async def recent(db: aiosqlite.Connection, *, limit: int = 20) -> list[dict]:
    """Most recent outcome events (debug/verification/display)."""
    cur = await db.execute(
        "SELECT * FROM outcome_events ORDER BY occurred_at DESC LIMIT ?",
        (limit,),
    )
    rows = await cur.fetchall()
    return _rows_to_dicts(cur, rows)


async def calibration_pairs(
    db: aiosqlite.Connection,
    *,
    source: str,
    tier: int = 1,
    days: int = 90,
) -> list[dict]:
    """Confidence/outcome pairs for ONE source at one tier — the calibration
    input set. Filtered by ``source`` so e.g. ego calibration never mixes in
    outreach/triage rows (cross-source contamination would corrupt the ECE).
    Unlike ``calibration_by_domain``, this ignores the per-row ``domain`` so an
    aggregate (e.g. all ego T1 rows) can be computed regardless of action_type.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    cur = await db.execute(
        """
        SELECT stated_confidence, value, occurred_at
        FROM outcome_events
        WHERE source = ?
          AND signal_tier = ?
          AND stated_confidence IS NOT NULL
          AND value IS NOT NULL
          AND occurred_at >= ?
        ORDER BY occurred_at
        """,
        (source, tier, cutoff),
    )
    rows = await cur.fetchall()
    return _rows_to_dicts(cur, rows)
