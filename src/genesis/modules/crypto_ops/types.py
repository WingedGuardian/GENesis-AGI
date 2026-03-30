"""Data types for crypto token operations."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum


class Chain(StrEnum):
    """Supported blockchain networks."""

    SOLANA = "solana"
    BASE = "base"


class NarrativeStatus(StrEnum):
    """Lifecycle of a detected narrative."""

    EMERGING = "emerging"
    BUILDING = "building"
    PEAKING = "peaking"
    FADING = "fading"


class LaunchStatus(StrEnum):
    """Status of a token launch."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    DEPLOYED = "deployed"
    MONITORING = "monitoring"
    EXITED = "exited"
    FAILED = "failed"


@dataclass
class Narrative:
    """A detected crypto narrative with momentum tracking."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    description: str = ""
    status: NarrativeStatus = NarrativeStatus.EMERGING
    momentum_score: float = 0.0  # 0-1, higher = more energy
    signals: list[str] = field(default_factory=list)
    related_tokens: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    first_seen: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ"))
    last_updated: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ"))
    metadata: dict = field(default_factory=dict)


@dataclass
class LaunchPackage:
    """A prepared token launch package for user approval."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    narrative_id: str = ""
    chain: Chain = Chain.SOLANA
    token_name: str = ""
    token_ticker: str = ""
    description: str = ""
    initial_liquidity: float = 0.0
    deployment_method: str = ""  # e.g., "pump.fun", "anchor", "factory"
    status: LaunchStatus = LaunchStatus.PROPOSED
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ"))


@dataclass
class TokenPosition:
    """An active or closed token position."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    launch_id: str = ""
    chain: Chain = Chain.SOLANA
    token_address: str = ""
    token_name: str = ""
    token_ticker: str = ""
    # Position data
    entry_price: float = 0.0
    current_price: float = 0.0
    exit_price: float | None = None
    holder_count: int = 0
    volume_24h: float = 0.0
    liquidity: float = 0.0
    # P&L
    invested: float = 0.0
    current_value: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    # Narrative health
    narrative_momentum: float = 0.0
    # Timestamps
    deployed_at: str = ""
    last_checked: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ"))
    exited_at: str = ""
