"""Autonomous dispatch policy — API-first routing with gated CLI fallback."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from genesis.autonomy.cli_policy import load_autonomous_cli_policy
from genesis.cc.types import CCInvocation, CCOutput

logger = logging.getLogger(__name__)

_APPROVE_WORDS = frozenset({
    "approve", "approved", "ok", "yes", "go", "lgtm",
})
_REJECT_WORDS = frozenset({
    "reject", "rejected", "deny", "denied", "no", "nope",
})


def _json_loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _context_dump(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True)


def _approval_key(
    *,
    subsystem: str,
    policy_id: str,
    action_label: str,
    invocation: CCInvocation | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "subsystem": subsystem,
        "policy_id": policy_id,
        "action_label": action_label,
    }
    if invocation is not None:
        payload["prompt"] = invocation.prompt
        payload["model"] = str(invocation.model)
        payload["effort"] = str(invocation.effort)
        payload["system_prompt"] = invocation.system_prompt or ""
        payload["working_dir"] = invocation.working_dir or ""
    if extra:
        payload["extra"] = extra
    raw = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _reply_decision(reply_text: str) -> str | None:
    import re

    text = (reply_text or "").strip().lower()
    if not text:
        return None
    first = re.sub(r"[^\w]", "", text.split()[0])
    if not first:
        return None
    if first in _APPROVE_WORDS:
        return "approved"
    if first in _REJECT_WORDS:
        return "rejected"
    return None


@dataclass(frozen=True)
class AutonomousDispatchRequest:
    subsystem: str
    policy_id: str
    action_label: str
    messages: list[dict[str, str]]
    cli_invocation: CCInvocation
    api_call_site_id: str | None = None
    cli_fallback_allowed: bool = True
    approval_required_for_cli: bool = True
    context: dict[str, Any] | None = None
    # Optional per-call override of the call site's runtime dispatch
    # mode.  When ``None`` (default), ``AutonomousDispatchRouter.route``
    # looks up ``CallSiteConfig.dispatch`` from the routing config for
    # ``api_call_site_id``.  Explicit values "api" / "cli" / "dual"
    # bypass the config lookup entirely — use sparingly, mostly for
    # tests or targeted one-shot overrides.  See
    # ``genesis.routing.config._VALID_DISPATCH_MODES``.
    dispatch_mode: str | None = None


@dataclass(frozen=True)
class AutonomousDispatchDecision:
    mode: str  # "api" | "cli_approved" | "blocked"
    reason: str
    output: CCOutput | None = None
    provider_used: str | None = None
    approval_request_id: str | None = None
    api_error: str | None = None


class AutonomousCliApprovalGate:
    """Manual-approval gate for autonomous CLI fallback dispatch."""

    def __init__(
        self,
        *,
        runtime: Any,
        approval_manager: Any,
        policy_loader=load_autonomous_cli_policy,
    ) -> None:
        self._runtime = runtime
        self._approval_manager = approval_manager
        self._policy_loader = policy_loader
        self._delivery_to_request: dict[str, str] = {}

    async def hydrate_delivery_map(self, db) -> int:
        """Rebuild _delivery_to_request from pending approvals in DB.

        Call at startup to restore quote-reply resolution after restart.
        Returns number of mappings restored.
        """
        from genesis.db.crud import approval_requests

        pending = await approval_requests.list_pending(db)
        count = 0
        for row in pending:
            request_id = row.get("id")
            context_raw = row.get("context")
            if not request_id or not context_raw:
                continue
            context = _json_loads(context_raw)
            delivery_id = context.get("delivery_id")
            if delivery_id:
                self._delivery_to_request[str(delivery_id)] = request_id
                count += 1
        if count:
            logger.info(
                "Hydrated delivery-to-request map: %d pending approvals", count,
            )
        return count

    def _policy(self):
        return self._policy_loader()

    @property
    def approval_manager(self) -> Any:
        """Public accessor for the underlying ApprovalManager.

        Exposed so callers (specifically the inbox resume pass) can
        look up approval rows by id without reaching into the private
        ``_approval_manager`` attribute.  Using the public property
        means wrappers / test doubles that only mirror public API
        still work without silent fall-through.
        """
        return self._approval_manager

    async def ensure_approval(
        self,
        *,
        subsystem: str,
        policy_id: str,
        action_label: str,
        invocation: CCInvocation | None = None,
        api_call_site_id: str | None = None,
        api_error: str | None = None,
        extra_context: dict[str, Any] | None = None,
        action_type: str = "autonomous_cli_fallback",
    ) -> tuple[str, str | None, str]:
        """Return ``(status, request_id, reason)`` for CLI fallback.

        Status is one of: ``approved``, ``pending``, ``rejected``.

        For sentinel approvals, pass ``action_type="sentinel_dispatch"``
        or ``"sentinel_action"`` and include sentinel-specific details
        in ``extra_context``.
        """
        policy = self._policy()
        if not policy.manual_approval_required:
            return ("approved", None, "manual approval disabled by config")

        approval_key = _approval_key(
            subsystem=subsystem,
            policy_id=policy_id,
            action_label=action_label,
            invocation=invocation,
            extra=extra_context,
        )

        existing = await self._find_existing(
            approval_key, subsystem=subsystem, policy_id=policy_id,
        )
        if existing is not None:
            status = str(existing.get("status") or "pending")
            request_id = str(existing["id"])
            context = _json_loads(existing.get("context"))
            delivery_id = context.get("delivery_id")
            if delivery_id:
                self._delivery_to_request[str(delivery_id)] = request_id
            if status == "approved":
                logger.info(
                    "%s pre-approved for %s (%s)",
                    action_type, policy_id, request_id,
                )
                return ("approved", request_id, "existing approval found")
            if status == "rejected":
                logger.info(
                    "%s previously rejected for %s (%s)",
                    action_type, policy_id, request_id,
                )
                return ("rejected", request_id, "existing rejection found")

            resent = await self._maybe_resend(
                request_id=request_id,
                context=context,
                subsystem=subsystem,
                policy_id=policy_id,
                action_label=action_label,
                invocation=invocation,
                api_error=api_error,
            )
            if resent:
                return ("pending", request_id, "approval pending; reminder sent")
            return ("pending", request_id, "approval pending")

        description = (
            f"Approve {action_label}?"
            if action_type.startswith("sentinel_")
            else f"Approve autonomous Claude Code fallback for {action_label}?"
        )
        context = {
            "kind": "autonomous_cli_fallback",
            "approval_key": approval_key,
            "subsystem": subsystem,
            "policy_id": policy_id,
            "action_label": action_label,
            "action_type": action_type,
            "api_call_site_id": api_call_site_id,
            "api_error": api_error,
            "model": str(invocation.model) if invocation else None,
            "effort": str(invocation.effort) if invocation else None,
            "channel": policy.approval_channel,
            "delivery_id": None,
            "last_sent_at": None,
            "next_reask_at": None,
        }
        if extra_context:
            context["extra"] = extra_context

        request_id = await self._approval_manager.request_approval(
            action_type=action_type,
            action_class="costly_reversible",
            description=description,
            context=_context_dump(context),
            timeout_seconds=None,
        )

        await self._send_request(
            request_id=request_id,
            context=context,
            action_label=action_label,
            invocation=invocation,
            api_error=api_error,
        )
        return ("pending", request_id, "approval requested")

    async def resolve_from_reply(self, delivery_id: str, reply_text: str) -> bool:
        """Resolve an approval from a Telegram quote-reply to a specific
        message.

        Legacy fallback for users who formally quote-reply to a specific
        approval message.  The primary UX is inline ✅ buttons, and the
        secondary UX is a bare "approve"/"reject" text in the Approvals
        topic (handled separately in the Telegram handler, resolving the
        most recent pending request).  This path stays for the minority
        case of quote-reply to a specific historic message.

        IMPORTANT: single-reply *no longer* auto-batches.  If the user
        wants to approve multiple queued requests at once, they use the
        explicit "✅✅ Approve all N pending" inline button.  Silent
        batch-approve from a single reply was surprising and made it
        impossible to approve one-out-of-many.
        """
        request_id = self._delivery_to_request.get(str(delivery_id))
        if request_id is None:
            return False
        decision = _reply_decision(reply_text)
        if decision is None:
            return False
        ok = await self._approval_manager.resolve(
            request_id,
            status=decision,
            resolved_by=f"{self._policy().approval_channel}:reply",
        )
        if ok:
            logger.info(
                "Resolved autonomous CLI approval %s as %s via %s reply",
                request_id, decision, self._policy().approval_channel,
            )
        return ok

    async def resolve_most_recent_pending(
        self, *, decision: str, resolved_by: str,
    ) -> str | None:
        """Resolve the most-recent pending autonomous_cli_fallback approval.

        Used by the Telegram "bare approve/reject in Approvals topic"
        handler so the user can type "approve" without having to quote-
        reply to a specific message.  Returns the resolved request_id,
        or ``None`` if no pending request exists.

        Only considers ``action_type == 'autonomous_cli_fallback'`` so
        unrelated approval requests (ego, modules, etc.) are not
        accidentally touched.
        """
        if decision not in ("approved", "rejected"):
            logger.warning(
                "resolve_most_recent_pending called with invalid decision "
                "%r (expected 'approved' or 'rejected') — no-op",
                decision,
            )
            return None
        pending = await self._approval_manager.get_pending()
        # Most recent first (get_pending returns ordered by created_at ASC)
        candidates = [
            req for req in pending
            if req.get("action_type") == "autonomous_cli_fallback"
        ]
        if not candidates:
            return None
        most_recent = candidates[-1]
        request_id = str(most_recent["id"])
        ok = await self._approval_manager.resolve(
            request_id, status=decision, resolved_by=resolved_by,
        )
        if ok:
            logger.info(
                "Resolved most-recent autonomous CLI approval %s as %s via %s",
                request_id, decision, resolved_by,
            )
            return request_id
        return None

    async def approve_all_pending(self, *, resolved_by: str) -> int:
        """Approve all pending CLI-fallback approval requests. Returns count.

        Scoped to ``autonomous_cli_fallback`` action type only — does NOT
        touch approval requests from other subsystems (ego, modules, etc.).
        """
        pending = await self._approval_manager.get_pending()
        count = 0
        for req in pending:
            if req.get("action_type") != "autonomous_cli_fallback":
                continue
            ok = await self._approval_manager.resolve(
                req["id"], status="approved", resolved_by=resolved_by,
            )
            if ok:
                count += 1
        return count

    async def resolve_request(
        self, request_id: str, *, decision: str, resolved_by: str,
    ) -> bool:
        return await self._approval_manager.resolve(
            request_id, status=decision, resolved_by=resolved_by,
        )

    async def _find_existing(
        self, approval_key: str,
        *,
        subsystem: str | None = None,
        policy_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Find an existing approval request that matches this call.

        Primary match: content-stable ``approval_key`` (hits any status,
        preserves "previously rejected" behaviour so a rejected row blocks
        the dispatch instead of creating a duplicate).

        Race-safety fallback: if no approval_key match and the caller
        provided ``subsystem``/``policy_id``, also match any *pending* row
        whose context has the same (subsystem, policy_id).  This catches
        the case where two concurrent schedulers build slightly different
        content hashes for the same call site and would otherwise each
        create their own approval.

        Resume fallback: match any *approved* row for the same site.
        This handles the approval-resume path: the user approved a
        reflection, the resume triggers it on a new tick with different
        prompt data (different approval_key), but the approved status
        from the original request should still be honored.
        """
        recent = await self._approval_manager.get_recent(limit=200)
        # Pass 1: exact content-key match (preserves status-sensitive behavior).
        for row in recent:
            context = _json_loads(row.get("context"))
            if (
                context.get("kind") == "autonomous_cli_fallback"
                and context.get("approval_key") == approval_key
            ):
                return row
        # Pass 2: race-safety fallback — pending rows for the same site.
        if subsystem is None or policy_id is None:
            return None
        for row in recent:
            if str(row.get("status") or "") != "pending":
                continue
            context = _json_loads(row.get("context"))
            if (
                context.get("kind") == "autonomous_cli_fallback"
                and context.get("subsystem") == subsystem
                and context.get("policy_id") == policy_id
            ):
                return row
        # Pass 3: resume fallback — approved rows for the same site.
        # When a user approves a reflection/Sentinel dispatch and the
        # resume path re-enters ensure_approval with different tick data,
        # the approval_key won't match Pass 1. This pass picks up the
        # approved row so the action can proceed without a second approval.
        for row in recent:
            if str(row.get("status") or "") != "approved":
                continue
            context = _json_loads(row.get("context"))
            if (
                context.get("kind") == "autonomous_cli_fallback"
                and context.get("subsystem") == subsystem
                and context.get("policy_id") == policy_id
            ):
                return row
        return None

    async def find_site_pending(
        self, *, subsystem: str, policy_id: str,
    ) -> dict[str, Any] | None:
        """Return the pending autonomous_cli_fallback approval for this
        call site, or ``None`` if nothing is pending.

        Used by callers to skip scheduling work while the call site is
        gated on an approval.  Correctness invariant: the returned row
        has ``status == 'pending'`` AND its context has matching
        ``subsystem`` and ``policy_id``.  Approved/rejected rows are
        *not* returned — their state has already been resolved and the
        caller should dispatch normally (which will hit ``ensure_approval``
        and get the resolved status).
        """
        pending = await self._approval_manager.get_pending()
        for row in pending:
            if row.get("action_type") != "autonomous_cli_fallback":
                continue
            context = _json_loads(row.get("context"))
            if (
                context.get("subsystem") == subsystem
                and context.get("policy_id") == policy_id
            ):
                return row
        return None

    async def find_recently_approved(
        self, *, subsystem: str, policy_id: str,
    ) -> dict[str, Any] | None:
        """Find an approved-but-unconsumed request for this call site.

        Used by the resume mechanism: when a user approves a blocked action
        (via Telegram/dashboard), the next awareness tick can pick it up and
        dispatch the action without waiting for the original trigger to re-fire.
        """
        from genesis.db.crud import approval_requests as ar_crud

        return await ar_crud.find_approved_unconsumed(
            self._approval_manager._db,
            subsystem=subsystem, policy_id=policy_id,
        )

    async def mark_consumed(self, request_id: str) -> bool:
        """Mark an approved request as consumed (action dispatched).

        Atomic: returns False if already consumed (double-dispatch guard).
        """
        from datetime import UTC, datetime

        from genesis.db.crud import approval_requests as ar_crud

        return await ar_crud.mark_consumed(
            self._approval_manager._db, request_id,
            consumed_at=datetime.now(UTC).isoformat(),
        )

    async def get_pending_count(self) -> int:
        """Return the count of pending approvals (CLI fallback + sentinel).

        Used by ``_send_request`` to decide whether to include the
        "✅✅ Approve all N pending" batch button in the inline keyboard.
        """
        _GATED_TYPES = {"autonomous_cli_fallback", "sentinel_dispatch", "sentinel_action"}
        pending = await self._approval_manager.get_pending()
        return sum(
            1 for req in pending
            if req.get("action_type") in _GATED_TYPES
        )

    async def _maybe_resend(
        self,
        *,
        request_id: str,
        context: dict[str, Any],
        subsystem: str,
        policy_id: str,
        action_label: str,
        invocation: CCInvocation | None,
        api_error: str | None,
    ) -> bool:
        next_reask_at = context.get("next_reask_at")
        if next_reask_at:
            try:
                next_dt = datetime.fromisoformat(str(next_reask_at))
                if next_dt > datetime.now(UTC):
                    return False
            except ValueError:
                pass
        await self._send_request(
            request_id=request_id,
            context=context,
            action_label=action_label,
            invocation=invocation,
            api_error=api_error,
        )
        logger.info(
            "Re-sent autonomous CLI approval request %s for %s/%s",
            request_id, subsystem, policy_id,
        )
        return True

    async def _send_request(
        self,
        *,
        request_id: str,
        context: dict[str, Any],
        action_label: str,
        invocation: CCInvocation | None,
        api_error: str | None,
    ) -> None:
        """Deliver an approval notification via the outreach pipeline.

        Routes through ``OutreachPipeline.submit_raw`` with
        ``OutreachCategory.APPROVAL``, so topic routing sends the message
        to the "Approvals" supergroup topic.  Attaches an inline keyboard:

        - One row with a single ✅ Approve button keyed to this
          ``request_id``.
        - A second row with "✅✅ Approve all N pending" *iff* at least
          two ``autonomous_cli_fallback`` approvals are pending at send
          time (including this one).

        Fire-and-forget: the dispatcher stores the request in
        ``approval_requests`` with ``status='pending'`` and relies on the
        caller's next tick to notice resolution via ``find_site_pending``
        / ``ensure_approval``.  Does NOT use ``submit_raw_and_wait`` —
        that's Sentinel's blocking pattern and does not fit the gating
        model.
        """
        delivery_id: str | None = None
        pipeline = getattr(self._runtime, "_outreach_pipeline", None)

        if pipeline is None:
            # ERROR per observability rules: delivery failure of an approval
            # request is an operational failure, not a recoverable degradation.
            # The request row is still in approval_requests, so the dashboard
            # approval API can resolve it manually.
            logger.error(
                "Approval request %s cannot be delivered: "
                "outreach pipeline unavailable; dashboard-only fallback",
                request_id,
            )
        else:
            # Count BEFORE sending so we don't double-count the current
            # request (which is already in 'pending' state by now).
            try:
                pending_count = await self.get_pending_count()
            except Exception:
                logger.warning(
                    "Failed to count pending approvals for %s", request_id,
                    exc_info=True,
                )
                pending_count = 1

            message = self._format_message(
                request_id=request_id,
                action_label=action_label,
                invocation=invocation,
                api_error=api_error,
                pending_count=pending_count,
                action_type=context.get("action_type", "autonomous_cli_fallback"),
                extra_context=context.get("extra"),
            )

            # Build inline keyboard — lazy import to keep the module
            # importable without python-telegram-bot installed (mirrors
            # Sentinel's pattern at sentinel/dispatcher.py:387).
            keyboard: object | None = None
            try:
                from telegram import InlineKeyboardButton, InlineKeyboardMarkup

                rows = [[InlineKeyboardButton(
                    "✅ Approve",
                    callback_data=f"cli_approve:{request_id}",
                )]]
                if pending_count >= 2:
                    rows.append([InlineKeyboardButton(
                        f"✅✅ Approve all {pending_count} pending",
                        callback_data=f"cli_approve_all:{request_id}",
                    )])
                keyboard = InlineKeyboardMarkup(rows)
            except ImportError:
                logger.warning(
                    "python-telegram-bot not installed; approval %s will "
                    "deliver without inline buttons",
                    request_id,
                )

            try:
                from genesis.outreach.types import (
                    OutreachCategory,
                    OutreachRequest,
                    OutreachStatus,
                )

                outreach_request = OutreachRequest(
                    category=OutreachCategory.APPROVAL,
                    topic=f"Approval: {action_label[:80]}",
                    context=message,
                    salience_score=1.0,
                    signal_type="cli_approval",
                    source_id=f"cli-approval:{request_id}",
                )
                result = await pipeline.submit_raw(
                    message, outreach_request, reply_markup=keyboard,
                )
                if result.status == OutreachStatus.DELIVERED and result.delivery_id:
                    delivery_id = str(result.delivery_id)
                    # Populate the mapping so legacy quote-reply text
                    # fallback still works for users who formally reply
                    # to a specific message.
                    self._delivery_to_request[delivery_id] = request_id
                else:
                    logger.error(
                        "Approval request %s delivery did not complete "
                        "(status=%s, error=%s); dashboard-only fallback",
                        request_id, result.status, result.error,
                    )
            except Exception:
                logger.error(
                    "Failed to deliver approval request %s via outreach pipeline",
                    request_id, exc_info=True,
                )

        # Record the delivery_id regardless (None means "not yet delivered"),
        # but only advance the re-ask cadence when delivery actually
        # succeeded.  If delivery failed (delivery_id is None), leave
        # last_sent_at / next_reask_at alone so the NEXT scan tick can
        # retry immediately via _maybe_resend instead of waiting the full
        # reask_interval_hours (24h default).  The prior code bumped the
        # reask window unconditionally, which made failed deliveries
        # invisible for 24h even though the retry loop was ready to go.
        context["delivery_id"] = delivery_id
        if delivery_id is not None:
            now = datetime.now(UTC)
            context["last_sent_at"] = now.isoformat()
            context["next_reask_at"] = (
                now + timedelta(hours=max(1, self._policy().reask_interval_hours))
            ).isoformat()
        await self._approval_manager.update_context(
            request_id, context=_context_dump(context),
        )

    @staticmethod
    def _format_message(
        *,
        request_id: str,
        action_label: str,
        invocation: CCInvocation | None,
        api_error: str | None,
        pending_count: int = 1,
        action_type: str = "autonomous_cli_fallback",
        extra_context: dict[str, Any] | None = None,
    ) -> str:
        extra = extra_context or {}

        # Sentinel-specific message formatting
        if action_type == "sentinel_dispatch":
            tier_label = extra.get("tier_label", "Unknown tier")
            trigger_source = extra.get("trigger_source", "unknown")
            trigger_reason = extra.get("trigger_reason", "")
            lines = [
                "<b>Sentinel Activation Request</b>",
                "",
                f"The Sentinel detected a <b>{tier_label}</b> fire alarm and "
                f"wants to investigate and fix the issue.",
                "",
                f"<b>Trigger:</b> {trigger_source}",
                f"<b>Reason:</b> {trigger_reason}",
                f"Request ID: <code>{request_id}</code>",
            ]
        elif action_type == "sentinel_action":
            diagnosis = extra.get("diagnosis", "")[:200]
            actions = extra.get("proposed_actions", [])
            action_lines = []
            for i, action in enumerate(actions[:5], 1):
                desc = action.get("description", "Unknown action")
                cmd = action.get("command", "")
                safe = "safe" if action.get("safe") else "potentially unsafe"
                action_lines.append(f"{i}. {desc}\n   <code>{cmd}</code> ({safe})")
            lines = [
                "<b>Sentinel Action Approval</b>",
                "",
                f"<b>Diagnosis:</b> {diagnosis}",
                "",
                "<b>Proposed actions:</b>",
                *action_lines,
                "",
                f"Request ID: <code>{request_id}</code>",
            ]
        else:
            # Default: autonomous CLI fallback message
            lines = [
                "<b>Approval Needed</b>",
                "",
                f"Approve autonomous Claude Code fallback for <b>{action_label}</b>?",
                f"Request ID: <code>{request_id}</code>",
            ]
            if invocation:
                lines.extend([
                    f"Model: <code>{invocation.model}</code>",
                    f"Effort: <code>{invocation.effort}</code>",
                ])
            if api_error:
                lines.extend([
                    "",
                    "<b>Why CLI fallback is being considered</b>",
                    api_error[:500],
                ])

        lines.extend([
            "",
            "Tap ✅ below, or type <code>approve</code> in the Approvals "
            "topic (bare message or quote-reply both work).",
        ])
        if pending_count >= 2:
            lines.append(
                f"<i>{pending_count - 1} other approval(s) pending — "
                f"use the batch button to resolve them together.</i>",
            )
        return "\n".join(lines)


class AutonomousDispatchRouter:
    """API-first router for autonomous/background calls."""

    def __init__(
        self,
        *,
        router: Any,
        approval_gate: AutonomousCliApprovalGate,
        policy_loader=load_autonomous_cli_policy,
    ) -> None:
        self._router = router
        self._approval_gate = approval_gate
        self._policy_loader = policy_loader

    @property
    def approval_gate(self) -> AutonomousCliApprovalGate:
        """Public accessor for the CLI approval gate.

        Exposed so call sites (inbox, ego, reflection, executor) can
        perform ``find_site_pending`` pre-checks to skip scheduling new
        work when their call site is blocked on an in-flight approval.
        """
        return self._approval_gate

    def _resolve_dispatch_mode(
        self, request: AutonomousDispatchRequest,
    ) -> str:
        """Return the effective dispatch mode for a request.

        Precedence:
          1. ``request.dispatch_mode`` if set (explicit per-call override).
          2. ``CallSiteConfig.dispatch`` from the routing config, looked
             up via ``api_call_site_id``.
          3. ``"dual"`` as a safe default when neither source resolves
             (e.g. unknown call site id, router missing config).

        Never raises — unknown values from either source fall through
        to ``"dual"`` rather than blocking dispatch entirely.
        """
        if request.dispatch_mode is not None:
            return request.dispatch_mode
        call_site_id = request.api_call_site_id
        if not call_site_id:
            return "dual"
        # Defensive lookup: the router's config is a dataclass in prod
        # but tests routinely pass an ``AsyncMock`` as the router, whose
        # attribute access returns ``AsyncMock`` instances that don't
        # behave like a real mapping.  Require ``call_sites`` to quack
        # like a ``Mapping`` before indexing into it; any mismatch
        # defaults to "dual" so tests / misconfigured routers never
        # throw here.  ``Mapping`` (not ``dict``) is deliberate: keeps
        # the door open to read-only mapping types like
        # ``types.MappingProxyType`` or frozen-dict subclasses.
        from collections.abc import Mapping
        try:
            call_sites = self._router.config.call_sites
        except AttributeError:
            return "dual"
        if not isinstance(call_sites, Mapping):
            return "dual"
        site = call_sites.get(call_site_id)
        if site is None:
            return "dual"
        value = getattr(site, "dispatch", None)
        if not isinstance(value, str) or not value:
            return "dual"
        return value

    async def route(
        self, request: AutonomousDispatchRequest,
    ) -> AutonomousDispatchDecision:
        api_error: str | None = None
        dispatch_mode = self._resolve_dispatch_mode(request)

        # CLI-only: skip the API chain entirely and go straight to the
        # approval gate + CLI fallback.  Used when the user forces a
        # call site onto the Claude Code subprocess path (e.g. to test
        # the approval gate, or to work around an unusable API key).
        # ``cli_fallback_allowed=False`` still wins here — a caller
        # that explicitly disables CLI fallback should be respected
        # even with dispatch=cli, so we return a dedicated ``blocked``
        # mode rather than silently ignoring the flag.
        if dispatch_mode == "cli":
            if not request.cli_fallback_allowed:
                return AutonomousDispatchDecision(
                    mode="blocked",
                    reason="dispatch=cli but CLI fallback disabled by caller",
                )
            logger.info(
                "Autonomous dispatch %s: dispatch=cli, skipping API chain",
                request.policy_id,
            )
            api_error = "Call site forced to CLI via dispatch=cli toggle"
            return await self._cli_fallback_decision(
                request, api_error=api_error,
            )

        if request.api_call_site_id:
            result = await self._router.route_call(
                request.api_call_site_id, request.messages,
            )
            # Success is only a real success if the provider actually
            # produced content.  Free-tier providers (notably gemini-free)
            # can return HTTP 200 with empty/null content — the router
            # sees that as success, but downstream consumers receive an
            # empty CCOutput and silently produce blank artifacts (e.g.
            # frontmatter-only inbox response files).  Treat empty
            # content as a failed dispatch so we fall through to the
            # CLI fallback path.
            if result.success and (result.content or "").strip():
                logger.info(
                    "Autonomous dispatch %s routed via API provider %s",
                    request.policy_id, result.provider_used,
                )
                return AutonomousDispatchDecision(
                    mode="api",
                    reason=f"API route succeeded via {result.provider_used}",
                    provider_used=result.provider_used,
                    output=CCOutput(
                        session_id="",
                        text=result.content or "",
                        model_used=result.model_id or result.provider_used or "",
                        cost_usd=result.cost_usd,
                        input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                        duration_ms=0,
                        exit_code=0,
                    ),
                )
            if result.success:
                api_error = (
                    f"provider {result.provider_used} returned empty content"
                )
                # ERROR not WARNING: a provider returning HTTP 200 with
                # empty content is an operational API failure, not a
                # recoverable degradation.  Per CLAUDE.md observability
                # rules, API call failures log at ERROR.
                logger.error(
                    "Autonomous dispatch %s: provider %s returned empty "
                    "content — treating as failure",
                    request.policy_id, result.provider_used,
                )
            else:
                api_error = result.error or "API route failed"
                logger.warning(
                    "Autonomous dispatch %s API route failed: %s",
                    request.policy_id, api_error,
                )

        # API-only mode: if the API chain exhausted without a usable
        # response, do NOT escalate to CLI.  Return ``mode="blocked"``
        # with a clear reason so the caller (e.g. reflection bridge)
        # can record a failed call-site run instead of silently
        # triggering CC subprocess dispatch the operator said not to.
        if dispatch_mode == "api":
            return AutonomousDispatchDecision(
                mode="blocked",
                reason=(
                    "dispatch=api: API chain exhausted, CLI escalation "
                    "suppressed per call-site config"
                ),
                api_error=api_error,
            )

        return await self._cli_fallback_decision(
            request, api_error=api_error,
        )

    async def _cli_fallback_decision(
        self,
        request: AutonomousDispatchRequest,
        *,
        api_error: str | None,
    ) -> AutonomousDispatchDecision:
        """Run the CLI fallback approval / gate logic and return a decision.

        Extracted from ``route`` so the ``dispatch=cli`` short-circuit
        branch and the normal API-exhausted branch share the same code
        path.  Honours the autonomous-CLI policy (fallback disabled →
        ``blocked``) and the approval gate (pending / rejected →
        ``blocked``).
        """
        policy = self._policy_loader()
        if (
            not policy.autonomous_cli_fallback_enabled
            or not request.cli_fallback_allowed
        ):
            return AutonomousDispatchDecision(
                mode="blocked",
                reason="CLI fallback disabled",
                api_error=api_error,
            )

        request_id: str | None = None
        if request.approval_required_for_cli:
            status, request_id, reason = await self._approval_gate.ensure_approval(
                subsystem=request.subsystem,
                policy_id=request.policy_id,
                action_label=request.action_label,
                invocation=request.cli_invocation,
                api_call_site_id=request.api_call_site_id,
                api_error=api_error,
                extra_context=request.context,
            )
            if status != "approved":
                return AutonomousDispatchDecision(
                    mode="blocked",
                    reason=reason,
                    approval_request_id=request_id,
                    api_error=api_error,
                )

        logger.info(
            "Autonomous dispatch %s approved for CLI fallback",
            request.policy_id,
        )
        return AutonomousDispatchDecision(
            mode="cli_approved",
            reason="CLI fallback approved",
            approval_request_id=request_id,
            api_error=api_error,
        )
