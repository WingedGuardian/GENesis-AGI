"""Type definitions for the Automaton Supervisor module."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class SurvivalTier(StrEnum):
    """Automaton survival tiers based on credit balance."""

    HIGH = "high"  # >$5
    NORMAL = "normal"  # >$0.50
    LOW_COMPUTE = "low_compute"  # >$0.10
    CRITICAL = "critical"  # $0
    DEAD = "dead"  # 1hr at zero


class InstanceStatus(StrEnum):
    """Lifecycle status of a managed Automaton instance."""

    PROVISIONING = "provisioning"
    ACTIVE = "active"
    PAUSED = "paused"
    DEAD = "dead"


@dataclass
class ExecResult:
    """Result of executing a command in a Conway Cloud sandbox."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1


@dataclass
class SandboxInfo:
    """Conway Cloud sandbox metadata."""

    id: str
    status: str = "unknown"
    region: str = ""
    vcpu: int = 1
    memory_mb: int = 512
    disk_gb: int = 5
    terminal_url: str | None = None
    created_at: str = ""


@dataclass
class PricingTier:
    """Conway Cloud pricing tier."""

    name: str
    amount_usd: float
    credits_cents: int


@dataclass
class AutomatonInstance:
    """A managed Automaton instance on Conway Cloud."""

    id: str
    sandbox_id: str
    name: str
    wallet_address: str = ""
    genesis_prompt: str = ""
    status: InstanceStatus = InstanceStatus.PROVISIONING
    survival_tier: SurvivalTier = SurvivalTier.NORMAL
    created_at: str = ""
    last_probe: str = ""
    total_earnings_cents: int = 0
    total_spent_cents: int = 0
    total_turns: int = 0


@dataclass
class TreasuryPolicy:
    """Genesis-level spending policy for Automaton oversight."""

    max_single_transfer_cents: int = 1000  # $10
    hourly_cap_cents: int = 2500  # $25
    daily_cap_cents: int = 10000  # $100
    min_reserve_cents: int = 500  # $5 — auto-topup below this
    auto_topup_amount_cents: int = 500  # $5 per auto-topup
    require_approval_above_cents: int = 2500  # $25 — needs user approval


@dataclass
class ProbeResult:
    """Result from a health/economic probe."""

    probe_type: str
    success: bool
    instance_id: str = ""
    value: float | None = None
    message: str = ""
    alerts: list[str] = field(default_factory=list)


