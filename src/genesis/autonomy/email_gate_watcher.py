"""WS-8 email gate resolution watcher — drains held sends.

The correctness guarantee for the email autonomy gate: a periodic drain
(``CronTrigger`` */5min, ``max_instances=1``) that resolves each
``pending_email_sends`` row against its linked approval:

- **approved**  → send below the gate (``pipeline.deliver_approved``,
  ``gate_cleared``) + ``record_success`` on the cell + mark sent/consumed.
- **rejected/cancelled** → mark rejected + ``record_correction`` (an explicit
  no is competence-negative).
- **expired** → mark expired, NO correction (no-decision ≠ rejection).
- **orphaned** (approval row gone) → expire, never send.
- **pending** → still waiting; left held.

Single-threaded (``max_instances=1``) so there are no in-drain races.
Deliver-first ordering makes an approved send **at-least-once**: a crash between
a successful ``adapter.send`` and ``mark_sent`` re-delivers next cycle (rare).
A transient delivery failure leaves the row held and is retried next cycle —
``deliver_approved`` sets ``gate_cleared`` and ``_deliver`` skips ``_defer`` for
gate-cleared sends, so the drain is the SOLE retry owner (no re-gate loop).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from genesis.db.crud import approval_requests as approval_crud
from genesis.db.crud import capability_grants as cg
from genesis.db.crud import pending_email_sends as pes
from genesis.outreach.types import OutreachStatus

logger = logging.getLogger(__name__)


async def _bulk_recipient_authorized(db: object, recipient: str | None) -> bool:
    """True iff ``recipient`` is a CURATED, non-opted-out ``marketing_prospects``
    row — the EXACT predicate the email gate's g2 BULK scope guard applies
    (``email_gate.py`` ``_scope_guard_trip``). Fail-closed: a blank / not-curated
    / opted-out recipient is NOT authorized. Status (active/contacted/replied) is
    deliberately DECOUPLED — only opt-out revokes authorization, matching g2."""
    if not recipient:
        return False
    from genesis.db.crud import marketing_prospects as mp

    row = await mp.get_by_email(db, recipient)
    if row is None:
        return False
    return not row.get("opted_out")


async def _recipient_opted_out(db: object, recipient: str | None) -> bool:
    """True iff ``recipient`` matches a ``marketing_prospects`` row that has OPTED
    OUT — a NARROW predicate that applies to a held send of ANY capability class.

    Distinct from ``_bulk_recipient_authorized`` (which fail-CLOSES on an
    *uncurated* recipient): this returns True ONLY for a KNOWN, opted-out prospect,
    so it never blocks a legitimate non-marketing send to someone who simply isn't
    in the prospect store. Blank recipient / no row / not-opted-out → False (nothing
    to refuse)."""
    if not recipient:
        return False
    from genesis.db.crud import marketing_prospects as mp

    row = await mp.get_by_email(db, recipient)
    if row is None:
        return False
    return bool(row.get("opted_out"))


def _subject(context: str | None) -> str:
    if not context:
        return ""
    try:
        return json.loads(context).get("subject", "") or ""
    except (ValueError, TypeError):
        return ""


async def drain_pending_email_sends(rt: object) -> int:
    """Resolve all held email sends. Returns the number of rows resolved."""
    db = getattr(rt, "_db", None)
    pipeline = getattr(rt, "_outreach_pipeline", None)
    if db is None or pipeline is None:
        return 0

    resolved = 0
    for row in await pes.list_held(db):
        now = datetime.now(UTC).isoformat()
        approval = await approval_crud.get_by_id(db, row["request_id"])

        if approval is None:
            await pes.mark_rejected(db, row["id"], rejected_at=now, expired=True)
            logger.warning(
                "Held email %s orphaned (approval missing) — expired",
                row["id"],
            )
            resolved += 1
            continue

        status = approval.get("status")
        if status == "approved":
            if approval.get("consumed_at") is not None:
                # The approval was already consumed — a prior cycle delivered it
                # but crashed before marking the hold 'sent'. Reconcile WITHOUT
                # re-sending: this narrows the at-least-once window to the gap
                # between adapter.send and mark_consumed.
                await pes.mark_sent(db, row["id"], sent_at=now)
                resolved += 1
                logger.info(
                    "Reconciled held email %s (approval already consumed) — not re-sent",
                    row["id"],
                )
                continue
            # Absolute opt-out re-check (ALL classes). A marketing pitch whose body
            # trips the FINANCIAL money-pattern classifier lands as a FINANCIAL (not
            # BULK) hold, so the bulk-gated re-check just below would skip it — yet an
            # opted-out prospect must NEVER receive an autonomous send regardless of
            # how the pitch classified. Refuse any held send to a KNOWN opted-out
            # prospect, deliver-side, fail-safe. Narrow by construction: only a
            # curated-and-opted-out row trips this, so a genuine non-marketing send to
            # a non-prospect is untouched. Accepted residue (STOPGAP, tracked in the
            # deferred dedup/provenance PR2, follow-up 17261bc9): a held STANDARD reply
            # to someone who is ALSO an opted-out marketing prospect is refused too —
            # rare (granted-cell replies auto-allow and never reach this branch) and
            # fail-safe. The durable fix is a send-provenance flag separating a
            # marketing send from an ordinary reply.
            if await _recipient_opted_out(db, row["validated_recipient"]):
                if await pes.mark_rejected(db, row["id"], rejected_at=now):
                    await approval_crud.mark_consumed(db, row["request_id"], consumed_at=now)
                    resolved += 1
                logger.warning(
                    "Held email %s recipient is an opted-out marketing prospect — "
                    "not delivered, marked rejected (class=%s)",
                    row["id"],
                    row["cell_risk_class"],
                )
                continue
            # Opt-out re-check at approved-send time (BULK / cold-marketing only).
            # deliver_approved runs below the gate (gate_cleared=True), which
            # bypasses the g2 BULK scope guard — so a prospect who opted out AFTER
            # the hold was enqueued but BEFORE the owner approved would otherwise
            # still be delivered. Re-apply g2's EXACT predicate (curated,
            # non-opted-out marketing_prospects row) immediately before delivery;
            # fail-closed. Non-BULK rows are untouched (behavior preserved).
            if row["cell_risk_class"] == "bulk" and not await _bulk_recipient_authorized(
                db, row["validated_recipient"]
            ):
                # Not owner rejection — a deterministic guard (like the IGNORED
                # terminal path): mark the hold rejected + consume the approval so
                # it can't busy-loop or linger as a ghost approved/unconsumed row.
                # No correction (a guard trip is not owner competence signal).
                if await pes.mark_rejected(db, row["id"], rejected_at=now):
                    await approval_crud.mark_consumed(db, row["request_id"], consumed_at=now)
                    resolved += 1
                logger.warning(
                    "Held BULK email %s recipient no longer authorized "
                    "(opted-out/uncurated) — not delivered, marked rejected",
                    row["id"],
                )
                continue
            # Deliver FIRST (verbatim, below the gate). Only on success do we
            # mark sent/consumed — a transient failure leaves the row held for
            # the next cycle (the drain owns resume retries).
            result = await pipeline.deliver_approved(
                row,
                subject=_subject(approval.get("context")),
            )
            if result.status == OutreachStatus.DELIVERED:
                await approval_crud.mark_consumed(db, row["request_id"], consumed_at=now)
                await pes.mark_sent(db, row["id"], sent_at=now)
                await cg.record_success(
                    db,
                    domain=row["cell_domain"],
                    verb=row["cell_verb"],
                    risk_class=row["cell_risk_class"],
                    updated_at=now,
                    # WS-3 gate-3: the OWNER approved this send — owner evidence.
                    origin_class="owner",
                )
                resolved += 1
                logger.info(
                    "Resolved held email %s → sent to %s",
                    row["id"],
                    row["validated_recipient"],
                )
            elif result.status == OutreachStatus.IGNORED:
                # The pipeline terminally SKIPPED this approved send (self-
                # addressed / no recipient). It is not deliverable and not an
                # error — mark the hold rejected so it can't busy-loop every
                # cycle, and consume the approval so it doesn't linger as a ghost
                # approved/unconsumed row. No correction (a guard, not owner intent).
                if await pes.mark_rejected(db, row["id"], rejected_at=now):
                    await approval_crud.mark_consumed(
                        db,
                        row["request_id"],
                        consumed_at=now,
                    )
                    resolved += 1
                logger.warning(
                    "Held email %s skipped by pipeline (IGNORED) — marked terminal",
                    row["id"],
                )
            else:
                logger.warning(
                    "Held email %s delivery failed (%s) — retry next cycle",
                    row["id"],
                    result.status.value,
                )
        elif status in ("rejected", "cancelled"):
            if await pes.mark_rejected(db, row["id"], rejected_at=now):
                await cg.record_correction(
                    db,
                    domain=row["cell_domain"],
                    verb=row["cell_verb"],
                    risk_class=row["cell_risk_class"],
                    updated_at=now,
                    # WS-3 gate-3: the OWNER rejected/cancelled — owner decision.
                    origin_class="owner",
                )
                resolved += 1
        elif status == "expired":
            # No-decision (the owner never answered) — expire the hold but do
            # NOT record a correction; expiry is not a competence signal.
            if await pes.mark_rejected(db, row["id"], rejected_at=now, expired=True):
                resolved += 1
        # status == 'pending' → still awaiting the owner; leave held.

    return resolved
