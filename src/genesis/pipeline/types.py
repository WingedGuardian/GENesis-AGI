"""Pipeline data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum


class Tier(IntEnum):
    COLLECTION = 0
    TRIAGE = 1
    ANALYSIS = 2
    JUDGMENT = 3


class SignalStatus(StrEnum):
    RAW = "raw"
    TRIAGED = "triaged"
    ANALYZED = "analyzed"
    ACTIONABLE = "actionable"
    DISCARDED = "discarded"


@dataclass
class ResearchSignal:
    """A piece of information flowing through the pipeline."""

    id: str
    source: str  # e.g., "searxng", "polymarket_api", "reddit"
    profile_name: str  # which research profile generated this
    content: str  # the actual data
    url: str | None = None
    tier: Tier = Tier.COLLECTION
    status: SignalStatus = SignalStatus.RAW
    relevance_score: float = 0.0  # 0-1, set by triage
    confidence: float = 0.0  # 0-1, set by analysis
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    collected_at: str = ""
    promoted_at: str | None = None


@dataclass
class CollectorResult:
    """Result from a single collector run."""

    collector_name: str
    signals: list[ResearchSignal]
    errors: list[str] = field(default_factory=list)


@dataclass
class PipelineRunResult:
    """Result from a full pipeline cycle."""

    profile_name: str
    tier0_collected: int = 0
    tier1_survived: int = 0
    tier2_survived: int = 0
    tier3_surfaced: int = 0
    discarded: int = 0
    errors: list[str] = field(default_factory=list)
