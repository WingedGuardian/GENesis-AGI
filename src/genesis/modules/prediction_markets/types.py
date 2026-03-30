"""Data types for the prediction markets module."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum


class MarketStatus(StrEnum):
    """Status of a prediction market."""

    OPEN = "open"
    CLOSED = "closed"
    RESOLVED = "resolved"


class BetStatus(StrEnum):
    """Status of a placed or considered bet."""

    CONSIDERED = "considered"  # Analyzed but not placed
    PLACED = "placed"  # Bet placed, awaiting resolution
    WON = "won"
    LOST = "lost"
    VOID = "void"  # Market voided


class MarketSource(StrEnum):
    """Supported prediction market platforms."""

    POLYMARKET = "polymarket"
    KALSHI = "kalshi"
    METACULUS = "metaculus"
    MANIFOLD = "manifold"
    CUSTOM = "custom"


@dataclass
class Market:
    """A prediction market opportunity."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    source: MarketSource = MarketSource.CUSTOM
    title: str = ""
    description: str = ""
    url: str = ""
    current_price: float = 0.5  # Market-implied probability (0-1)
    volume: float = 0.0
    liquidity: float = 0.0
    close_date: str = ""  # ISO format
    resolution_date: str = ""
    status: MarketStatus = MarketStatus.OPEN
    categories: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    fetched_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ"))


@dataclass
class Estimate:
    """A probability estimate with reasoning."""

    market_id: str = ""
    estimated_probability: float = 0.5
    market_price: float = 0.5
    edge: float = 0.0  # estimated_probability - market_price
    confidence_in_estimate: float = 0.5  # How confident in OUR estimate (meta)
    reasoning: str = ""
    signals_used: list[str] = field(default_factory=list)
    bias_checks: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ"))


@dataclass
class BetRecord:
    """A bet placed or considered on a prediction market."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    market_id: str = ""
    market_title: str = ""
    source: MarketSource = MarketSource.CUSTOM
    status: BetStatus = BetStatus.CONSIDERED
    # Probabilities at time of bet
    market_price_at_entry: float = 0.5
    genesis_estimate: float = 0.5
    user_estimate: float | None = None
    # Position
    position_size: float = 0.0  # Dollar amount
    direction: str = "yes"  # "yes" or "no"
    # Resolution
    actual_outcome: float | None = None  # 1.0 (yes) or 0.0 (no)
    pnl: float | None = None
    brier_score: float | None = None
    # Reasoning
    reasoning: str = ""
    category: str = ""  # e.g., "politics", "crypto", "economics"
    # Timestamps
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ"))
    resolved_at: str = ""
