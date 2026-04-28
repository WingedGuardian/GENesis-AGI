"""Skill refinement — LLM-driven skill improvement proposals."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from genesis.learning.skills.types import ChangeSize, SkillProposal, SkillReport

if TYPE_CHECKING:
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

# Call site 33: skill_refiner
_CALL_SITE = "33_skill_refiner"


class SkillRefiner:
    """Proposes skill improvements via LLM analysis."""

    async def propose(
        self,
        report: SkillReport,
        current_content: str,
        *,
        router: Router | None = None,
    ) -> SkillProposal | None:
        """Generate a skill improvement proposal.

        Uses LLM (call site 33) to analyze the skill report and current
        content, then propose changes. Returns None if the LLM call fails
        or no improvements are warranted.
        """
        if router is None:
            logger.warning("No router available for skill refinement")
            return None

        prompt = self._build_prompt(report, current_content)

        try:
            result = await router.route_call(
                call_site_id=_CALL_SITE,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception:
            logger.exception("Skill refiner LLM call failed")
            return None

        return self._parse_response(report.skill_name, result.content)

    def _build_prompt(self, report: SkillReport, current_content: str) -> str:
        lines = len(current_content.splitlines())
        notes: list[str] = []

        if lines > 500:  # noqa: PLR2004
            notes.append(
                "IMPORTANT: This skill is over 500 lines. Consider restructuring "
                "into a shorter SKILL.md with references/ for detailed content."
            )

        # Encourage examples if missing
        if "## Example" not in current_content and "## Examples: Not Required" not in current_content:
            notes.append(
                "NOTE: This skill lacks an Examples section. Include at least one "
                "realistic input→output example in your proposed content to anchor "
                "expected behavior and prevent output drift."
            )

        notes_block = "\n\n".join(notes)
        if notes_block:
            notes_block = f"\n\n{notes_block}"

        baseline_str = (
            f"\n- Baseline: {report.baseline_success_rate:.1%}"
            if report.baseline_success_rate is not None
            else ""
        )

        return (
            f"Analyze this skill and propose improvements.\n\n"
            f"## Skill Report\n"
            f"- Name: {report.skill_name}\n"
            f"- Usage: {report.usage_count} sessions\n"
            f"- Success rate: {report.success_rate:.1%}"
            f"{baseline_str}\n"
            f"- Trend: {report.trend.value}\n"
            f"- Failure patterns: {', '.join(report.failure_patterns) or 'none'}\n"
            f"- Tools used: {', '.join(report.tools_used) or 'none'}\n"
            f"- Tools declared: {', '.join(report.tools_declared) or 'none'}\n"
            f"{notes_block}\n\n"
            f"## Current Content ({lines} lines)\n```\n{current_content[:3000]}\n```\n\n"
            f"Respond with JSON:\n"
            f'{{"proposed_content": "...", "rationale": "...", '
            f'"change_size": "minor|moderate|major", '
            f'"confidence": 0.0-1.0, '
            f'"failure_patterns_addressed": ["..."]}}'
        )

    def _parse_response(self, skill_name: str, text: str) -> SkillProposal | None:
        try:
            match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
            raw = match.group(1) if match else text
            data = json.loads(raw)
        except (json.JSONDecodeError, AttributeError):
            logger.warning("Failed to parse skill refiner response")
            return None

        change_size_str = data.get("change_size", "minor")
        try:
            change_size = ChangeSize(change_size_str)
        except ValueError:
            change_size = ChangeSize.MINOR

        proposed = data.get("proposed_content", "")
        if not proposed:
            return None

        return SkillProposal(
            skill_name=skill_name,
            proposed_content=proposed,
            rationale=data.get("rationale", ""),
            change_size=change_size,
            confidence=float(data.get("confidence", 0.7)),
            failure_patterns_addressed=data.get("failure_patterns_addressed", []),
        )
