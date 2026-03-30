"""BookmarkEnrichmentExecutor — enriches micro-bookmarks with rich summaries.

Follows the same pattern as CodeAuditExecutor: a dedicated surplus executor
wired separately from the reflection engine. Reads transcripts, calls an LLM
for structured summaries, and writes results back to the bookmark.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.surplus.types import ExecutorResult, SurplusTask

if TYPE_CHECKING:
    import aiosqlite

    from genesis.bookmark.manager import BookmarkManager

logger = logging.getLogger(__name__)

# Summary prompt for the LLM
_SUMMARY_PROMPT = """\
Summarize this conversation session concisely. Include:
1. **Key decisions** made during the session
2. **Where we left off** — what was being worked on when the session ended
3. **Next steps** — what should be done next
4. **Keywords** — 5-10 searchable terms that would help find this session later

Keep it under 300 words. Focus on what would be most useful for someone
resuming this work later.

Conversation:
{transcript}
"""


def _read_transcript(transcript_path: str, max_exchanges: int = 20) -> str:
    """Read the last N exchanges from a CC transcript JSONL file."""
    path = Path(transcript_path)
    if not path.exists():
        return ""

    try:
        # Read from the end of the file to get recent exchanges
        lines = path.read_text().strip().splitlines()
        # Take last N lines (each line is a JSON event)
        recent = lines[-(max_exchanges * 3):]  # ~3 events per exchange

        exchanges: list[str] = []
        for line in recent:
            try:
                event = json.loads(line)
                etype = event.get("type", "")

                if etype == "human":
                    content = event.get("message", {}).get("content", "")
                    if isinstance(content, str) and content.strip():
                        exchanges.append(f"User: {content[:500]}")

                elif etype == "assistant":
                    content = event.get("message", {}).get("content", [])
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")[:500]
                            if text.strip():
                                exchanges.append(f"Assistant: {text}")
            except (json.JSONDecodeError, TypeError, KeyError):
                continue

        return "\n\n".join(exchanges[-max_exchanges:])
    except OSError:
        logger.warning("Failed to read transcript at %s", transcript_path, exc_info=True)
        return ""


class BookmarkEnrichmentExecutor:
    """Enriches micro-bookmarks with LLM-generated rich summaries.

    Designed for the surplus system — runs during idle time, not on the
    critical path. Reads transcripts and generates structured summaries
    to improve future bookmark search recall.
    """

    def __init__(
        self,
        *,
        bookmark_manager: BookmarkManager,
        db: aiosqlite.Connection,
        router=None,
    ) -> None:
        self._bookmark_mgr = bookmark_manager
        self._db = db
        self._router = router

    async def execute(self, task: SurplusTask) -> ExecutorResult:
        """Execute a bookmark enrichment task."""
        if not task.payload:
            return ExecutorResult(
                success=False, error="No payload (bookmark_id) in task",
            )

        try:
            payload = json.loads(task.payload)
        except json.JSONDecodeError:
            # Payload might be just the bookmark_id string
            payload = {"bookmark_id": task.payload}

        bookmark_id = payload.get("bookmark_id", "")
        transcript_path = payload.get("transcript_path", "")

        if not bookmark_id:
            return ExecutorResult(success=False, error="No bookmark_id in payload")

        # Load bookmark from DB
        from genesis.db.crud import session_bookmarks as bookmark_crud

        bookmark = await bookmark_crud.get_by_id(self._db, bookmark_id)
        if bookmark is None:
            return ExecutorResult(
                success=False, error=f"Bookmark {bookmark_id[:8]} not found",
            )

        if bookmark["has_rich_summary"]:
            return ExecutorResult(
                success=True, content="Already enriched",
            )

        # Use transcript_path from payload or bookmark record
        t_path = transcript_path or bookmark.get("transcript_path", "")
        if not t_path:
            return ExecutorResult(
                success=False, error="No transcript_path available",
            )

        # Read transcript
        transcript = _read_transcript(t_path)
        if not transcript:
            return ExecutorResult(
                success=False, error=f"Could not read transcript at {t_path}",
            )

        # Generate summary via LLM
        summary = await self._generate_summary(transcript)
        if not summary:
            return ExecutorResult(
                success=False, error="LLM summary generation failed",
            )

        # Write enrichment back to bookmark
        success = await self._bookmark_mgr.enrich(bookmark_id, summary)
        if not success:
            return ExecutorResult(
                success=False, error="Failed to write enrichment to bookmark",
            )

        logger.info(
            "Enriched bookmark %s with %d-char summary",
            bookmark_id[:8], len(summary),
        )

        return ExecutorResult(
            success=True,
            content=summary,
            insights=[{
                "content": summary,
                "source_task_type": "bookmark_enrichment",
                "generating_model": "router",
                "drive_alignment": "curiosity",
                "confidence": 0.75,
            }],
        )

    async def _generate_summary(self, transcript: str) -> str:
        """Generate a rich summary using the LLM router."""
        if self._router is None:
            logger.warning("No router available for bookmark enrichment")
            return ""

        prompt = _SUMMARY_PROMPT.format(transcript=transcript[:8000])

        try:
            response = await self._router.route(
                prompt=prompt,
                purpose="bookmark_enrichment",
            )
            return response if response else ""
        except Exception:
            logger.error("LLM summary generation failed", exc_info=True)
            return ""
