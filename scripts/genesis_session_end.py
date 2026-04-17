#!/usr/bin/env python3
"""SessionEnd hook: persist session state for temporal awareness + auto-bookmarks.

Runs when a CC session terminates (via .claude/settings.json SessionEnd hook).
Must complete within 1.5s — file I/O only, no DB queries, no LLM calls.

Writes:
- ~/.genesis/last_foreground_session.json — previous session metadata for
  the next SessionStart to inject temporal context
- ~/.genesis/pending_bookmark.json — session context for auto-bookmark
  creation on next session start

Reads hook input from stdin as JSON:
  {"session_id": "...", "transcript_path": "...", "reason": "...", ...}

Skips background sessions (GENESIS_CC_SESSION=1).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

_FLAG = Path.home() / ".genesis" / "cc_context_enabled"
_GENESIS_DIR = Path.home() / ".genesis"
_LAST_SESSION_FILE = _GENESIS_DIR / "last_foreground_session.json"
_PENDING_BOOKMARK_FILE = _GENESIS_DIR / "pending_bookmark.json"
# Duplicated from genesis.db.crud.cognitive_state — keep in sync.
# Cannot import from genesis here (file-I/O-only hook, no package deps).
_PATCHES_FILE = _GENESIS_DIR / "session_patches.json"
_MAX_PATCHES = 20

_FILLER_WORDS = frozenset({
    "yes", "no", "ok", "okay", "thanks", "thank you", "sure", "yep",
    "yeah", "nah", "nope", "got it", "sounds good", "looks good",
    "lgtm", "commit that", "do it", "go ahead",
})


def _extract_topic(messages: list[dict], fallback: str = "") -> str:
    """Extract the best topic hint from a message buffer.

    Prefers the first substantive user message (>20 chars, not filler)
    over the last message. Falls back to the provided fallback string.
    """
    for msg in messages:
        text = msg.get("text", "").strip()
        if len(text) > 20 and text.lower() not in _FILLER_WORDS:
            return text[:200]
    return fallback[:200]


def _append_session_patch(
    *,
    patches_file: Path | None = None,
    session_id: str,
    topic_hint: str,
    message_count: int,
    ended_at: str,
) -> None:
    """Append a session patch to the patches file. File-I/O only."""
    path = patches_file or _PATCHES_FILE
    existing: list[dict] = []
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if isinstance(data, list):
                existing = data
        except (json.JSONDecodeError, OSError):
            existing = []  # Corrupt file — start fresh

    # Deduplicate: if this session_id already has a patch, replace it
    existing = [p for p in existing if p.get("session_id") != session_id]

    existing.append({
        "session_id": session_id,
        "ended_at": ended_at,
        "topic": topic_hint,
        "message_count": message_count,
    })

    # Cap at _MAX_PATCHES (keep most recent)
    if len(existing) > _MAX_PATCHES:
        existing = existing[-_MAX_PATCHES:]

    try:
        path.write_text(json.dumps(existing))
    except OSError as exc:
        print(f"SessionEnd hook: failed to write session patch: {exc}", file=sys.stderr)


def main() -> None:
    if not _FLAG.exists():
        return

    # Skip Genesis-dispatched background sessions
    if os.environ.get("GENESIS_CC_SESSION") == "1":
        return

    # Parse hook input from stdin
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        hook_input = {}

    session_id = hook_input.get("session_id", "")
    transcript_path = hook_input.get("transcript_path", "")
    reason = hook_input.get("reason", "other")

    if not session_id:
        return

    now = datetime.now(UTC).isoformat()

    # Read message buffer from session-scoped state (written by UserPromptSubmit)
    session_dir = _GENESIS_DIR / "sessions" / session_id
    messages_file = session_dir / "messages.jsonl"
    messages: list[dict] = []
    topic_hint = ""

    if messages_file.exists():
        try:
            lines = messages_file.read_text().strip().splitlines()
            import contextlib
            for line in lines[-5:]:  # Last 5 messages
                with contextlib.suppress(json.JSONDecodeError):
                    messages.append(json.loads(line))
            if messages:
                topic_hint = messages[-1].get("text", "")[:100]
        except OSError:
            pass

    _GENESIS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Write last foreground session metadata (for temporal awareness)
    try:
        session_data = {
            "session_id": session_id,
            "ended_at": now,
            "reason": reason,
            "topic_hint": topic_hint,
            "message_count": len(messages),
        }
        _LAST_SESSION_FILE.write_text(json.dumps(session_data))
    except OSError as exc:
        print(f"SessionEnd hook: failed to write session state: {exc}", file=sys.stderr)

    # 2. Write pending bookmark data (for auto-bookmark on next session start)
    if messages:
        try:
            bookmark_data = {
                "session_id": session_id,
                "transcript_path": transcript_path,
                "messages": messages,
                "topic_hint": topic_hint,
                "ended_at": now,
            }
            _PENDING_BOOKMARK_FILE.write_text(json.dumps(bookmark_data))
        except OSError as exc:
            print(f"SessionEnd hook: failed to write pending bookmark: {exc}", file=sys.stderr)

    # 3. Append session patch for cognitive state freshness
    patch_topic = _extract_topic(messages, fallback=topic_hint)
    _append_session_patch(
        session_id=session_id,
        topic_hint=patch_topic,
        message_count=len(messages),
        ended_at=now,
    )

    # 4. Trigger async essential knowledge regeneration (fire-and-forget).
    # Spawns a background subprocess since LLM/DB work exceeds 1500ms budget.
    _trigger_essential_knowledge_regen()


def _trigger_essential_knowledge_regen() -> None:
    """Spawn background subprocess to regenerate essential knowledge.

    Fire-and-forget — must not block the 1500ms hook budget.
    Calls scripts/regen_essential_knowledge.py as a detached subprocess.
    """
    import subprocess

    regen_script = Path(__file__).resolve().parent / "regen_essential_knowledge.py"
    if not regen_script.exists():
        return


    _LOG_DIR = _GENESIS_DIR / "logs"
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _log_file = _LOG_DIR / "ek_regen.log"
    import contextlib
    with contextlib.suppress(OSError):
        log_fh = open(_log_file, "a")  # noqa: SIM115
        subprocess.Popen(
            [sys.executable, str(regen_script)],
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
            start_new_session=True,  # Detach from hook process group
        )


if __name__ == "__main__":
    main()
