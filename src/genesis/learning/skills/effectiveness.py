"""Skill effectiveness analyzer — queries cc_sessions for per-skill metrics."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from genesis.db.crud import cc_sessions
from genesis.learning.skills.types import SkillReport, SkillTrend

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

_TREND_WINDOW = 10


class SkillEffectivenessAnalyzer:
    """Computes per-skill effectiveness metrics from cc_sessions data."""

    async def analyze(self, db: aiosqlite.Connection, skill_name: str) -> SkillReport:
        """Analyze effectiveness of a single skill."""
        # query_by_skill_tag does the cheap `metadata LIKE '%"skill"%'`
        # prefilter; on its own that also matches the skill name appearing in
        # OTHER metadata fields (e.g. a `profile` value — "research" is both a
        # profile and a skill name). Keep only sessions that actually list this
        # skill in `skill_tags`.
        candidate_rows = await cc_sessions.query_by_skill_tag(db, skill_tag=skill_name)
        rows = [
            r
            for r in candidate_rows
            if skill_name in _parse_metadata(r.get("metadata")).get("skill_tags", [])
        ]

        success_count = sum(1 for r in rows if r["status"] == "completed")
        failure_count = sum(1 for r in rows if r["status"] == "failed")
        usage_count = len(rows)
        success_rate = success_count / usage_count if usage_count else 0.0

        # Extract failure patterns from metadata
        failure_patterns: list[str] = []
        tools_used: set[str] = set()
        for row in rows:
            meta = _parse_metadata(row.get("metadata"))
            if row["status"] == "failed" and meta.get("failure_reason"):
                pattern = meta["failure_reason"]
                if pattern not in failure_patterns:
                    failure_patterns.append(pattern)
            for tool in meta.get("tools_used", []):
                tools_used.add(tool)

        # Trend computation
        trend = self._compute_trend(rows)

        # Baseline: sessions WITHOUT this skill tag for same session_type
        baseline = await self._compute_baseline(db, skill_name, rows)

        # Tools declared from metadata of any session
        tools_declared: set[str] = set()
        for row in rows:
            meta = _parse_metadata(row.get("metadata"))
            for tool in meta.get("tools_declared", []):
                tools_declared.add(tool)

        return SkillReport(
            skill_name=skill_name,
            usage_count=usage_count,
            success_count=success_count,
            failure_count=failure_count,
            success_rate=success_rate,
            baseline_success_rate=baseline,
            failure_patterns=failure_patterns,
            trend=trend,
            tools_used=sorted(tools_used),
            tools_declared=sorted(tools_declared),
        )

    async def analyze_all(self, db: aiosqlite.Connection) -> list[SkillReport]:
        """Analyze all known skills."""
        from genesis.learning.skills.wiring import list_available_skills

        reports = []
        for skill_name in list_available_skills():
            report = await self.analyze(db, skill_name)
            reports.append(report)
        return reports

    def needs_review(self, report: SkillReport) -> bool:
        """Determine if a skill needs review based on its report."""
        # Below baseline
        if (
            report.baseline_success_rate is not None
            and report.success_rate < report.baseline_success_rate
        ):
            return True

        # Declining trend
        if report.trend == SkillTrend.DECLINING:
            return True

        # Tools mismatch: used tools not declared
        return bool(
            report.tools_declared
            and report.tools_used
            and not set(report.tools_used).issubset(set(report.tools_declared))
        )

    def _compute_trend(self, rows: list[dict]) -> SkillTrend:
        """Compute trend from recent sessions."""
        recent = rows[:_TREND_WINDOW]
        if len(recent) < 4:  # noqa: PLR2004
            return SkillTrend.STABLE

        mid = len(recent) // 2
        newer = recent[:mid]
        older = recent[mid:]

        newer_rate = sum(1 for r in newer if r["status"] == "completed") / len(newer)
        older_rate = sum(1 for r in older if r["status"] == "completed") / len(older)

        threshold = 0.15
        if newer_rate > older_rate + threshold:
            return SkillTrend.IMPROVING
        if newer_rate < older_rate - threshold:
            return SkillTrend.DECLINING
        return SkillTrend.STABLE

    async def _compute_baseline(
        self,
        db: aiosqlite.Connection,
        skill_name: str,
        skill_rows: list[dict],
    ) -> float | None:
        """Compute baseline success rate for sessions without this skill."""
        if not skill_rows:
            return None

        # Get session types used with this skill
        session_types = {r["session_type"] for r in skill_rows if r.get("session_type")}
        if not session_types:
            return None

        same_type_rows = await cc_sessions.get_by_session_types(db, session_types)
        # Baseline = same-type sessions that do NOT use this skill. Match on
        # skill_tags membership (not a loose substring) for the same reason as
        # analyze() — a session whose `profile` equals the skill name is not a
        # user of the skill.
        baseline_rows = [
            r
            for r in same_type_rows
            if skill_name not in _parse_metadata(r.get("metadata")).get("skill_tags", [])
        ]
        if not baseline_rows:
            return None

        completed = sum(1 for r in baseline_rows if r.get("status") == "completed")
        return completed / len(baseline_rows)


def _parse_metadata(raw: str | None) -> dict:
    """Safely parse metadata JSON."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
