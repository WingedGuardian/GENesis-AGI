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
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.autonomy.capabilities import InvalidTransition
from genesis.autonomy.classification import classify_email_action
from genesis.autonomy.types import CellEvent, CellState, RiskClass
from genesis.db.crud import autonomous_email_sends as aes
from genesis.db.crud import capability_grants as cg
from genesis.db.crud import pending_email_sends as pes
from genesis.observability.types import Severity, Subsystem

logger = logging.getLogger(__name__)

#: Distinct action_type so these holds are isolated from CLI-fallback approvals
#: (excluded from ``approve_all_pending`` / the voice pending count).
EMAIL_GATE_ACTION_TYPE = "email_capability_gate"

#: WS-8 PR-D deterministic pre-send scope guards for a GRANTED cell.  A trip is
#: treated as scope drift inside the grant ⇒ the send is HELD and the cell is
#: demoted (recorded as a correction).  Conservative defaults; calibration here.
_RATE_LIMIT_WINDOW = timedelta(hours=1)   # g3: per-cell autonomous-send window
_RATE_LIMIT_MAX = 10                      # g3: max autonomous sends per cell per window


@dataclass(frozen=True)
class GateDecision:
    """Outcome of an autonomy-gate check. ``allow`` False ⇒ the send was held."""

    allow: bool
    pending_id: str | None = None
    request_id: str | None = None
    reason: str = ""
    #: (domain, verb, risk_class) of the cell — set on a GRANTED (autonomous)
    #: allow so the pipeline can log the send to the owner-visible ledger.
    cell: tuple[str, str, str] | None = None


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
            # WS-8 PR-D auto-revert net: deterministic pre-send scope guards.
            # A trip = scope drift inside the grant → demote (record a correction,
            # which regresses GRANTED→ASK) AND hold THIS send for owner approval.
            trip = await self._scope_guard_trip(request, recipient, domain, verb, risk)
            if trip is None:
                return GateDecision(
                    allow=True, reason="granted", cell=(domain, verb, risk),
                )
            logger.warning(
                "Email scope guard '%s' tripped on GRANTED cell %s:%s:%s "
                "(recipient=%s) — demoting + holding",
                trip, domain, verb, risk, recipient,
            )
            await cg.record_correction(
                self._db, domain=domain, verb=verb, risk_class=risk, updated_at=now,
            )
            return await self._hold(request, message_text, classification, recipient, now)

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

    async def _scope_guard_trip(
        self, request: object, recipient: str, domain: str, verb: str, risk: str,
    ) -> str | None:
        """Deterministic pre-send scope guards for a GRANTED cell.

        Returns a trip-reason if the send drifts outside the grant's scope (so
        the caller demotes + holds), else None (allow).  These are the ONLY
        autonomous safety net once a cell is GRANTED — a GRANTED cell no longer
        holds for owner approval, so nothing else would catch the drift.
        """
        # g1 (defense-in-depth): a standard reply must go to a participant of its
        # thread.  validated_recipient is already thread-derived, so this rarely
        # fires — but it catches a thread_id/recipient mismatch deterministically.
        if risk == RiskClass.STANDARD.value:
            thread_id = getattr(request, "thread_id", None)
            if not await self._recipient_in_thread(thread_id, recipient):
                return "recipient_mismatch"

        # g3 (primary): a burst of autonomous sends for one cell = a runaway loop.
        since = (datetime.now(UTC) - _RATE_LIMIT_WINDOW).isoformat()
        count = await aes.count_for_cell_since(
            self._db, cell_domain=domain, cell_verb=verb, cell_risk_class=risk,
            since=since,
        )
        if count >= _RATE_LIMIT_MAX:
            return "rate_limit"
        return None

    async def _recipient_in_thread(
        self, thread_id: str | None, recipient: str,
    ) -> bool:
        """True iff ``recipient`` is a known participant (received-message sender)
        of the thread.  The inbound path always records ``sender`` (the From
        header — ``reply_poller``/``threads.record_reply`` require it), so a
        normal reply matches its correspondent here.  We deliberately do NOT
        treat a NULL/blank sender as a match: that would let a single
        unparsed-sender row in a thread grant unbounded recipient scope (the safe
        failure for a SECURITY guard is to trip and hold, not to wave through)."""
        if not thread_id:
            return False  # a 'standard' reply with no thread is itself suspect
        cursor = await self._db.execute(
            "SELECT 1 FROM email_thread_messages "
            "WHERE thread_id = ? AND direction = 'received' AND sender = ? LIMIT 1",
            (thread_id, recipient),
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
