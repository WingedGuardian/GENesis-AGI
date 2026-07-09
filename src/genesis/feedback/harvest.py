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

Note: this module is a read-only ETL pipeline — its per-source ``SELECT``s are
intentional raw SQL (dynamic, filtered reads that don't map to a standard crud
API), NOT a crud-layer violation. The only write path is ``record_outcome`` via
``genesis.feedback.bus``; the authoritative source tables are never mutated.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.db.crud import ego as ego_crud
from genesis.feedback.bus import SignalType, record_outcome

logger = logging.getLogger(__name__)

BACKFILL_MARKER = "outcome_backfill_done"
# Bumped whenever a NEW harvest source is added AFTER installs have already set
# the marker — forces a one-time re-backfill so the new source's history lands.
# The stored value is "v{N}:{iso}"; a bare ISO (no "v" prefix) is the legacy v1
# stamp (proposals + outreach only). Re-running is safe: every source is
# idempotent on the (source, ref_type, ref_id, signal_type) unique key, so
# already-harvested rows are INSERT-OR-IGNORE no-ops (read I/O, no WAL write).
# v2 (2026-06-29): added the surplus_tasks source.
# NO bump for the surplus verified-correctness signal (2026-06-30): the new
# VERIFICATION_FAILED rows only exist for tasks completed AFTER deploy (legacy
# rows have NULL outcome_quality and so can never be hollow). There is nothing
# historical to re-backfill, and new hollow tasks are picked up by the normal
# incremental window — a forced full re-backfill would be pure wasted I/O.
BACKFILL_VERSION = 2

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
# Positive keys must stay in sync with genesis.outreach.types
# .POSITIVE_ENGAGEMENT_OUTCOMES ('engaged' is written by the dashboard /engage
# endpoint and was previously dropped here, losing that positive signal).
_OUTREACH_MAP: dict[str, tuple[str, str, float | None]] = {
    "useful":       ("outreach_reply", "positive", 1.0),
    "engaged":      ("outreach_reply", "positive", 1.0),
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


def _marker_version(value: str | None) -> int:
    """Parse the backfill schema-version from a stored marker value.

    The current stamp is ``"v{N}:{iso}"``; a bare ISO timestamp (no ``v``
    prefix) is the legacy v1 stamp. ISO dates start with ``"2"``, so there is no
    ambiguity with the ``"v"`` prefix. ``None`` (never backfilled) → 0 so the
    guard runs the first backfill.
    """
    if not value:
        return 0
    if value.startswith("v") and ":" in value:
        try:
            n = int(value.split(":", 1)[0][1:])
        except ValueError:
            return 1
        # Clamp malformed sub-v1 values (e.g. a hand-edited "v0:"/"v-1:") to the
        # legacy fallback so a corrupt marker re-runs once, not forever.
        return n if n >= 1 else 1
    return 1  # legacy bare-ISO stamp = v1


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
            "surplus": await self._safe(self._harvest_surplus_tasks, cutoff),
        }

    async def run_backfill(self) -> dict:
        """One-shot harvest of ALL history, guarded so it runs exactly once.

        The guard marker is set ONLY when every source completed without an
        exception — so a transient failure (e.g. a startup DB race) cannot
        permanently lock the backfill gate and silently lose the historical
        rows. A clean run over genuinely-empty tables (0 rows, no error) DOES
        set the marker — there is nothing to backfill.
        """
        stored = await ego_crud.get_state(self._db, BACKFILL_MARKER)
        if stored and _marker_version(stored) >= BACKFILL_VERSION:
            return {"skipped": True}

        failures = 0
        counts: dict[str, int] = {}
        # NB: surplus goes in THIS loop (which increments ``failures`` on error),
        # NOT via ``_safe`` — ``_safe`` swallows the exception, which would let
        # the marker be set despite a surplus failure and permanently lose its
        # history. The marker is set ONLY when failures == 0 (see below).
        for name, fn in (
            ("proposals", self._harvest_proposals),
            ("outreach", self._harvest_outreach),
            ("surplus", self._harvest_surplus_tasks),
        ):
            try:
                counts[name] = await fn(None)
            except Exception:
                logger.exception("Outcome backfill: source %r failed", name)
                counts[name] = 0
                failures += 1

        result = {"skipped": False, **counts}
        if failures == 0:
            await ego_crud.set_state(
                self._db,
                key=BACKFILL_MARKER,
                value=f"v{BACKFILL_VERSION}:{datetime.now(UTC).isoformat()}",
            )
            logger.info("Outcome backfill complete: %s", result)
        else:
            result["incomplete"] = True
            logger.warning(
                "Outcome backfill incomplete (%d source failure(s)) — marker NOT "
                "set; will retry next tick. %s", failures, result,
            )
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
                # user_response on an approved (not-yet-executed) proposal is an
                # optional approval note — execution suffixes only appear once a
                # proposal is `executed` (handled above).
                eid = await record_outcome(
                    self._db, **common, signal_type=SignalType.USER_DECISION,
                    polarity="positive", reason_text=r["user_response"],
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
            elif status == "expired":
                # The user let the proposal time out. Per the signal-quality
                # design a timeout is NOT disapproval — coverage only (T3),
                # kept out of any quality denominator.
                eid = await record_outcome(
                    self._db, **common, signal_type=SignalType.LIFECYCLE_EXPIRED,
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
            "SELECT id, engagement_outcome, engagement_signal, user_response, category, "
            "       prediction_error, delivered_at, created_at "
            "FROM outreach_history "
            "WHERE engagement_outcome IS NOT NULL AND trim(engagement_outcome) != ''"
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
            outcome = (r["engagement_outcome"] or "").strip()
            # No-reply (24h timeout) carries no value signal — silence is not a
            # negative (WS-0: "ignored" != no value). An explicit dismissal via
            # the outreach_engagement tool has a non-'timeout' signal and still
            # maps through _OUTREACH_MAP below.
            if outcome == "ignored" and (r.get("engagement_signal") or "").strip() == "timeout":
                continue
            mapping = _OUTREACH_MAP.get(outcome)
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

    async def _harvest_surplus_tasks(self, cutoff: str | None) -> int:
        """surplus_tasks → execution_outcome (T1 ground truth).

        Background autonomous work (reapers, audits, indexing, …) is the richest
        execution-outcome silo Genesis has: a terminal ``status`` of
        ``completed``/``failed`` is a real "did the work run to completion"
        signal. ``cutoff`` (ISO) limits to recent rows; ``None`` = all history.

        Two orthogonal signals per task: EXECUTION_OUTCOME ("did the work run")
        and — for insight-producing types whose output was hollow — an additional
        VERIFICATION_FAILED ("was the output useful"). A hollow task therefore
        yields BOTH a positive (it ran) and a negative (it produced nothing),
        netting ~0.5 in ``aggregate_by_domain`` — below a useful task (1.0) and
        above a hard failure (0.0). See ``surplus.types.INSIGHT_PRODUCING_TASK_TYPES``
        and ``surplus_tasks.outcome_quality``.

        Maintainer notes (deliberate asymmetries):
        - ``cancelled`` (and non-terminal pending/running) tasks are NOT
          recorded — a cancel is a lifecycle event, not an execution outcome.
          Unlike tabled/withdrawn proposals (which get LIFECYCLE_* rows), surplus
          has no lifecycle signal type and a cancel carries no quality info.
        - ``stated_confidence`` is left NULL: surplus tasks carry no
          pre-execution confidence, so they are CORRECTLY excluded from
          ``calibration_pairs`` (which requires a non-NULL confidence). Do NOT
          add a synthetic value to "fill" it.
        - ``domain`` = ``task_type``. ``aggregate_by_domain`` groups by ``domain``
          across all sources, so a surplus ``task_type`` shares that namespace
          with ego ``action_type``; no collision exists today, and any future
          one is read-only observability noise (``calibration_pairs`` is
          source-filtered, so ego calibration never mixes in surplus rows).
        """
        sql = (
            "SELECT id, task_type, status, failure_reason, completed_at, "
            "       created_at, outcome_quality "
            "FROM surplus_tasks WHERE status IN ('completed', 'failed')"
        )
        params: list = []
        if cutoff is not None:
            sql += " AND COALESCE(completed_at, created_at) >= ?"
            params.append(cutoff)

        cur = await self._db.execute(sql, params)
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]

        inserted = 0
        for raw in rows:
            r = dict(zip(cols, raw, strict=False))
            completed = r["status"] == "completed"
            eid = await record_outcome(
                self._db,
                source="surplus",
                ref_type="task",
                ref_id=r["id"],
                domain=r["task_type"],
                signal_type=SignalType.EXECUTION_OUTCOME,
                polarity="positive" if completed else "negative",
                value=1.0 if completed else 0.0,
                reason_text=None if completed else r["failure_reason"],
                occurred_at=r["completed_at"] or r["created_at"],
                harvested_from="surplus_tasks",
            )
            if eid:
                inserted += 1

            # Verified-correctness (second, orthogonal axis). EXECUTION_OUTCOME
            # above answers "did it run"; this answers "was the output useful".
            # A completed insight task whose output the measurement-only quality
            # judge FAILED (score below the output_quality threshold) is hollow —
            # it ran but produced nothing of value — so it earns an ADDITIONAL
            # tier-1 negative. Because VERIFICATION_FAILED is a distinct
            # signal_type, it coexists with the positive EXECUTION_OUTCOME on the
            # same (source, ref_type, ref_id) under the unique key; the two never
            # dup-suppress each other across incremental re-harvests. NULL
            # outcome_quality (action tasks, legacy rows, judge outages, unknown
            # types, empty/too-short output) adds nothing — positive-only, unchanged.
            if completed and r["outcome_quality"] == "hollow":
                veid = await record_outcome(
                    self._db,
                    source="surplus",
                    ref_type="task",
                    ref_id=r["id"],
                    domain=r["task_type"],
                    signal_type=SignalType.VERIFICATION_FAILED,
                    polarity="negative",
                    value=0.0,
                    reason_text="quality judge scored output below threshold (hollow)",
                    occurred_at=r["completed_at"] or r["created_at"],
                    harvested_from="surplus_tasks",
                )
                if veid:
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
