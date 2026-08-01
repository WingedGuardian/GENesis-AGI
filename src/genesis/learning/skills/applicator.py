"""Skill applicator — stages skill improvement proposals for human review.

**Propose-only.** Autonomous skill-evolution NEVER writes a skill file. Every
proposal — MINOR or larger — is *staged* for a human / CC session to review and
apply through the normal worktree/PR flow. The structural validator result, the
MODERATE+ apply-recommendation, and the shadow Critic verdict all ride the staged
proposal (with the full proposed content) so a reviewer can decide.

Gated auto-apply is deferred to the future WS1 ``enforce`` mode; the autonomy
level, validator wiring, and provenance capture below are retained for that path.
(Origin: 2026-08-01 — the auto-apply seam repeatedly landed principle-violating
edits that the shadow Critic flagged but could not block.)
"""

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

# GROUNDWORK(skill-autonomy-graduation): autonomy_state category skill_evolution,
# starts L2. Retained for the future WS1 `enforce` mode that re-enables gated
# auto-apply; under propose-only every change size is staged for human review.
_DEFAULT_AUTONOMY_LEVEL = 2


class SkillApplicator:
    """Stages skill proposals for human review (propose-only)."""

    def __init__(self, *, autonomy_level: int = _DEFAULT_AUTONOMY_LEVEL):
        self._autonomy_level = autonomy_level
        self._validator = SkillValidator()

    @staticmethod
    def _build_modification_metadata(proposal: SkillProposal) -> dict:
        """Provenance recorded with a staged proposal — the triggering signals
        (failure_patterns_addressed + the SkillReport-derived trace), so an
        eventually-applied edit can be understood and rolled back."""
        return {
            "skill_name": proposal.skill_name,
            "change_size": proposal.change_size.value,
            "confidence": proposal.confidence,
            "failure_patterns_addressed": proposal.failure_patterns_addressed,
            "provenance_trace": proposal.provenance_trace,
        }

    async def apply(
        self,
        proposal: SkillProposal,
        db: aiosqlite.Connection,
        *,
        router: object | None = None,
        current_content: str | None = None,
    ) -> dict:
        """Stage a skill proposal for human review — never writes a skill file.

        Propose-only: MINOR and larger alike are staged. Autonomous auto-apply is
        retired here; it returns via the future WS1 ``enforce`` mode.

        Args:
            proposal: The skill proposal to stage.
            db: Database connection.
            router: LLM router for the MODERATE+ apply-recommendation + the Critic.
            current_content: Current SKILL.md content (Critic baseline + validation).
        """
        now = datetime.now(UTC).isoformat()

        # Structural validation — informs the reviewer (and the future enforce gate).
        validation = self._validator.validate(proposal, current_content)
        # `validated` = passed the applicable gate: the structural validator for
        # MINOR, the LLM apply-recommendation for MODERATE+ (False when there is no
        # router to run it). Same semantics as before, now on the staging path.
        if proposal.change_size == ChangeSize.MINOR:
            validated = validation.passed
        elif router is not None:
            validated = await self._llm_validate(proposal, router=router)
        else:
            validated = False

        # Shadow Critic (WS1): screens for self-modification pathologies. Under
        # propose-only this is pure ENRICHMENT — it logs the WS1 shadow observation
        # AND its verdict rides the staged proposal for the reviewer. It never
        # gates anything (nothing auto-applies).
        critic = await self._run_critic(
            proposal, db, router=router, current_content=current_content, now=now
        )

        return await self._stage(
            proposal,
            db,
            validated=validated,
            now=now,
            validation_detail=None if validation.passed else validation.test_results,
            validation_warnings=(validation.warnings or None),
            critic=critic,
        )

    async def _run_critic(
        self,
        proposal: SkillProposal,
        db: aiosqlite.Connection,
        *,
        router: object | None,
        current_content: str | None,
        now: str,
    ) -> dict | None:
        """Run the shadow Critic, log the WS1 observation, return the verdict.

        Best-effort — a judge problem must never disturb staging. Returns the
        verdict dict (to ride the proposal) or ``None`` (gate off / no router /
        no baseline / judge unavailable).
        """
        from genesis.db.crud import observations

        try:
            from genesis.learning.skills.skill_edit_critic import run_critic

            critic = await run_critic(
                current_content=current_content, proposal=proposal, router=router
            )
        except Exception:
            logger.warning(
                "skill-edit Critic failed for %s (staging unaffected)",
                proposal.skill_name,
                exc_info=True,
            )
            return None
        if critic is None:
            return None
        try:
            await observations.create(
                db,
                id=str(uuid.uuid4()),
                source="skill_evolution_gate",
                type="skill_edit_critic",
                category=proposal.skill_name,  # indexed dampening/dedup key
                content=json.dumps(critic),
                priority=("high" if critic.get("verdict") == "flagged" else "low"),
                created_at=now,
            )
        except Exception:
            logger.warning(
                "failed to log skill_edit_critic observation for %s",
                proposal.skill_name,
                exc_info=True,
            )
        return critic

    async def _stage(
        self,
        proposal: SkillProposal,
        db: aiosqlite.Connection,
        *,
        validated: bool,
        now: str,
        validation_detail: dict[str, str] | None = None,
        validation_warnings: list[str] | None = None,
        critic: dict | None = None,
    ) -> dict:
        """Stage a proposal for human review — persists the FULL proposed content.

        The full ``proposed_content`` is stored so the proposal is applicable by a
        reviewer / CC session; a short preview + the Critic verdict are what a
        surfacing view should show.
        """
        from genesis.db.crud import observations

        content = {
            "skill_name": proposal.skill_name,
            "change_size": proposal.change_size.value,
            "rationale": proposal.rationale,
            "confidence": proposal.confidence,
            "validated": validated,
            # Full body — required so the staged proposal can actually be applied.
            "proposed_content": proposal.proposed_content,
            "proposed_content_preview": proposal.proposed_content[:500],
            "provenance": self._build_modification_metadata(proposal),
        }
        if validation_detail:
            content["validation_detail"] = validation_detail
        if validation_warnings:
            content["validation_warnings"] = validation_warnings
        if critic is not None:
            # The reviewer's key signal — the pathology screen verdict.
            content["critic"] = {
                "verdict": critic.get("verdict"),
                "score": critic.get("score"),
                "rationale": critic.get("rationale"),
                "pathologies": critic.get("pathologies"),
            }

        flagged = bool(critic and critic.get("verdict") == "flagged")
        priority = "high" if (proposal.change_size == ChangeSize.MAJOR or flagged) else "medium"

        await observations.create(
            db,
            id=str(uuid.uuid4()),
            source="skill_evolution",
            type="skill_proposal",
            category=proposal.skill_name,  # indexed dampening/dedup key
            content=json.dumps(content),
            priority=priority,
            created_at=now,
        )

        logger.info(
            "Staged %s skill proposal for %s (validated=%s, critic=%s)",
            proposal.change_size.value,
            proposal.skill_name,
            validated,
            (critic or {}).get("verdict"),
        )
        return {
            "action": "staged",
            "skill_name": proposal.skill_name,
            "validated": validated,
            "critic_verdict": (critic or {}).get("verdict"),
        }

    async def _llm_validate(self, proposal: SkillProposal, *, router: object) -> bool:
        """Validate a MODERATE+ proposal with a second LLM call (apply-recommendation)."""
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
            # 33_skill_refiner — second caller of this site (besides skills/refiner.py:18).
            # Also aliased by bookmark_enrichment via router.py:405.
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
