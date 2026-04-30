"""Autonomous dispatch policy — API-first routing with gated CLI fallback.

Data classes live here.  Implementation split into:
- ``approval_gate.py`` — ``AutonomousCliApprovalGate`` + approval helpers
- ``dispatch_router.py`` — ``AutonomousDispatchRouter``

All public names are re-exported for backward compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from genesis.cc.types import CCInvocation, CCOutput

# --- Data classes (canonical home) ----------------------------------------

_APPROVE_WORDS = frozenset({
    "approve", "approved", "ok", "yes", "go", "lgtm",
})
_REJECT_WORDS = frozenset({
    "reject", "rejected", "deny", "denied", "no", "nope",
})


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
    # When True, the approval key is computed WITHOUT the invocation
    # content (prompt, model, etc.), making it stable across ticks.
    # Use for recurring dispatches (ego cycles, inbox, reflections)
    # where the action is the same but the prompt changes each time.
    # One pending request per (subsystem, policy_id, action_label);
    # approving it authorizes exactly one dispatch.
    # NOTE: ``context`` still enters the key. If you set this True,
    # ensure ``context`` is constant or None — a varying context dict
    # will silently break key stability.
    approval_key_stable: bool = False
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


# --- Re-exports (backward compatibility) ----------------------------------

from genesis.autonomy.approval_gate import (  # noqa: E402, F401
    AutonomousCliApprovalGate,
    _reply_decision,
)
from genesis.autonomy.dispatch_router import (  # noqa: E402, F401
    AutonomousDispatchRouter,
)

__all__ = [
    "AutonomousDispatchRequest",
    "AutonomousDispatchDecision",
    "AutonomousCliApprovalGate",
    "AutonomousDispatchRouter",
    "_reply_decision",
]
