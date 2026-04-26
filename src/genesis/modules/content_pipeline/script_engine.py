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
    """Create the content_scripts table if it doesn't exist, then migrate."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS content_scripts (
            id TEXT PRIMARY KEY,
            idea_id TEXT NOT NULL,
            content TEXT NOT NULL,
            platform TEXT NOT NULL,
            voice_calibrated INTEGER NOT NULL DEFAULT 0,
            anti_slop_passed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'drafted',
            register TEXT
        )
    """)
    # Migrate existing tables that lack the new columns.
    cursor = await db.execute("PRAGMA table_info(content_scripts)")
    existing_cols = {row[1] for row in await cursor.fetchall()}
    if "status" not in existing_cols:
        await db.execute(
            "ALTER TABLE content_scripts ADD COLUMN status TEXT NOT NULL DEFAULT 'drafted'"
        )
    if "register" not in existing_cols:
        await db.execute(
            "ALTER TABLE content_scripts ADD COLUMN register TEXT"
        )
    await db.commit()


def _row_to_script(row: aiosqlite.Row, col_names: list[str] | None = None) -> Script:
    """Convert a database row to a Script.

    Supports both positional (legacy) and named-column access.
    """
    if col_names is not None:
        d = dict(zip(col_names, row, strict=False))
        return Script(
            id=d["id"],
            idea_id=d["idea_id"],
            content=d["content"],
            platform=d["platform"],
            voice_calibrated=bool(d.get("voice_calibrated", 0)),
            anti_slop_passed=bool(d.get("anti_slop_passed", 0)),
            created_at=d.get("created_at", ""),
            status=d.get("status", "drafted"),
            register=d.get("register"),
        )
    # Positional fallback for callers that don't pass col_names.
    return Script(
        id=row[0],
        idea_id=row[1],
        content=row[2],
        platform=row[3],
        voice_calibrated=bool(row[4]),
        anti_slop_passed=bool(row[5]),
        created_at=row[6],
        status=row[7] if len(row) > 7 else "drafted",
        register=row[8] if len(row) > 8 else None,
    )


class ScriptEngine:
    """Drafts and refines content scripts using LLM.

    When ``dispatch_mode`` is True, ``draft_script()`` records the idea
    with ``status='pending_draft'`` and returns immediately — the actual
    drafting is deferred to a CC session with the appropriate voice and
    platform skills loaded.  When False (the default), the engine drafts
    in-process via the Router as before.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        drafter: ContentDrafter | None = None,
        *,
        dispatch_mode: bool = False,
    ) -> None:
        self._db = db
        self._drafter = drafter
        self.dispatch_mode = dispatch_mode

    async def draft_script(
        self,
        idea: ContentIdea,
        platform: str,
        config: dict | None = None,
    ) -> Script:
        """Draft a script for a content idea.

        In dispatch mode, records the idea with status ``pending_draft``
        and returns immediately — the actual drafting is deferred to a
        CC session with voice-master + platform skills.

        Otherwise, uses ContentDrafter for LLM generation when available,
        falling back to the idea content directly.
        """
        register = config.get("register") if config else None

        if self.dispatch_mode:
            return await self._record_pending(idea, platform, register)

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
                    register=register or "professional_peers",
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

        # NOTE: register is stored on the Script for CC session dispatch
        # but has no effect on in-process Router drafting — voice calibration
        # requires the voice-master skill loaded in a CC session.
        voice_calibrated = False

        script = Script(
            id=str(uuid.uuid4()),
            idea_id=idea.id,
            content=content,
            platform=platform,
            voice_calibrated=voice_calibrated,
            anti_slop_passed=False,
            created_at=datetime.now(UTC).isoformat(),
            status="drafted",
            register=register,
        )
        await self._persist_script(script)
        logger.debug("Drafted script %s for idea %s", script.id, idea.id)
        return script

    async def _record_pending(
        self,
        idea: ContentIdea,
        platform: str,
        register: str | None,
    ) -> Script:
        """Record a pending-draft script for later CC session processing."""
        script = Script(
            id=str(uuid.uuid4()),
            idea_id=idea.id,
            content=idea.content,
            platform=platform,
            voice_calibrated=False,
            anti_slop_passed=False,
            created_at=datetime.now(UTC).isoformat(),
            status="pending_draft",
            register=register,
        )
        await self._persist_script(script)
        logger.debug(
            "Recorded pending draft %s for idea %s (dispatch mode)",
            script.id,
            idea.id,
        )
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

        col_names = [desc[0] for desc in cursor.description]
        original = _row_to_script(row, col_names=col_names)
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
            status="refined",
            register=original.register,
        )
        await self._persist_script(refined)
        logger.debug("Refined script %s -> %s", script_id, refined.id)
        return refined

    async def _persist_script(self, script: Script) -> None:
        """Insert a Script into the content_scripts table."""
        await self._db.execute(
            """INSERT INTO content_scripts
               (id, idea_id, content, platform, voice_calibrated,
                anti_slop_passed, created_at, status, register)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                script.id,
                script.idea_id,
                script.content,
                script.platform,
                int(script.voice_calibrated),
                int(script.anti_slop_passed),
                script.created_at,
                script.status,
                script.register,
            ),
        )
        await self._db.commit()


def _resolve_format_target(platform: str):
    """Map platform string to FormatTarget enum."""
    from genesis.content.types import FormatTarget

    mapping: dict[str, FormatTarget] = {
        "telegram": FormatTarget.TELEGRAM,
        "email": FormatTarget.EMAIL,
        "linkedin": FormatTarget.LINKEDIN,
        "twitter": FormatTarget.TWITTER,
        "medium": FormatTarget.MEDIUM,
        "terminal": FormatTarget.TERMINAL,
    }
    return mapping.get(platform.lower(), FormatTarget.GENERIC)
