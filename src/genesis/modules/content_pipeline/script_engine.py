"""ScriptEngine — draft and refine content scripts."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from genesis.modules.content_pipeline.types import ContentIdea, Script

if TYPE_CHECKING:
    import aiosqlite

    from genesis.content.drafter import ContentDrafter

logger = logging.getLogger(__name__)


async def ensure_table(db: aiosqlite.Connection) -> None:
    """Create the content_scripts table if it doesn't exist."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS content_scripts (
            id TEXT PRIMARY KEY,
            idea_id TEXT NOT NULL,
            content TEXT NOT NULL,
            platform TEXT NOT NULL,
            voice_calibrated INTEGER NOT NULL DEFAULT 0,
            anti_slop_passed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    await db.commit()


def _row_to_script(row: aiosqlite.Row) -> Script:
    """Convert a database row to a Script."""
    return Script(
        id=row[0],
        idea_id=row[1],
        content=row[2],
        platform=row[3],
        voice_calibrated=bool(row[4]),
        anti_slop_passed=bool(row[5]),
        created_at=row[6],
    )


class ScriptEngine:
    """Drafts and refines content scripts using LLM."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        drafter: ContentDrafter | None = None,
    ) -> None:
        self._db = db
        self._drafter = drafter

    async def draft_script(
        self,
        idea: ContentIdea,
        platform: str,
        config: dict | None = None,
    ) -> Script:
        """Draft a script for a content idea.

        Uses ContentDrafter for LLM generation when available,
        otherwise falls back to the idea content directly.
        """
        content = idea.content

        if self._drafter is not None:
            try:
                from genesis.content.types import DraftRequest

                target = _resolve_format_target(platform)
                request = DraftRequest(
                    topic=idea.content,
                    context=f"Tags: {', '.join(idea.tags)}" if idea.tags else "",
                    target=target,
                    tone=config.get("tone", "professional") if config else "professional",
                    max_length=config.get("max_length") if config else None,
                )
                result = await self._drafter.draft(request)
                content = result.raw_draft or result.content.text
            except Exception:
                logger.warning(
                    "LLM draft failed for idea %s, using raw content",
                    idea.id,
                    exc_info=True,
                )

        # Check for voice-master skill availability.
        # NOTE: This flag indicates the skill is *available*, not that the
        # content was actually voice-calibrated. Voice calibration happens at
        # the CC session level when the skill is loaded as a playbook.
        voice_calibrated = False

        script = Script(
            id=str(uuid.uuid4()),
            idea_id=idea.id,
            content=content,
            platform=platform,
            voice_calibrated=voice_calibrated,
            anti_slop_passed=False,
            created_at=datetime.now(UTC).isoformat(),
        )
        await self._db.execute(
            """INSERT INTO content_scripts
               (id, idea_id, content, platform, voice_calibrated, anti_slop_passed, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                script.id,
                script.idea_id,
                script.content,
                script.platform,
                int(script.voice_calibrated),
                int(script.anti_slop_passed),
                script.created_at,
            ),
        )
        await self._db.commit()
        logger.debug("Drafted script %s for idea %s", script.id, idea.id)
        return script

    async def refine_script(self, script_id: str, feedback: str) -> Script:
        """Refine an existing script based on feedback.

        Returns a new Script (the refined version). The original is kept.
        """
        cursor = await self._db.execute(
            "SELECT * FROM content_scripts WHERE id = ?", (script_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            msg = f"Script {script_id} not found"
            raise ValueError(msg)

        original = _row_to_script(row)
        refined_content = original.content

        if self._drafter is not None:
            try:
                from genesis.content.types import DraftRequest

                target = _resolve_format_target(original.platform)
                request = DraftRequest(
                    topic=f"Refine this content based on feedback.\n\nOriginal:\n{original.content}\n\nFeedback:\n{feedback}",
                    target=target,
                )
                result = await self._drafter.draft(request)
                refined_content = result.raw_draft or result.content.text
            except Exception:
                logger.warning(
                    "LLM refinement failed for script %s",
                    script_id,
                    exc_info=True,
                )

        refined = Script(
            id=str(uuid.uuid4()),
            idea_id=original.idea_id,
            content=refined_content,
            platform=original.platform,
            voice_calibrated=original.voice_calibrated,
            anti_slop_passed=False,
            created_at=datetime.now(UTC).isoformat(),
        )
        await self._db.execute(
            """INSERT INTO content_scripts
               (id, idea_id, content, platform, voice_calibrated, anti_slop_passed, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                refined.id,
                refined.idea_id,
                refined.content,
                refined.platform,
                int(refined.voice_calibrated),
                int(refined.anti_slop_passed),
                refined.created_at,
            ),
        )
        await self._db.commit()
        logger.debug("Refined script %s -> %s", script_id, refined.id)
        return refined


def _resolve_format_target(platform: str):
    """Map platform string to FormatTarget enum."""
    from genesis.content.types import FormatTarget

    mapping: dict[str, FormatTarget] = {
        "telegram": FormatTarget.TELEGRAM,
        "email": FormatTarget.EMAIL,
        "linkedin": FormatTarget.LINKEDIN,
        "twitter": FormatTarget.TWITTER,
        "terminal": FormatTarget.TERMINAL,
    }
    return mapping.get(platform.lower(), FormatTarget.GENERIC)
