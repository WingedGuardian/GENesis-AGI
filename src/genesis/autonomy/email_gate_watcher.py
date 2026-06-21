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
                "Held email %s orphaned (approval missing) — expired", row["id"],
            )
            resolved += 1
            continue

        status = approval.get("status")
        if status == "approved":
            # Deliver FIRST (verbatim, below the gate). Only on success do we
            # mark sent/consumed — a transient failure leaves the row held for
            # the next cycle (the drain owns resume retries).
            result = await pipeline.deliver_approved(
                row, subject=_subject(approval.get("context")),
            )
            if result.status == OutreachStatus.DELIVERED:
                await approval_crud.mark_consumed(db, row["request_id"], consumed_at=now)
                await pes.mark_sent(db, row["id"], sent_at=now)
                await cg.record_success(
                    db, domain=row["cell_domain"], verb=row["cell_verb"],
                    risk_class=row["cell_risk_class"], updated_at=now,
                )
                resolved += 1
                logger.info(
                    "Resolved held email %s → sent to %s",
                    row["id"], row["validated_recipient"],
                )
            else:
                logger.warning(
                    "Held email %s delivery failed (%s) — retry next cycle",
                    row["id"], result.status.value,
                )
        elif status in ("rejected", "cancelled"):
            if await pes.mark_rejected(db, row["id"], rejected_at=now):
                await cg.record_correction(
                    db, domain=row["cell_domain"], verb=row["cell_verb"],
                    risk_class=row["cell_risk_class"], updated_at=now,
                )
                resolved += 1
        elif status == "expired":
            # No-decision (the owner never answered) — expire the hold but do
            # NOT record a correction; expiry is not a competence signal.
            if await pes.mark_rejected(db, row["id"], rejected_at=now, expired=True):
                resolved += 1
        # status == 'pending' → still awaiting the owner; leave held.

    return resolved
