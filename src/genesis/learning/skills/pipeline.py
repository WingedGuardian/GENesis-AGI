"""Skill evolution pipeline — orchestrates analysis, refinement, and application.

Wires together SkillEffectivenessAnalyzer, SkillRefiner, and SkillApplicator
into a single callable pipeline. Runs on a weekly schedule as a backup trigger
and can be invoked on-demand (e.g., by the failure detector via propose_for_skill).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from genesis.learning.skills.applicator import SkillApplicator
from genesis.learning.skills.effectiveness import SkillEffectivenessAnalyzer
from genesis.learning.skills.refiner import SkillRefiner
from genesis.learning.skills.types import ChangeSize, SkillProposal
from genesis.learning.skills.wiring import load_skill

if TYPE_CHECKING:
    import aiosqlite

    from genesis.routing.router import Router

logger = logging.getLogger(__name__)


class SkillEvolutionPipeline:
    """Orchestrates the full skill evolution lifecycle.

    Flow: analyze_all() → filter needs_review → propose() → apply().
    """

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        router: Router,
        outreach_fn: object | None = None,
    ) -> None:
        self._db = db
        self._router = router
        self._outreach_fn = outreach_fn
        self._analyzer = SkillEffectivenessAnalyzer()
        self._refiner = SkillRefiner()
        self._applicator = SkillApplicator()

    async def run(self) -> dict:
        """Run the full pipeline: analyze → refine → apply.

        Returns summary dict with counts of analyzed, proposed, applied, staged.
        """
        reports = await self._analyzer.analyze_all(self._db)
        needs_review = [r for r in reports if self._analyzer.needs_review(r)]

        proposed = 0
        applied = 0
        staged = 0

        for report in needs_review:
            content = load_skill(report.skill_name)
            if content is None:
                logger.warning("Skill %s has no SKILL.md, skipping", report.skill_name)
                continue

            proposal = await self._refiner.propose(
                report, content, router=self._router,
            )
            if proposal is None:
                continue
            proposed += 1

            result = await self._applicator.apply(
                proposal, self._db, router=self._router,
            )

            if result["action"] == "applied":
                applied += 1
            elif result["action"] == "staged":
                staged += 1
                # Notify via outreach for MODERATE+ proposals
                if proposal.change_size != ChangeSize.MINOR:
                    await self._notify_proposal(proposal)

        summary = {
            "analyzed": len(reports),
            "needs_review": len(needs_review),
            "proposed": proposed,
            "applied": applied,
            "staged": staged,
        }
        logger.info("Skill evolution pipeline: %s", summary)
        return summary

    async def propose_for_skill(
        self,
        skill_name: str,
        failure_context: str = "",
    ) -> dict | None:
        """Targeted proposal for a specific skill after a failure.

        Called by the failure detector when a procedure linked to a skill
        accumulates enough failures to warrant a review.
        """
        report = await self._analyzer.analyze(self._db, skill_name)
        content = load_skill(skill_name)
        if content is None:
            return None

        proposal = await self._refiner.propose(
            report, content, router=self._router,
        )
        if proposal is None:
            return None

        result = await self._applicator.apply(
            proposal, self._db, router=self._router,
        )

        if result["action"] == "staged" and proposal.change_size != ChangeSize.MINOR:
            await self._notify_proposal(proposal)

        return result

    async def _notify_proposal(self, proposal: SkillProposal) -> None:
        """Send outreach notification for a staged proposal."""
        if self._outreach_fn is None:
            return

        try:
            from genesis.outreach.types import OutreachCategory, OutreachRequest

            request = OutreachRequest(
                category=OutreachCategory.ALERT,
                topic=f"Skill improvement: {proposal.skill_name}",
                context=(
                    f"Skill '{proposal.skill_name}' has a {proposal.change_size.value} "
                    f"improvement proposal: {proposal.rationale[:200]}"
                ),
                salience_score=0.6,
                signal_type="skill_proposal",
            )
            await self._outreach_fn(request)
        except Exception:
            logger.warning(
                "Failed to send outreach for skill proposal %s",
                proposal.skill_name,
                exc_info=True,
            )
