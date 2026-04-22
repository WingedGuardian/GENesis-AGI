"""SessionManager — CC session lifecycle: create, track, morning reset, health."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from genesis.cc.types import CCModel, ChannelType, EffortLevel, SessionType
from genesis.db.crud import cc_sessions

logger = logging.getLogger(__name__)

# Callback signatures for session lifecycle hooks
OnSessionStart = Callable[[str, str, str], Awaitable[None]]  # (session_id, session_type, source_tag)
OnSessionEnd = Callable[[str], Awaitable[None]]  # (session_id,)


class SessionManager:
    def __init__(self, *, db, invoker=None, event_bus=None, day_boundary_hour: int = 0):
        self._db = db
        self._invoker = invoker
        self._event_bus = event_bus
        self._day_boundary_hour = day_boundary_hour
        self._on_start_hooks: list[OnSessionStart] = []
        self._on_end_hooks: list[OnSessionEnd] = []

    _PENDING_BOOKMARK_FILE = Path.home() / ".genesis" / "pending_bookmark.json"

    def add_on_start(self, hook: OnSessionStart) -> None:
        """Register a callback fired when a background session is created."""
        self._on_start_hooks.append(hook)

    def add_on_end(self, hook: OnSessionEnd) -> None:
        """Register a callback fired when a session completes or fails."""
        self._on_end_hooks.append(hook)

    async def get_or_create_foreground(
        self,
        *,
        user_id: str,
        channel: ChannelType | str,
        model: CCModel = CCModel.SONNET,
        effort: EffortLevel = EffortLevel.MEDIUM,
        thread_id: str | None = None,
    ) -> dict:
        ch = str(channel)
        existing = await cc_sessions.get_active_foreground(
            self._db, user_id=user_id, channel=ch, thread_id=thread_id,
        )
        if existing:
            return existing

        # Process any pending auto-bookmark from the previous session
        try:
            await self.process_pending_bookmark()
        except Exception:
            logger.error("Failed to process pending bookmark", exc_info=True)
        now = datetime.now(UTC).isoformat()
        sess_id = str(uuid.uuid4())
        await cc_sessions.create(
            self._db,
            id=sess_id,
            session_type="foreground",
            model=str(model),
            effort=str(effort),
            status="active",
            user_id=user_id,
            channel=ch,
            started_at=now,
            last_activity_at=now,
            source_tag="foreground",
            thread_id=thread_id,
        )
        return await cc_sessions.get_by_id(self._db, sess_id)

    async def create_background(
        self,
        *,
        session_type: SessionType,
        model: CCModel,
        effort: EffortLevel = EffortLevel.MEDIUM,
        source_tag: str = "background",
        skill_tags: list[str] | None = None,
        dispatch_mode: str | None = None,
    ) -> dict:
        now = datetime.now(UTC).isoformat()
        sess_id = str(uuid.uuid4())
        meta: dict = {}
        if skill_tags:
            meta["skill_tags"] = skill_tags
        if dispatch_mode:
            meta["dispatch_mode"] = dispatch_mode
        metadata = json.dumps(meta) if meta else None
        await cc_sessions.create(
            self._db,
            id=sess_id,
            session_type=str(session_type),
            model=str(model),
            effort=str(effort),
            status="active",
            started_at=now,
            last_activity_at=now,
            source_tag=source_tag,
            metadata=metadata,
        )
        sess = await cc_sessions.get_by_id(self._db, sess_id)
        for hook in self._on_start_hooks:
            try:
                await hook(sess_id, str(session_type), source_tag)
            except Exception:
                logger.error("Session start hook failed for %s", sess_id[:8], exc_info=True)
        return sess

    async def checkpoint(self, session_id: str) -> None:
        await cc_sessions.update_status(self._db, session_id, status="checkpointed")

    async def complete(
        self,
        session_id: str,
        *,
        cost_usd: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        await cc_sessions.update_status(self._db, session_id, status="completed")
        if cost_usd > 0 or input_tokens > 0:
            try:
                await self._db.execute(
                    """UPDATE cc_sessions
                       SET cost_usd = ?, input_tokens = ?, output_tokens = ?
                       WHERE id = ?""",
                    (cost_usd, input_tokens, output_tokens, session_id),
                )
                await self._db.commit()
            except Exception:
                logger.error("Failed to record CC session cost data", exc_info=True)
        await self._fire_end_hooks(session_id)

    async def fail(self, session_id: str, *, reason: str | None = None) -> None:
        await cc_sessions.update_status(self._db, session_id, status="failed")
        await self._fire_end_hooks(session_id)

    async def _fire_end_hooks(self, session_id: str) -> None:
        for hook in self._on_end_hooks:
            try:
                await hook(session_id)
            except Exception:
                logger.error("Session end hook failed for %s", session_id[:8], exc_info=True)

    async def update_activity(self, session_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        await cc_sessions.update_activity(self._db, session_id, last_activity_at=now)

    async def check_morning_reset(self, *, user_id: str) -> bool:
        """Check if sessions from a previous day boundary should be reset.

        Returns True if there are completed/expired sessions from before
        the current day boundary (suggesting a new day has started).
        """
        now = datetime.now(UTC)
        boundary = now.replace(
            hour=self._day_boundary_hour, minute=0, second=0, microsecond=0,
        )
        if now < boundary:
            boundary -= timedelta(days=1)
        boundary_iso = boundary.isoformat()

        # Check if there are sessions that completed before today's boundary
        rows = await cc_sessions.query_stale(self._db, older_than=boundary_iso)
        return len(rows) > 0

    # Source tags that are safe to auto-expire. Reflection output is persisted
    # in the observations table (not cc_sessions), so expiring session records
    # does NOT lose reflection history or context continuity.
    _EXPIRABLE_SOURCE_TAGS = frozenset({
        "reflection_light", "reflection_micro",
        "reflection_deep", "reflection_strategic",
        "brainstorm", "weekly_assessment", "quality_calibration",
        "code_audit", "infrastructure_monitor",
        "direct_session",
    })
    # Source tags to NEVER auto-expire — currently empty. Only foreground
    # sessions are preserved (handled by the session_type check below).
    _PRESERVE_SOURCE_TAGS: frozenset[str] = frozenset()

    async def cleanup_stale(self, *, max_idle_minutes: int = 60) -> int:
        """Expire stale background sessions per type-specific policy.

        Policy:
        - Foreground: NEVER auto-expire (user might resume)
        - All background types: expire after idle
        """
        cutoff = (datetime.now(UTC) - timedelta(minutes=max_idle_minutes)).isoformat()
        stale = await cc_sessions.query_stale(self._db, older_than=cutoff)
        count = 0
        for row in stale:
            session_type = row.get("session_type", "")
            source_tag = row.get("source_tag", "")

            # NEVER expire foreground sessions
            if session_type == "foreground":
                continue

            # Skip preserved tags (currently empty — foreground handled above)
            if source_tag in self._PRESERVE_SOURCE_TAGS:
                continue

            # Expire if source_tag is in the expirable set, OR if it's a
            # background_task type, OR if unrecognised (default: expire)
            if (source_tag in self._EXPIRABLE_SOURCE_TAGS
                    or session_type == "background_task"
                    or session_type != "foreground"):
                await cc_sessions.update_status(self._db, row["id"], status="expired")
                await self._fire_end_hooks(row["id"])
                count += 1

        if count:
            logger.info("Expired %d stale background sessions (idle > %d min)", count, max_idle_minutes)
        return count

    async def process_pending_bookmark(self) -> str | None:
        """Check for and process a pending auto-bookmark from the previous session.

        Returns the bookmark_id if created, None otherwise.
        Called on foreground session creation to pick up SessionEnd hook data.
        """
        if not self._PENDING_BOOKMARK_FILE.exists():
            return None

        try:
            raw = self._PENDING_BOOKMARK_FILE.read_text()
            data = json.loads(raw)
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to read pending bookmark file", exc_info=True)
            return None
        finally:
            # Always clean up the file to avoid reprocessing
            import contextlib
            with contextlib.suppress(OSError):
                self._PENDING_BOOKMARK_FILE.unlink(missing_ok=True)

        session_id = data.get("session_id", "")
        messages = data.get("messages", [])
        transcript_path = data.get("transcript_path", "")

        if not session_id or not messages:
            return None

        try:
            from genesis.bookmark.manager import BookmarkManager
            from genesis.runtime import GenesisRuntime

            rt = GenesisRuntime.instance()
            if rt.memory_store is None or rt.hybrid_retriever is None:
                return None

            mgr = BookmarkManager(
                memory_store=rt.memory_store,
                hybrid_retriever=rt.hybrid_retriever,
                db=self._db,
            )
            bookmark_id = await mgr.create_micro(
                cc_session_id=session_id,
                context_messages=messages,
                transcript_path=transcript_path,
            )
            logger.info(
                "Auto-created bookmark %s for previous session %s",
                bookmark_id[:8], session_id[:8],
            )
            return bookmark_id
        except Exception:
            logger.error("Failed to create auto-bookmark", exc_info=True)
            return None
