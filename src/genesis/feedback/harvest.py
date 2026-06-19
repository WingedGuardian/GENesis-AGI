"""feedback/harvest.py — fold existing siloed signals into the Outcome Bus.

The ``OutcomeHarvester`` reads Genesis's authoritative outcome tables and records
them as structured ``outcome_events`` via the fire-and-forget bus. It NEVER
mutates the source tables or touches their writers/consumers — it only reads.

Two entry points:
- ``run()`` — scheduled, incremental: re-scan a recent window each tick.
  Idempotent on the unique key, so re-scanning is a cheap no-op.
- ``run_backfill()`` — one-shot over ALL history, guarded by an ``ego_state``
  marker so it executes exactly once. This is what closes the historical gap
  (e.g. the ~63 executed/failed proposal outcomes currently buried as a string
  suffix on ``ego_proposals.user_response``, captured for ~1.6% today).

Source authority (no double-counting):
- **ego_proposals** — the source of truth for proposal lifecycle AND the buried
  T1 execution outcome. ``intervention_journal`` is a redundant echo of the same
  proposals, so it is intentionally NOT harvested here (the unique key would
  dedupe it anyway, but skipping the redundant read is cleaner).
- **outreach_history** — engagement outcomes.
- ``ego_cycle_outcomes`` is process telemetry (cycle assessments / costs), not a
  per-action outcome signal, so it is captured as attribution metadata
  (``cycle_id``) on proposal events rather than as standalone rows.

Each source read is wrapped independently (``_safe``) so one bad source can never
abort the others.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.db.crud import ego as ego_crud
from genesis.feedback.bus import SignalType, record_outcome

logger = logging.getLogger(__name__)

BACKFILL_MARKER = "outcome_backfill_done"

# Markers update_proposal_outcome appends to ego_proposals.user_response.
# Two forms exist: "<session_id>|completed:<summary>" and a bare
# "|completed:<summary>" (when the dispatch-record step was skipped). ``find``
# handles both regardless of any prefix.
_SUFFIX_MAP = (
    ("|completed:", "positive", 1.0),
    ("|failed:", "negative", 0.0),
)


# engagement_outcome → (signal_type, polarity, value). The live vocabulary is
# richer than the schema CHECK (which lists only useful/not_useful/ambivalent/
# ignored): prod also carries 'acted_on' and 'acknowledged' (positive behavioural
# signals) — a pre-existing outreach schema/data drift, tracked separately. We map
# what actually exists; explicit > implicit-negative defaulting (which would
# mislabel acted_on/acknowledged and the empty-string rows).
_OUTREACH_MAP: dict[str, tuple[str, str, float | None]] = {
    "useful":       ("outreach_reply", "positive", 1.0),
    "acted_on":     ("outreach_reply", "positive", 1.0),
    "acknowledged": ("outreach_reply", "positive", 0.5),
    "not_useful":   ("outreach_reply", "negative", 0.0),
    "ambivalent":   ("outreach_implicit", "neutral", None),
    "ignored":      ("outreach_implicit", "negative", 0.0),
}


def _parse_execution_suffix(user_response: str | None) -> tuple[str, float, str] | None:
    """Extract the buried T1 outcome from ``user_response``.

    Returns ``(polarity, value, summary)`` if a completion/failure suffix is
    present, else ``None`` (the field holds a user rationale, a bare session id,
    or nothing).
    """
    if not user_response:
        return None
    for marker, polarity, value in _SUFFIX_MAP:
        idx = user_response.find(marker)
        if idx != -1:
            return polarity, value, user_response[idx + len(marker):][:1000]
    return None


class OutcomeHarvester:
    """Idempotently folds existing outcome signals into ``outcome_events``."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    # -- public entry points ------------------------------------------------ #

    async def run(self, *, window_days: int = 2) -> dict:
        """Incremental harvest of a recent window (scheduled cadence)."""
        cutoff = (datetime.now(UTC) - timedelta(days=window_days)).isoformat()
        return {
            "proposals": await self._safe(self._harvest_proposals, cutoff),
            "outreach": await self._safe(self._harvest_outreach, cutoff),
        }

    async def run_backfill(self) -> dict:
        """One-shot harvest of ALL history, guarded so it runs exactly once."""
        if await ego_crud.get_state(self._db, BACKFILL_MARKER):
            return {"skipped": True}
        result = {
            "skipped": False,
            "proposals": await self._safe(self._harvest_proposals, None),
            "outreach": await self._safe(self._harvest_outreach, None),
        }
        await ego_crud.set_state(
            self._db, key=BACKFILL_MARKER, value=datetime.now(UTC).isoformat()
        )
        logger.info("Outcome backfill complete: %s", result)
        return result

    # -- per-source harvesters --------------------------------------------- #

    async def _harvest_proposals(self, cutoff: str | None) -> int:
        """ego_proposals → execution_outcome (T1) / user_decision (T2-3) /
        lifecycle (T3). ``cutoff`` (ISO) limits to recent rows; ``None`` = all."""
        sql = (
            "SELECT id, status, user_response, confidence, action_type, "
            "       cycle_id, created_at, resolved_at "
            "FROM ego_proposals WHERE status != 'pending'"
        )
        params: list = []
        if cutoff is not None:
            sql += " AND COALESCE(resolved_at, created_at) >= ?"
            params.append(cutoff)

        cur = await self._db.execute(sql, params)
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]

        inserted = 0
        for raw in rows:
            r = dict(zip(cols, raw, strict=False))
            status = r["status"]
            occurred_at = r["resolved_at"] or r["created_at"]
            metadata = {"cycle_id": r["cycle_id"]} if r["cycle_id"] else None
            common = dict(
                source="ego",
                ref_type="proposal",
                ref_id=r["id"],
                domain=r["action_type"],
                stated_confidence=r["confidence"],
                occurred_at=occurred_at,
                harvested_from="ego_proposals",
                metadata=metadata,
            )
            suffix = _parse_execution_suffix(r["user_response"])

            if status == "executed" and suffix is not None:
                polarity, value, summary = suffix
                eid = await record_outcome(
                    self._db, **common, signal_type=SignalType.EXECUTION_OUTCOME,
                    polarity=polarity, value=value, reason_text=summary,
                )
            elif status == "executed":
                # Dispatched, but no outcome recorded yet → coverage only.
                eid = await record_outcome(
                    self._db, **common, signal_type=SignalType.DISPATCH,
                    polarity="neutral",
                )
            elif status == "failed":
                eid = await record_outcome(
                    self._db, **common, signal_type=SignalType.EXECUTION_OUTCOME,
                    polarity="negative", value=0.0, reason_text="dispatch failed",
                )
            elif status == "rejected":
                eid = await record_outcome(
                    self._db, **common, signal_type=SignalType.USER_DECISION,
                    polarity="negative", reason_text=r["user_response"],
                )
            elif status == "approved":
                reason = None if suffix else r["user_response"]
                eid = await record_outcome(
                    self._db, **common, signal_type=SignalType.USER_DECISION,
                    polarity="positive", reason_text=reason,
                )
            elif status == "tabled":
                eid = await record_outcome(
                    self._db, **common, signal_type=SignalType.LIFECYCLE_TABLED,
                    polarity="neutral",
                )
            elif status == "withdrawn":
                eid = await record_outcome(
                    self._db, **common, signal_type=SignalType.LIFECYCLE_WITHDRAWN,
                    polarity="neutral",
                )
            else:
                continue  # unknown/non-terminal status — skip

            if eid:
                inserted += 1
        return inserted

    async def _harvest_outreach(self, cutoff: str | None) -> int:
        """outreach_history → outreach_reply (T2) / outreach_implicit (T3)."""
        sql = (
            "SELECT id, engagement_outcome, user_response, category, "
            "       prediction_error, delivered_at, created_at "
            "FROM outreach_history "
            "WHERE engagement_outcome IS NOT NULL AND engagement_outcome != ''"
        )
        params: list = []
        if cutoff is not None:
            sql += " AND COALESCE(delivered_at, created_at) >= ?"
            params.append(cutoff)

        cur = await self._db.execute(sql, params)
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]

        inserted = 0
        for raw in rows:
            r = dict(zip(cols, raw, strict=False))
            mapping = _OUTREACH_MAP.get((r["engagement_outcome"] or "").strip())
            if mapping is None:
                continue  # empty / unknown engagement value — no real signal
            signal_type, polarity, value = mapping
            eid = await record_outcome(
                self._db,
                source="outreach",
                ref_type="outreach",
                ref_id=r["id"],
                domain=r["category"],
                prediction_error=r["prediction_error"],
                occurred_at=r["delivered_at"] or r["created_at"],
                harvested_from="outreach_history",
                signal_type=signal_type,
                polarity=polarity,
                value=value,
                reason_text=r["user_response"],
            )
            if eid:
                inserted += 1
        return inserted

    # -- helper ------------------------------------------------------------- #

    async def _safe(self, fn, cutoff: str | None) -> int:
        """Run one source harvest; isolate its failure from the others."""
        try:
            return await fn(cutoff)
        except Exception:
            logger.debug("OutcomeHarvester: source %s failed", fn.__name__, exc_info=True)
            return 0
