"""Skill applicator — applies or stages skill improvement proposals."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from genesis.learning.skills.types import ChangeSize, SkillProposal
from genesis.learning.skills.validator import SkillValidator

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

# GROUNDWORK(skill-autonomy-graduation): autonomy_state category skill_evolution, starts L2
_DEFAULT_AUTONOMY_LEVEL = 2


class SkillApplicator:
    """Applies or stages skill proposals based on change size and autonomy level."""

    def __init__(self, *, autonomy_level: int = _DEFAULT_AUTONOMY_LEVEL):
        self._autonomy_level = autonomy_level
        self._validator = SkillValidator()

    async def apply(
        self,
        proposal: SkillProposal,
        db: aiosqlite.Connection,
        *,
        router: object | None = None,
        current_content: str | None = None,
    ) -> dict:
        """Apply or stage a skill proposal.

        At L2: MINOR auto-applies (if validation passes), MODERATE+ staged for review.

        Args:
            proposal: The skill proposal to apply or stage.
            db: Database connection.
            router: LLM router for MODERATE+ validation (optional).
            current_content: Current SKILL.md content for consistency checking.
        """
        now = datetime.now(UTC).isoformat()

        if proposal.change_size == ChangeSize.MINOR and self._autonomy_level >= 2:  # noqa: PLR2004
            # Validate before auto-applying
            validation = self._validator.validate(proposal, current_content)

            if not validation.passed:
                logger.info(
                    "MINOR proposal for %s failed validation (%s), staging for review",
                    proposal.skill_name,
                    validation.blocking_failures,
                )
                # Fall through to staging path
                return await self._stage(
                    proposal, db, validated=False, now=now,
                    validation_detail=validation.test_results,
                )

            # Auto-apply MINOR changes that pass validation
            from genesis.learning.skills.wiring import get_skill_path

            path = get_skill_path(proposal.skill_name)
            if path is None:
                return {"action": "failed", "reason": "skill not found"}

            path.write_text(proposal.proposed_content, encoding="utf-8")

            # Log observation (include validation warnings if any)
            from genesis.db.crud import observations

            obs_content = {
                "skill_name": proposal.skill_name,
                "change_size": proposal.change_size.value,
                "rationale": proposal.rationale,
                "confidence": proposal.confidence,
            }
            if validation.warnings:
                obs_content["validation_warnings"] = validation.warnings

            await observations.create(
                db,
                id=str(uuid.uuid4()),
                source="skill_evolution",
                type="skill_evolution",
                content=json.dumps(obs_content),
                priority="medium",
                created_at=now,
            )

            logger.info("Auto-applied MINOR skill change to %s", proposal.skill_name)
            return {"action": "applied", "skill_name": proposal.skill_name}

        else:
            # Stage MODERATE+ for review (or any change if autonomy < 2)
            validated = False
            if proposal.change_size != ChangeSize.MINOR and router:
                validated = await self._llm_validate(proposal, router=router)

            return await self._stage(proposal, db, validated=validated, now=now)

    async def _stage(
        self,
        proposal: SkillProposal,
        db: aiosqlite.Connection,
        *,
        validated: bool,
        now: str,
        validation_detail: dict[str, str] | None = None,
    ) -> dict:
        """Stage a proposal for user review."""
        from genesis.db.crud import observations

        content = {
            "skill_name": proposal.skill_name,
            "change_size": proposal.change_size.value,
            "rationale": proposal.rationale,
            "confidence": proposal.confidence,
            "validated": validated,
            "proposed_content_preview": proposal.proposed_content[:500],
        }
        if validation_detail:
            content["validation_detail"] = validation_detail

        await observations.create(
            db,
            id=str(uuid.uuid4()),
            source="skill_evolution",
            type="skill_proposal",
            content=json.dumps(content),
            priority="high" if proposal.change_size == ChangeSize.MAJOR else "medium",
            created_at=now,
        )

        logger.info(
            "Staged %s skill proposal for %s (validated=%s)",
            proposal.change_size.value,
            proposal.skill_name,
            validated,
        )
        return {
            "action": "staged",
            "skill_name": proposal.skill_name,
            "validated": validated,
        }

    async def _llm_validate(self, proposal: SkillProposal, *, router: object) -> bool:
        """Validate a MODERATE+ proposal with a second LLM call."""
        prompt = (
            f"Review this skill change proposal and determine if it should be applied.\n\n"
            f"Skill: {proposal.skill_name}\n"
            f"Change size: {proposal.change_size.value}\n"
            f"Rationale: {proposal.rationale}\n"
            f"Confidence: {proposal.confidence}\n"
            f"Content preview:\n```\n{proposal.proposed_content[:2000]}\n```\n\n"
            f'Respond with JSON: {{"approved": true/false, "reason": "..."}}'
        )

        try:
            result = await router.route_call(  # type: ignore[union-attr]
                call_site_id="33_skill_refiner",
                messages=[{"role": "user", "content": prompt}],
            )
            data = json.loads(result.content)
            return bool(data.get("approved", False))
        except Exception:
            logger.warning(
                "Validation LLM call failed, defaulting to not validated",
                exc_info=True,
            )
            return False
