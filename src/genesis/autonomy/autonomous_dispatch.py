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
    invocation: CCInvocation,
    extra: dict[str, Any] | None = None,
) -> str:
    payload = {
        "subsystem": subsystem,
        "policy_id": policy_id,
        "action_label": action_label,
        "prompt": invocation.prompt,
        "model": str(invocation.model),
        "effort": str(invocation.effort),
        "system_prompt": invocation.system_prompt or "",
        "working_dir": invocation.working_dir or "",
    }
    if extra:
        payload["extra"] = extra
    raw = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _reply_decision(reply_text: str) -> str | None:
    text = (reply_text or "").strip().lower()
    if not text:
        return None
    first = text.split()[0]
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

    def _policy(self):
        return self._policy_loader()

    async def ensure_approval(
        self,
        *,
        subsystem: str,
        policy_id: str,
        action_label: str,
        invocation: CCInvocation,
        api_call_site_id: str | None,
        api_error: str | None,
        extra_context: dict[str, Any] | None = None,
    ) -> tuple[str, str | None, str]:
        """Return ``(status, request_id, reason)`` for CLI fallback.

        Status is one of: ``approved``, ``pending``, ``rejected``.
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

        existing = await self._find_existing(approval_key)
        if existing is not None:
            status = str(existing.get("status") or "pending")
            request_id = str(existing["id"])
            context = _json_loads(existing.get("context"))
            delivery_id = context.get("delivery_id")
            if delivery_id:
                self._delivery_to_request[str(delivery_id)] = request_id
            if status == "approved":
                logger.info(
                    "Autonomous CLI fallback pre-approved for %s (%s)",
                    policy_id, request_id,
                )
                return ("approved", request_id, "existing approval found")
            if status == "rejected":
                logger.info(
                    "Autonomous CLI fallback previously rejected for %s (%s)",
                    policy_id, request_id,
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

        description = f"Approve autonomous Claude Code fallback for {action_label}?"
        context = {
            "kind": "autonomous_cli_fallback",
            "approval_key": approval_key,
            "subsystem": subsystem,
            "policy_id": policy_id,
            "action_label": action_label,
            "api_call_site_id": api_call_site_id,
            "api_error": api_error,
            "model": str(invocation.model),
            "effort": str(invocation.effort),
            "channel": policy.approval_channel,
            "delivery_id": None,
            "last_sent_at": None,
            "next_reask_at": None,
        }
        if extra_context:
            context["extra"] = extra_context

        request_id = await self._approval_manager.request_approval(
            action_type="autonomous_cli_fallback",
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

    async def resolve_request(
        self, request_id: str, *, decision: str, resolved_by: str,
    ) -> bool:
        return await self._approval_manager.resolve(
            request_id, status=decision, resolved_by=resolved_by,
        )

    async def _find_existing(self, approval_key: str) -> dict[str, Any] | None:
        recent = await self._approval_manager.get_recent(limit=200)
        for row in recent:
            context = _json_loads(row.get("context"))
            if (
                context.get("kind") == "autonomous_cli_fallback"
                and context.get("approval_key") == approval_key
            ):
                return row
        return None

    async def _maybe_resend(
        self,
        *,
        request_id: str,
        context: dict[str, Any],
        subsystem: str,
        policy_id: str,
        action_label: str,
        invocation: CCInvocation,
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
        invocation: CCInvocation,
        api_error: str | None,
    ) -> None:
        delivery_id: str | None = None
        adapter = None
        recipient = ""
        channel = self._policy().approval_channel
        pipeline = getattr(self._runtime, "_outreach_pipeline", None)
        if pipeline is not None:
            adapter = getattr(pipeline, "_channels", {}).get(channel)
            recipient = getattr(pipeline, "_recipients", {}).get(channel, "")

        if adapter is None or not recipient:
            logger.warning(
                "Approval request %s has no deliverable channel (%s); dashboard-only fallback",
                request_id, channel,
            )
        else:
            message = self._format_message(
                request_id=request_id,
                action_label=action_label,
                invocation=invocation,
                api_error=api_error,
            )
            delivery_id = str(await adapter.send_message(recipient, message))
            self._delivery_to_request[delivery_id] = request_id

        now = datetime.now(UTC)
        context["delivery_id"] = delivery_id
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
        invocation: CCInvocation,
        api_error: str | None,
    ) -> str:
        lines = [
            "<b>Approval Needed</b>",
            "",
            f"Approve autonomous Claude Code fallback for <b>{action_label}</b>?",
            f"Request ID: <code>{request_id}</code>",
            f"Model: <code>{invocation.model}</code>",
            f"Effort: <code>{invocation.effort}</code>",
        ]
        if api_error:
            lines.extend([
                "",
                "<b>Why CLI fallback is being considered</b>",
                api_error[:500],
            ])
        lines.extend([
            "",
            "Reply to this message with <code>approve</code> or <code>reject</code>.",
            "You can also resolve it from the dashboard approvals API.",
        ])
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

    async def route(
        self, request: AutonomousDispatchRequest,
    ) -> AutonomousDispatchDecision:
        api_error: str | None = None
        if request.api_call_site_id:
            result = await self._router.route_call(
                request.api_call_site_id, request.messages,
            )
            if result.success:
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
            api_error = result.error or "API route failed"
            logger.warning(
                "Autonomous dispatch %s API route failed: %s",
                request.policy_id, api_error,
            )

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
            api_error=api_error,
        )
