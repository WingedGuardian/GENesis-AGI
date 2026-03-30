"""Bookmark tools: shelve, unshelve."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..memory import mcp


def _memory_mod():
    import genesis.mcp.memory_mcp as memory_mod

    return memory_mod

logger = logging.getLogger(__name__)


@mcp.tool()
async def bookmark_shelve(
    session_id: str,
    tags: str = "",
    context: str = "",
) -> str:
    """Shelve a session — create a bookmark so you can easily return to it."""
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._bookmark_mgr is not None

    session_id = memory_mod._resolve_session_id(session_id)

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    messages: list[dict] = []
    session_dir = Path.home() / ".genesis" / "sessions" / session_id
    messages_file = session_dir / "messages.jsonl"
    if messages_file.exists():
        try:
            import contextlib
            for line in messages_file.read_text().strip().splitlines():
                with contextlib.suppress(json.JSONDecodeError):
                    messages.append(json.loads(line))
        except OSError:
            pass

    bookmark_id = await memory_mod._bookmark_mgr.create_explicit(
        cc_session_id=session_id,
        context_messages=messages,
        context_note=context,
        tags=tag_list,
    )

    return (
        f"Session bookmarked (ID: {bookmark_id[:8]}). "
        f"To find it later, use bookmark_unshelve with a keyword. "
        f"To resume directly: `claude --resume {session_id}`"
    )


@mcp.tool()
async def bookmark_unshelve(
    query: str = "",
    limit: int = 5,
) -> str:
    """Find a previously shelved session bookmark."""
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._bookmark_mgr is not None

    if query:
        results = await memory_mod._bookmark_mgr.search(query, limit=limit)
    else:
        results = await memory_mod._bookmark_mgr.recent(limit=limit)

    if not results:
        return "No bookmarks found." + (" Try a broader query." if query else "")

    from genesis.db.crud import session_bookmarks as bookmark_crud
    assert memory_mod._db is not None
    for r in results:
        try:
            await bookmark_crud.increment_resumed(memory_mod._db, r.bookmark_id)
        except Exception:
            logger.warning("Failed to increment resumed count for %s", r.bookmark_id[:8], exc_info=True)

    _source_labels = {"explicit": "[shelved]", "plan": "[plan]", "auto": "[auto]"}

    lines = [f"Found {len(results)} bookmark(s):\n"]
    for r in results:
        enriched = " [enriched]" if r.has_rich_summary else ""
        score = f" (score: {r.score:.2f})" if r.score > 0 else ""
        source_label = _source_labels.get(r.source, f"[{r.source}]")
        lines.append(
            f"- **{r.topic or 'Untitled'}** {source_label}{enriched}{score}\n"
            f"  Type: {r.bookmark_type} | Created: {r.created_at}\n"
            f"  Resume: `claude --resume {r.cc_session_id}`"
        )

    return "\n".join(lines)
