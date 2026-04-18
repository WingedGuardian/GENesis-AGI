"""Compute-routing type definitions — enums, frozen dataclasses, protocols."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class ProviderState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class ErrorCategory(StrEnum):
    TRANSIENT = "transient"
    DEGRADED = "degraded"
    PERMANENT = "permanent"
    QUOTA_EXHAUSTED = "quota_exhausted"


class DegradationLevel(StrEnum):
    NORMAL = "L0"
    FALLBACK = "L1"
    REDUCED = "L2"
    ESSENTIAL = "L3"
    MEMORY_IMPAIRED = "L4"
    LOCAL_COMPUTE_DOWN = "L5"


class BudgetStatus(StrEnum):
    UNDER_LIMIT = "under_limit"
    WARNING = "warning"
    EXCEEDED = "exceeded"


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    provider_type: str
    model_id: str
    is_free: bool
    rpm_limit: int | None
    open_duration_s: int
    base_url: str | None = None
    keep_alive: str | int | None = None
    enabled: bool = True
    profile: str | None = None


@dataclass(frozen=True)
class CallSiteConfig:
    id: str
    chain: list[str]
    default_paid: bool = False
    never_pays: bool = False
    retry_profile: str = "default"
    # Runtime dispatch mode, read by AutonomousDispatchRouter.route():
    #   "dual" (default) — API chain first, CLI fallback on exhaustion
    #   "cli"             — skip the API chain entirely, go straight to
    #                       the CLI approval gate (user wants CC CLI)
    #   "api"             — API-only; if the chain exhausts, return
    #                       mode="blocked" instead of escalating to CLI
    # Stored under ``call_sites.<id>.dispatch`` in model_routing.yaml.
    # Legacy alias ``"cc"`` (written by earlier dashboard code before
    # the three-state selector landed) is accepted at load time and
    # normalized to ``"cli"`` — see ``config._normalize_dispatch``.
    dispatch: str = "dual"


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 3
    base_delay_ms: int = 500
    max_delay_ms: int = 30000
    backoff_multiplier: float = 2.0
    jitter_pct: float = 0.25


@dataclass(frozen=True)
class CallResult:
    success: bool
    content: str | None = None
    error: str | None = None
    status_code: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cost_known: bool = True
    retry_after_s: float | None = None


@dataclass(frozen=True)
class RoutingResult:
    success: bool
    call_site_id: str
    provider_used: str | None = None
    model_id: str | None = None
    content: str | None = None
    attempts: int = 0
    fallback_used: bool = False
    failed_providers: tuple[str, ...] = ()
    error: str | None = None
    dead_lettered: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class RoutingConfig:
    providers: dict[str, ProviderConfig]
    call_sites: dict[str, CallSiteConfig]
    retry_profiles: dict[str, RetryPolicy]
    # Provider name → provider_type for providers disabled at config load
    # (e.g., no API key). Stored for the API keys dashboard display.
    disabled_providers: dict[str, str] = field(default_factory=dict)


class CallDelegate(Protocol):
    async def call(
        self, provider: str, model_id: str, messages: list[dict], **kwargs
    ) -> CallResult: ...
