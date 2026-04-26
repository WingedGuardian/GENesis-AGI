"""Content pipeline data types."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ContentIdea:
    """A content idea captured from any source."""

    id: str
    source: str  # manual | recon | trend | curated
    content: str
    tags: list[str] = field(default_factory=list)
    score: float = 0.0
    status: str = "new"  # new | planned | drafted | published | archived
    platform_target: str | None = None
    created_at: str = ""
    planned_at: str | None = None
    published_at: str | None = None


@dataclass
class PlannedContent:
    """A single scheduled content item within a plan."""

    idea_id: str
    platform: str
    scheduled_date: str
    notes: str = ""


@dataclass
class ContentPlan:
    """A content plan covering a time period."""

    id: str
    period_start: str
    period_end: str
    items: list[PlannedContent] = field(default_factory=list)
    status: str = "draft"  # draft | approved | in_progress | completed
    created_at: str = ""


@dataclass
class Script:
    """A drafted script for a content idea."""

    id: str
    idea_id: str
    content: str
    platform: str
    voice_calibrated: bool = False
    anti_slop_passed: bool = False
    created_at: str = ""
    status: str = "drafted"
    register: str | None = None


@dataclass
class PublishResult:
    """Result of a publish attempt."""

    id: str
    idea_id: str
    platform: str
    content_text: str
    published_at: str | None = None
    status: str = "draft"  # draft | scheduled | published | failed


@dataclass
class ContentMetrics:
    """Metrics for a published content piece."""

    content_id: str
    platform: str
    views: int = 0
    likes: int = 0
    shares: int = 0
    fetched_at: str = ""


@dataclass
class AnalyticsInsight:
    """Aggregated analytics insight for a time period."""

    period: str
    top_performing: list[str] = field(default_factory=list)
    underperforming: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
