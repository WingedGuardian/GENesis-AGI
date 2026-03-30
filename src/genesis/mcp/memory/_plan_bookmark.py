"""Plan bookmark pending helper."""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


async def _process_plan_bookmark_pending(
    bookmark_mgr,
    resolve_session_id_fn,
) -> None:
    """Process a pending plan bookmark file (written by plan_bookmark_hook.py).

    One-shot, non-fatal. If the file doesn't exist, returns immediately.
    """
    pending_file = Path.home() / ".genesis" / "plan_bookmark_pending.json"
    if not pending_file.exists():
        return

    try:
        raw = pending_file.read_text()
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to read plan bookmark pending file", exc_info=True)
        return
    finally:
        import contextlib
        with contextlib.suppress(OSError):
            pending_file.unlink(missing_ok=True)

    plan_path = data.get("plan_path", "")
    title = data.get("title", "Plan session")
    session_id_hint = data.get("session_id_hint", "")

    cc_session_id = resolve_session_id_fn(session_id_hint) if session_id_hint else ""

    tags = ["plan", "approved"]
    if title:
        tags.append(title.lower().replace(" ", "-")[:30])

    context_note = f"Plan approved: {plan_path}" if plan_path else "Plan session approved"

    try:
        bookmark_id = await bookmark_mgr.create_micro(
            cc_session_id=cc_session_id,
            context_messages=[{"text": title or context_note, "timestamp": ""}],
            tags=tags,
            transcript_path="",
            source="plan",
        )
        logger.info(
            "Created plan bookmark %s for session %s: %s",
            bookmark_id[:8], cc_session_id[:8] if cc_session_id else "unknown", title,
        )
    except Exception:
        logger.error("Failed to create plan bookmark", exc_info=True)
