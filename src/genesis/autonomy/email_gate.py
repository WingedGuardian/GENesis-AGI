"""WS-8 email autonomy gate — the deterministic capability check at the
``outreach.pipeline._deliver`` chokepoint (Tenet 0).

For ``channel=="email"`` the pipeline calls :meth:`EmailAutonomyGate.check`
BEFORE ``adapter.send_message``.  The gate decides send/hold in CODE, *below*
the LLM tool call — it is never a permissions prompt the model sees, and it is
unbypassable because every send path (MCP tool, scheduler drain, recovery
retry, ~12 direct callers) converges on ``_deliver``.

On HOLD it records the fully-resolved send to ``pending_email_sends`` and
creates a linked ``approval_requests`` row (approval row FIRST so a crash can't
leave a pending row with no approval), emits ``autonomy.gate_held`` at
``Severity.INFO`` (an EXPECTED autonomy event, inert to the failure scorers),
and returns a hold decision.  A resolution watcher later sends the held email
below the gate on approval, or expires it on reject/timeout — see
``runtime/init`` and ``db/crud/pending_email_sends``.

GRANTED_EPHEMERAL = the basic per-hold approve-and-send: the watcher records a
single ``record_success`` on the cell but does NOT promote it to GRANTED
(promotion is earned via PR-D earn-back or an explicit grant).
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite

from genesis.autonomy.capabilities import InvalidTransition
from genesis.autonomy.classification import classify_email_action
from genesis.autonomy.types import CellEvent, CellState, RiskClass
from genesis.db.crud import capability_grants as cg
from genesis.db.crud import pending_email_sends as pes
from genesis.observability.types import Severity, Subsystem

logger = logging.getLogger(__name__)

#: Distinct action_type so these holds are isolated from CLI-fallback approvals
#: (excluded from ``approve_all_pending`` / the voice pending count).
EMAIL_GATE_ACTION_TYPE = "email_capability_gate"


@dataclass(frozen=True)
class GateDecision:
    """Outcome of an autonomy-gate check. ``allow`` False ⇒ the send was held."""

    allow: bool
    pending_id: str | None = None
    request_id: str | None = None
    reason: str = ""


class EmailAutonomyGate:
    """Deterministic owner-authorization gate for outbound email (WS-8)."""

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        approval_manager: object,
        event_bus: object | None = None,
    ) -> None:
        self._db = db
        self._approval = approval_manager
        self._event_bus = event_bus

    async def check(
        self, *, request: object, recipient: str, message_text: str,
    ) -> GateDecision:
        """Allow or hold an outbound email. ``recipient`` is the resolved
        delivery address (validated thread recipient or pipeline default)."""
        now = datetime.now(UTC).isoformat()

        # 1. Derive classifier inputs in CODE — never from the LLM.
        recipient_known = getattr(request, "validated_recipient", None) is not None
        is_reply = await self._has_inbound(getattr(request, "thread_id", None))
        is_bulk = bool(getattr(request, "labeled_surplus", False))
        classification = classify_email_action(
            is_reply=is_reply,
            recipient_known=recipient_known,
            is_bulk=is_bulk,
            subject=getattr(request, "topic", "") or "",
            body=message_text,
        )
        domain, verb, risk = classification.cell_key

        # 2. FINANCIAL is hardline — held BEFORE any cell lookup, so a stray
        #    approval can never create/unlock a financial cell.
        if classification.risk_class == RiskClass.FINANCIAL:
            return await self._hold(request, message_text, classification, recipient, now)

        # 3. First sight of a cell moves NOT_DETERMINED → ASK; then read state.
        #    Suppress InvalidTransition: the cell may already be past ASK.
        with contextlib.suppress(InvalidTransition):
            await cg.apply_event(
                self._db, domain=domain, verb=verb, risk_class=risk,
                event=CellEvent.CLASSIFY, updated_at=now,
            )
        cell = await cg.get_cell(self._db, domain, verb, risk)
        state = CellState(cell["state"]) if cell else CellState.ASK

        if state == CellState.GRANTED:
            return GateDecision(allow=True, reason="granted")

        # 4. ASK / NOT_DETERMINED / DENIED → hold for owner approval.
        return await self._hold(request, message_text, classification, recipient, now)

    async def _has_inbound(self, thread_id: str | None) -> bool:
        """True iff the email thread has at least one received (inbound) message
        — i.e. this send is a reply, not cold outreach."""
        if not thread_id:
            return False
        cursor = await self._db.execute(
            "SELECT 1 FROM email_thread_messages "
            "WHERE thread_id = ? AND direction = 'received' LIMIT 1",
            (thread_id,),
        )
        return await cursor.fetchone() is not None

    async def _hold(
        self, request: object, message_text: str, classification: object,
        recipient: str, now: str,
    ) -> GateDecision:
        domain, verb, risk = classification.cell_key
        thread_id = getattr(request, "thread_id", None)
        category = getattr(request, "category", None)
        category_val = category.value if category is not None else "outreach"
        context = json.dumps({
            "kind": EMAIL_GATE_ACTION_TYPE,
            "cell": [domain, verb, risk],
            "validated_recipient": recipient,
            "thread_id": thread_id,
            "subject": getattr(request, "topic", "") or "",
            "category": category_val,
        })

        # Approval row FIRST — so a crash between the two writes never leaves a
        # pending_email_sends row pointing at a non-existent approval.
        request_id = await self._approval.request_approval(
            action_type=EMAIL_GATE_ACTION_TYPE,
            action_class=str(classification.action_class),
            description=f"Send {classification.sub_class} email to {recipient}",
            context=context,
            timeout_seconds=None,  # wait for the owner; never auto-approve, never auto-drop
        )
        pending_id = str(uuid.uuid4())
        await pes.create(
            self._db,
            id=pending_id,
            request_id=request_id,
            validated_recipient=recipient,
            category=category_val,
            message=message_text,
            cell_domain=domain,
            cell_verb=verb,
            cell_risk_class=risk,
            held_at=now,
            thread_id=thread_id,
        )
        await self._emit_held(classification, recipient)
        logger.info(
            "Email gate HELD %s send to %s (cell=%s:%s:%s, request=%s)",
            classification.sub_class, recipient, domain, verb, risk, request_id,
        )
        return GateDecision(
            allow=False, pending_id=pending_id, request_id=request_id, reason="held",
        )

    async def _emit_held(self, classification: object, recipient: str) -> None:
        """Emit the EXPECTED autonomy.gate_held event at INFO (Tenet 0b) — so
        system monitoring knows a hold is routine, not a tool failure."""
        if self._event_bus is None:
            return
        try:
            await self._event_bus.emit(
                Subsystem.AUTONOMY,
                Severity.INFO,
                "autonomy.gate_held",
                f"Email capability gate held a {classification.sub_class} send "
                f"to {recipient} (cell {':'.join(classification.cell_key)})",
            )
        except Exception:
            logger.error("Failed to emit autonomy.gate_held", exc_info=True)
