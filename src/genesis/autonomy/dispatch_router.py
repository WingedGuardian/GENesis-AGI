"""API-first dispatch router for autonomous/background calls.

Routes through the API provider chain first; falls back to CLI (Claude
Code subprocess) only when the API chain is exhausted and CLI fallback
is enabled + approved.
"""

from __future__ import annotations

import logging
from typing import Any

from genesis.autonomy.approval_gate import AutonomousCliApprovalGate
from genesis.autonomy.cli_policy import load_autonomous_cli_policy
from genesis.cc.types import CCOutput

logger = logging.getLogger(__name__)


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
        self, request,
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
        self, request,
    ):
        """Route an autonomous dispatch request.

        Returns an ``AutonomousDispatchDecision``.  Import is deferred to
        avoid circular dependency with the data classes module.
        """
        from genesis.autonomy.autonomous_dispatch import (
            AutonomousDispatchDecision,
        )

        api_error: str | None = None
        dispatch_mode = self._resolve_dispatch_mode(request)

        # CLI-only: skip the API chain entirely and go straight to the
        # approval gate + CLI fallback.
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
        # response, do NOT escalate to CLI.
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
        request,
        *,
        api_error: str | None,
    ):
        """Run the CLI fallback approval / gate logic and return a decision.

        Extracted from ``route`` so the ``dispatch=cli`` short-circuit
        branch and the normal API-exhausted branch share the same code
        path.  Honours the autonomous-CLI policy (fallback disabled →
        ``blocked``) and the approval gate (pending / rejected →
        ``blocked``).
        """
        from genesis.autonomy.autonomous_dispatch import (
            AutonomousDispatchDecision,
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

        request_id: str | None = None
        if request.approval_required_for_cli:
            # When approval_key_stable is set, exclude the invocation
            # from the approval key so recurring dispatches (ego cycles,
            # inbox, reflections) produce a stable key across ticks.
            # The real invocation is still used for the actual dispatch.
            approval_invocation = (
                None if request.approval_key_stable
                else request.cli_invocation
            )
            status, request_id, reason = await self._approval_gate.ensure_approval(
                subsystem=request.subsystem,
                policy_id=request.policy_id,
                action_label=request.action_label,
                invocation=approval_invocation,
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
            # Atomically consume — each dispatch needs its own approval.
            if request_id:
                consumed = await self._approval_gate.mark_consumed(request_id)
                if not consumed:
                    return AutonomousDispatchDecision(
                        mode="blocked",
                        reason="approval already consumed by concurrent dispatch",
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
