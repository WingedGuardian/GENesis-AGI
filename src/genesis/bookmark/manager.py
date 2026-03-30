"""BookmarkManager — creates, searches, and enriches session bookmarks.

Bookmarks are stored as episodic memories (memory_type="session_bookmark")
in the existing MemoryStore + HybridRetriever pipeline. A lightweight
session_bookmarks SQLite table provides fast lookups by session ID.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import aiosqlite

from genesis.db.crud import session_bookmarks as bookmark_crud
from genesis.memory.retrieval import HybridRetriever
from genesis.memory.store import MemoryStore

logger = logging.getLogger(__name__)


def _safe_get(row, key: str, default: str = "") -> str:
    """Get a column from an aiosqlite.Row, returning default if missing."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


@dataclass(frozen=True)
class BookmarkResult:
    """A bookmark search result with enough context to resume."""

    bookmark_id: str
    cc_session_id: str
    topic: str
    bookmark_type: str
    created_at: str
    has_rich_summary: bool
    source: str = "auto"
    score: float = 0.0
    content: str = ""


class BookmarkManager:
    """Manages session bookmark lifecycle: create, search, enrich."""

    def __init__(
        self,
        *,
        memory_store: MemoryStore,
        hybrid_retriever: HybridRetriever,
        db: aiosqlite.Connection,
        surplus_queue=None,
    ) -> None:
        self._store = memory_store
        self._retriever = hybrid_retriever
        self._db = db
        self._surplus_queue = surplus_queue

    async def create_micro(
        self,
        cc_session_id: str,
        context_messages: list[dict],
        *,
        tags: list[str] | None = None,
        transcript_path: str = "",
        genesis_session_id: str = "",
        source: str = "auto",
    ) -> str:
        """Create a micro-bookmark from session context.

        No LLM call — topic is derived from message content. Fast and cheap.
        """
        bookmark_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()

        # Build content from context messages
        content_parts = []
        for msg in context_messages[-5:]:
            text = msg.get("text", "")
            ts = msg.get("timestamp", "")
            if text:
                content_parts.append(f"[{ts}] {text}")

        content = "\n".join(content_parts) if content_parts else "Session bookmark"
        topic = self._extract_topic(context_messages)

        # Store as memory for hybrid search
        tag_list = list(tags) if tags else []
        tag_list.append("session_bookmark")
        tag_list.append(cc_session_id[:8])

        memory_content = f"Session bookmark: {topic}\n\n{content}"

        try:
            await self._store.store(
                content=memory_content,
                source=f"session:{cc_session_id}",
                memory_type="session_bookmark",
                tags=tag_list,
                confidence=0.85,
                source_pipeline="conversation",
            )
        except Exception:
            logger.error(
                "Failed to store bookmark memory for session %s",
                cc_session_id[:8], exc_info=True,
            )

        # Write to index table
        try:
            await bookmark_crud.create(
                self._db,
                id=bookmark_id,
                cc_session_id=cc_session_id,
                genesis_session_id=genesis_session_id,
                bookmark_type="micro",
                topic=topic,
                tags=json.dumps(tag_list),
                transcript_path=transcript_path,
                created_at=now,
                source=source,
            )
        except Exception:
            logger.error(
                "Failed to create bookmark index for session %s",
                cc_session_id[:8], exc_info=True,
            )

        logger.info(
            "Created micro-bookmark %s for session %s (source=%s): %s",
            bookmark_id[:8], cc_session_id[:8], source, topic,
        )

        # Trigger immediate memory extraction for shelved content.
        # The user explicitly flagged this as important — don't wait
        # for the 2h periodic job. Confidence boost +0.1 on extractions.
        await self._trigger_extraction(
            cc_session_id, context_messages, transcript_path,
        )

        # Enqueue rich summary generation via surplus compute
        if transcript_path and self._surplus_queue is not None:
            try:
                from genesis.surplus.types import ComputeTier, TaskType

                payload = json.dumps({
                    "bookmark_id": bookmark_id,
                    "transcript_path": transcript_path,
                })
                await self._surplus_queue.enqueue(
                    TaskType.BOOKMARK_ENRICHMENT,
                    ComputeTier.FREE_API,
                    0.4,
                    "curiosity",
                    payload=payload,
                )
                logger.info("Enqueued enrichment for bookmark %s", bookmark_id[:8])
            except Exception:
                logger.warning(
                    "Failed to enqueue bookmark enrichment (non-fatal)",
                    exc_info=True,
                )

        return bookmark_id

    async def create_explicit(
        self,
        cc_session_id: str,
        context_messages: list[dict],
        *,
        context_note: str = "",
        tags: list[str] | None = None,
        transcript_path: str = "",
    ) -> str:
        """Create a bookmark from an explicit /shelve command.

        Uses the user's context_note as topic (not heuristic extraction).
        Higher confidence and enrichment priority than auto-bookmarks.
        """
        bookmark_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()

        # Use context_note as topic if provided, fall back to heuristic
        topic = context_note.split("\n")[0][:80] if context_note else ""
        if not topic:
            topic = self._extract_topic(context_messages)

        # Build content from context messages
        content_parts = []
        for msg in context_messages[-5:]:
            text = msg.get("text", "")
            ts = msg.get("timestamp", "")
            if text:
                content_parts.append(f"[{ts}] {text}")

        content = "\n".join(content_parts) if content_parts else "Session bookmark"

        # Store as memory — context note is prominent for keyword search
        tag_list = list(tags) if tags else []
        tag_list.append("session_bookmark")
        tag_list.append(cc_session_id[:8])

        if context_note:
            memory_content = (
                f"Session bookmark: {topic}\n"
                f"Context: {context_note}\n"
                f"Tags: {', '.join(tag_list)}\n\n"
                f"{content}"
            )
        else:
            memory_content = (
                f"Session bookmark: {topic}\n"
                f"Tags: {', '.join(tag_list)}\n\n"
                f"{content}"
            )

        try:
            await self._store.store(
                content=memory_content,
                source=f"session:{cc_session_id}",
                memory_type="session_bookmark",
                tags=tag_list,
                confidence=0.90,
                source_pipeline="conversation",
            )
        except Exception:
            logger.error(
                "Failed to store explicit bookmark memory for session %s",
                cc_session_id[:8], exc_info=True,
            )

        # Write to index table
        try:
            await bookmark_crud.create(
                self._db,
                id=bookmark_id,
                cc_session_id=cc_session_id,
                bookmark_type="micro",
                topic=topic,
                tags=json.dumps(tag_list),
                transcript_path=transcript_path,
                created_at=now,
                source="explicit",
            )
        except Exception:
            logger.error(
                "Failed to create explicit bookmark index for session %s",
                cc_session_id[:8], exc_info=True,
            )

        logger.info(
            "Created explicit bookmark %s for session %s: %s",
            bookmark_id[:8], cc_session_id[:8], topic,
        )

        # Trigger immediate extraction with confidence boost
        await self._trigger_extraction(
            cc_session_id, context_messages, transcript_path,
        )

        # Higher-priority enrichment for explicit shelves
        if transcript_path and self._surplus_queue is not None:
            try:
                from genesis.surplus.types import ComputeTier, TaskType

                payload = json.dumps({
                    "bookmark_id": bookmark_id,
                    "transcript_path": transcript_path,
                })
                await self._surplus_queue.enqueue(
                    TaskType.BOOKMARK_ENRICHMENT,
                    ComputeTier.FREE_API,
                    0.6,
                    "curiosity",
                    payload=payload,
                )
                logger.info("Enqueued enrichment for explicit bookmark %s", bookmark_id[:8])
            except Exception:
                logger.warning(
                    "Failed to enqueue bookmark enrichment (non-fatal)",
                    exc_info=True,
                )

        return bookmark_id

    async def create_topic(
        self,
        cc_session_id: str,
        context_messages: list[dict],
        *,
        tags: list[str] | None = None,
    ) -> str:
        """Create a topic bookmark (mid-session snapshot)."""
        bookmark_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()

        content_parts = []
        for msg in context_messages[-5:]:
            text = msg.get("text", "")
            if text:
                content_parts.append(text)

        content = "\n".join(content_parts) if content_parts else "Topic bookmark"
        topic = self._extract_topic(context_messages)

        tag_list = list(tags) if tags else []
        tag_list.append("session_bookmark")
        tag_list.append("topic")

        memory_content = f"Topic bookmark: {topic}\n\n{content}"

        try:
            await self._store.store(
                content=memory_content,
                source=f"session:{cc_session_id}",
                memory_type="session_bookmark",
                tags=tag_list,
                confidence=0.80,
                source_pipeline="conversation",
            )
        except Exception:
            logger.error(
                "Failed to store topic bookmark memory", exc_info=True,
            )

        try:
            await bookmark_crud.create(
                self._db,
                id=bookmark_id,
                cc_session_id=cc_session_id,
                bookmark_type="topic",
                topic=topic,
                tags=json.dumps(tag_list),
                created_at=now,
            )
        except Exception:
            logger.error(
                "Failed to create topic bookmark index", exc_info=True,
            )

        logger.info("Created topic bookmark %s: %s", bookmark_id[:8], topic)
        return bookmark_id

    async def enrich(self, bookmark_id: str, rich_summary: str) -> bool:
        """Add a rich summary to an existing bookmark.

        Updates the memory content with richer keywords and context
        to improve future search recall.
        """
        bookmark = await bookmark_crud.get_by_id(self._db, bookmark_id)
        if bookmark is None:
            logger.warning("Bookmark %s not found for enrichment", bookmark_id[:8])
            return False

        cc_session_id = bookmark["cc_session_id"]
        topic = bookmark["topic"] or "Unknown topic"

        # Store enriched content as a new memory (linked via source)
        enriched_content = (
            f"Session bookmark (enriched): {topic}\n\n"
            f"{rich_summary}"
        )

        try:
            await self._store.store(
                content=enriched_content,
                source=f"session:{cc_session_id}",
                memory_type="session_bookmark",
                tags=["session_bookmark", "enriched", cc_session_id[:8]],
                confidence=0.90,
                source_pipeline="conversation",
            )
        except Exception:
            logger.error("Failed to store enriched bookmark memory", exc_info=True)
            return False

        try:
            await bookmark_crud.mark_enriched(self._db, bookmark_id)
        except Exception:
            logger.error("Failed to mark bookmark as enriched", exc_info=True)
            return False

        logger.info("Enriched bookmark %s: %s", bookmark_id[:8], topic)
        return True

    async def search(self, query: str, limit: int = 10) -> list[BookmarkResult]:
        """Search bookmarks using hybrid retrieval (vector + FTS5 + RRF)."""
        results = await self._retriever.recall(
            query=query,
            source="both",
            limit=limit * 2,  # Over-fetch to filter
            min_activation=0.0,
        )

        bookmarks: list[BookmarkResult] = []
        for r in results:
            if r.memory_type != "session_bookmark":
                continue

            # Extract session_id from source field
            cc_session_id = ""
            if r.source and r.source.startswith("session:"):
                cc_session_id = r.source[len("session:"):]

            # Look up index table for metadata
            idx = None
            if cc_session_id:
                idx = await bookmark_crud.get_by_session(self._db, cc_session_id)

            bookmarks.append(BookmarkResult(
                bookmark_id=idx["id"] if idx else r.memory_id,
                cc_session_id=cc_session_id,
                topic=idx["topic"] if idx else "",
                bookmark_type=idx["bookmark_type"] if idx else "micro",
                created_at=idx["created_at"] if idx else "",
                has_rich_summary=bool(idx["has_rich_summary"]) if idx else False,
                source=_safe_get(idx, "source", "auto") if idx else "auto",
                score=r.score,
                content=r.content[:200],
            ))

            if len(bookmarks) >= limit:
                break

        return bookmarks

    async def recent(self, limit: int = 10) -> list[BookmarkResult]:
        """Get recent bookmarks from the index table."""
        rows = await bookmark_crud.get_recent(self._db, limit)
        return [
            BookmarkResult(
                bookmark_id=row["id"],
                cc_session_id=row["cc_session_id"] or "",
                topic=row["topic"] or "",
                bookmark_type=row["bookmark_type"],
                created_at=row["created_at"],
                has_rich_summary=bool(row["has_rich_summary"]),
                source=_safe_get(row, "source", "auto"),
            )
            for row in rows
        ]

    async def _trigger_extraction(
        self,
        cc_session_id: str,
        context_messages: list[dict],
        transcript_path: str,
    ) -> None:
        """Trigger immediate memory extraction for shelved session content.

        The user explicitly /shelved this session — treat it as a "this matters"
        signal. Extract entities/decisions with a confidence boost (+0.1).
        Falls back gracefully if extraction deps aren't available.
        """
        if not context_messages:
            return

        try:
            from genesis.memory.extraction import (
                build_extraction_prompt,
                extractions_to_store_kwargs,
                parse_extraction_response,
            )

            # Build conversation text from context messages
            parts = []
            for msg in context_messages:
                role = msg.get("role", "user").upper()
                if role == "ASSISTANT":
                    role = "GENESIS"
                text = msg.get("text", "")[:1800]
                parts.append(f"{role}:\n{text}\n\n---\n")

            conversation_text = "\n".join(parts)
            prompt = build_extraction_prompt(conversation_text)

            # Get router from runtime (if available)
            try:
                from genesis.runtime import GenesisRuntime
                rt = GenesisRuntime.instance()
                router = rt._router
            except Exception:
                logger.debug("Router not available for shelve extraction")
                return

            if router is None:
                return

            response = await router.route_call(
                call_site_id="9_fact_extraction",
                messages=[{"role": "user", "content": prompt}],
            )

            if not response.success:
                logger.warning("Shelve extraction router call failed: %s", response.error)
                return

            text = response.content or ""
            extractions = parse_extraction_response(text)

            for extraction in extractions:
                kwargs = extractions_to_store_kwargs(
                    extraction,
                    source_session_id=cc_session_id,
                    transcript_path=transcript_path,
                    source_line_range=None,
                )
                # Confidence boost: user explicitly flagged importance
                kwargs["confidence"] = min(1.0, kwargs["confidence"] + 0.1)
                try:
                    await self._store.store(**kwargs)
                except Exception:
                    logger.error(
                        "Failed to store shelve extraction", exc_info=True,
                    )

            if extractions:
                logger.info(
                    "Shelve extraction: %d entities from session %s",
                    len(extractions), cc_session_id[:8],
                )

        except Exception:
            # Non-fatal — shelve still works even if extraction fails
            logger.error("Shelve extraction failed (non-fatal)", exc_info=True)

    @staticmethod
    def _extract_topic(messages: list[dict]) -> str:
        """Extract a topic hint from messages. No LLM — just heuristics.

        Prefers the LAST substantive message (what was being discussed
        when the session ended) over the first (often a generic greeting).
        """
        if not messages:
            return "Untitled session"

        # Use the last substantive message as topic
        for msg in reversed(messages):
            text = msg.get("text", "").strip()
            if len(text) > 10:
                # Take first line or first 80 chars
                first_line = text.split("\n")[0]
                if len(first_line) > 80:
                    return first_line[:77] + "..."
                return first_line

        return "Untitled session"
