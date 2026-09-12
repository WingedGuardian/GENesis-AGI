"""Void historical owner-facing outreach predictions polluting reply-rate calibration.

The outreach ledger hook (``on_outreach_delivered``) wrote ``reply_received`` /
``positive_engagement`` predictions for EVERY delivered send, including owner-facing
pings (Telegram/voice — GitHub-monitor pings, career nudges, marketing reply-pings,
bite-relay, approvals, digests, blockers, alerts). Those are delivered to the OWNER,
not an external recipient, so they never get a reply: open rows resolve as silence (0)
and already-resolved rows keep dragging the recomputed calibration toward a ~0% reply
rate (the all-time cell never ages out). MEASURED 2026-09-02 on this install: 1116 of
1162 (96%) ``outreach_send`` predictions were owner-facing telegram.

The code fix (``on_outreach_delivered`` skips owner-facing channels) stops NEW pollution;
this one-time migration clears the historical rows. ``calibration_cells`` are recomputed
each grading pass from ``ledger_predictions.list_resolved`` (``status IN
('resolved','fuzzy_resolved')``), so setting a row's ``status='void'`` drops it out of
calibration automatically at the next recompute — no cell rewrite is needed here.

Channels are hardcoded (``telegram``, ``voice``) so this numbered migration is a frozen,
self-contained snapshot (never coupled to a constant whose membership might later change
— cf. ``OWNER_FACING_CHANNELS``). Guarded ``WHERE status != 'void'`` → idempotent. No
commit — the runner owns the transaction.

Voiding a previously-*resolved* row also CLEARS its ``outcome_value``/``resolver``/``brier``/
``evidence_ref`` so the result matches what the canonical ``ledger_predictions.resolve(...,
status="void")`` path writes and satisfies the ledger invariant "outcome_value is non-None
iff status is resolved" (``grader.py``). A void row carrying a stale grade (or dangling
evidence pointer) is a latent trap for any future reader keyed on ``outcome_value IS NOT
NULL`` alone.

It ALSO invalidates the DERIVED calibration data (``calibration_cells`` +
``calibration_cell_history``) for ``action_class='outreach_send'`` so the correction is
immediate rather than deferred to the next grader recompute (and effective even when the
grader is disabled). See ``up()`` for the rationale (cells can't be un-mixed per-channel, so
they're deleted and rebuilt cleanly by the recompute).

Scope limit (accepted): a prediction whose originating ``outreach_history`` row was already
deleted/pruned cannot be matched by the channel join (its channel is unknowable), so it
would remain — orphans are expected to be few-to-none in practice.
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite


async def _has_table(db: aiosqlite.Connection, name: str) -> bool:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return await cursor.fetchone() is not None


async def up(db: aiosqlite.Connection) -> None:
    # The join needs BOTH tables; in the standalone migration-runner (no
    # create_all_tables) either may be absent — skip cleanly. A real error on a present
    # table still propagates (no suppress).
    if not await _has_table(db, "ledger_predictions") or not await _has_table(
        db, "outreach_history"
    ):
        return
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "UPDATE ledger_predictions "
        "SET status = 'void', resolved_at = COALESCE(resolved_at, ?), "
        "    outcome_value = NULL, resolver = NULL, brier = NULL, evidence_ref = NULL "
        "WHERE action_class = 'outreach_send' "
        "AND status != 'void' "
        "AND subject_ref_id IN ("
        "    SELECT id FROM outreach_history WHERE channel IN ('telegram', 'voice')"
        ")",
        (now,),
    )
    # Invalidate the DERIVED calibration data too, so the correction is IMMEDIATE — not
    # deferred to the next grader recompute, and effective even if the grader is disabled
    # (GENESIS_LEDGER_GRADER_DISABLED=1). The current outreach_send cells become cold-start
    # ("no data") until the grader rebuilds them cleanly from the remaining (non-void)
    # resolved rows — an honest absence beats a stale ~0% reply rate. The cells aggregate
    # by domain (owner + external predictions mix in an outreach.<category> cell), so a cell
    # cannot be surgically un-mixed; deleting the outreach_send cells and letting the full
    # recompute rebuild them is the correct invalidation. The append-only trend snapshots
    # (read only by the calibration-status trend surface, never for current values) are
    # pruned for the same reason. Both guarded for the standalone runner.
    if await _has_table(db, "calibration_cells"):
        await db.execute("DELETE FROM calibration_cells WHERE action_class = 'outreach_send'")
    if await _has_table(db, "calibration_cell_history"):
        await db.execute(
            "DELETE FROM calibration_cell_history WHERE action_class = 'outreach_send'"
        )
