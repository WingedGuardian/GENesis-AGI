"""Plan bookmark pending helper."""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from pathlib import Path

from genesis.util.run_lock import _lock_nb

logger = logging.getLogger(__name__)
_MISSING = object()


def _consume_pending(pending_file: Path):
    """Serialize updated readers; the existing writer does not take this lock."""
    if not pending_file.exists():
        return _MISSING
    try:
        lock = _lock_nb(pending_file.with_name(pending_file.name + ".lock"), "a")
    except OSError:
        logger.warning("Pending plan bookmark claim unavailable", exc_info=True)
        return _MISSING
    # Closing this descriptor releases the flock. Keep its inode for the next
    # caller; unlinking a lock file would permit independent holders.
    with lock:
        if not pending_file.exists():
            return _MISSING
        try:
            return json.loads(pending_file.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to read plan bookmark pending file", exc_info=True)
            return _MISSING
        finally:
            with suppress(OSError):
                pending_file.unlink(missing_ok=True)


async def _process_plan_bookmark_pending(
    bookmark_mgr,
    resolve_session_id_fn,
    *,
    pending_file: Path | None = None,
) -> None:
    """Consume one pending plan; release its cooperative lock before any await.

    The default retains the ordinary HOME input. This is not exactly-once
    delivery: old readers and the writer ignore the lock, and a failed create
    retains the existing unlink-before-create loss behavior.
    """
    if pending_file is None:
        pending_file = Path.home() / ".genesis" / "plan_bookmark_pending.json"
    data = _consume_pending(pending_file)
    if data is _MISSING:
        return

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
