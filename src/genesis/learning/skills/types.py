"""Skill evolution types — effectiveness analysis, proposals, application."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ChangeSize(StrEnum):
    MINOR = "minor"
    MODERATE = "moderate"
    MAJOR = "major"


class SkillType(StrEnum):
    UPLIFT = "uplift"
    WORKFLOW = "workflow"
    HYBRID = "hybrid"


class SkillTrend(StrEnum):
    IMPROVING = "improving"
    STABLE = "stable"
    DECLINING = "declining"


@dataclass(frozen=True)
class SkillReport:
    skill_name: str
    usage_count: int
    success_count: int
    failure_count: int
    success_rate: float
    baseline_success_rate: float | None = None
    failure_patterns: list[str] = field(default_factory=list)
    trend: SkillTrend = SkillTrend.STABLE
    tools_used: list[str] = field(default_factory=list)
    tools_declared: list[str] = field(default_factory=list)
    sessions_since_last_refined: int = 0


@dataclass(frozen=True)
class SkillProposal:
    skill_name: str
    proposed_content: str
    rationale: str
    change_size: ChangeSize
    confidence: float = 0.7
    failure_patterns_addressed: list[str] = field(default_factory=list)
