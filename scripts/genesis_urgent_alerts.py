#!/usr/bin/env python3
"""UserPromptSubmit hook: temporal awareness + bookmark + shelve hints.

Runs before each user message is processed. Three responsibilities:
1. Inject absolute timestamps for temporal awareness (session clock)
2. Buffer user messages for session bookmarks (rolling last 5)
3. Detect /shelve or /unshelve and inject soft hint for LLM

This hook does NOT inject alerts into the prompt. Alerts are delivered
via the Telegram outreach pipeline; injecting them into the CC session
prompt was a duplicate channel that caused identical alerts to flood
every prompt. Removed 2026-04-09. Filename retained for now to avoid
churn in .claude/settings.json — TODO: rename to genesis_session_state.py
in a follow-up cleanup.

Reads hook input from stdin as JSON:
  {"session_id": "...", "prompt": "...", ...}

Reads session start timestamp from ~/.genesis/session_start (written by
the SessionStart hook). Falls back to a 10-minute lookback if file missing.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# Load secrets.env so USER_TIMEZONE and other env vars are available
# before any genesis module imports (which may read os.environ at import time).
_SECRETS_PATH = Path(__file__).resolve().parent.parent / "secrets.env"
if _SECRETS_PATH.is_file():
    try:
        from dotenv import load_dotenv
        load_dotenv(str(_SECRETS_PATH), override=False)
    except ImportError:
        pass

_FLAG = Path.home() / ".genesis" / "cc_context_enabled"
_SESSION_START_FILE = Path.home() / ".genesis" / "session_start"
_GENESIS_DIR = Path.home() / ".genesis"
_FALLBACK_LOOKBACK_MINUTES = 10
_MAX_BUFFER_LINES = 5
_MAX_MSG_LENGTH = 200

_SHELVE_PATTERN = re.compile(r"/(?:shelve|unshelve)\b", re.IGNORECASE)


def _get_session_start() -> str:
    """Get session start ISO timestamp. Falls back to 10 min ago."""
    if _SESSION_START_FILE.exists():
        try:
            return _SESSION_START_FILE.read_text().strip()
        except Exception:
            pass
    return (datetime.now(UTC) - timedelta(minutes=_FALLBACK_LOOKBACK_MINUTES)).isoformat()


def _format_day_time(iso: str) -> str:
    """Format ISO timestamp as 'Mon 14:32' in user's timezone."""
    try:
        from genesis.util.tz import fmt as _tz_fmt

        return _tz_fmt(iso, "%a %H:%M")
    except ImportError:
        # Fallback if genesis not importable
        try:
            dt = datetime.fromisoformat(iso)
            return dt.strftime("%a %H:%M")
        except (ValueError, TypeError):
            return "unknown"
    except (ValueError, TypeError):
        return "unknown"


def _emit_temporal_context(session_id: str, now: datetime) -> None:
    """Emit absolute timestamp context line."""
    session_start_iso = _get_session_start()
    started = _format_day_time(session_start_iso)

    # Read last prompt time from session-scoped state
    session_dir = _GENESIS_DIR / "sessions" / session_id
    last_prompt_file = session_dir / "last_prompt_time"
    last_msg = ""
    if last_prompt_file.exists():
        with contextlib.suppress(OSError):
            last_msg = _format_day_time(last_prompt_file.read_text().strip())

    try:
        from genesis.util.tz import fmt as _tz_fmt

        clock = _tz_fmt(now.isoformat())
    except ImportError:
        clock = now.strftime("%a %Y-%m-%d %H:%M UTC")
    parts = [f"Clock: {clock}", f"Session: {session_id[:8]}", f"Started: {started}"]
    if last_msg:
        parts.append(f"Last msg: {last_msg}")

    print(f"[{' | '.join(parts)}]")
    sys.stdout.flush()


def _buffer_message(session_id: str, prompt: str, now: datetime) -> None:
    """Append user message to session-scoped rolling buffer."""
    session_dir = _GENESIS_DIR / "sessions" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    messages_file = session_dir / "messages.jsonl"
    last_prompt_file = session_dir / "last_prompt_time"

    # Write current timestamp for next temporal context
    with contextlib.suppress(OSError):
        last_prompt_file.write_text(now.isoformat())

    # Append truncated message to rolling buffer
    entry = json.dumps({
        "text": prompt[:_MAX_MSG_LENGTH],
        "timestamp": now.isoformat(),
    })

    try:
        # Read existing lines, keep last N-1, append new
        existing: list[str] = []
        if messages_file.exists():
            existing = messages_file.read_text().strip().splitlines()
        existing = existing[-((_MAX_BUFFER_LINES) - 1):]
        existing.append(entry)
        messages_file.write_text("\n".join(existing) + "\n")
    except OSError:
        pass


def _check_shelve_hint(prompt: str) -> None:
    """Detect /shelve or /unshelve and emit a soft hint."""
    if _SHELVE_PATTERN.search(prompt):
        print(
            "The user may be asking to bookmark this session. "
            "If that's their intent, use the bookmark_shelve or "
            "bookmark_unshelve MCP tool."
        )
        sys.stdout.flush()


def _emit_charter_tag(session_id: str) -> None:
    """One-line drift tag: [Charter: <mission|origin snippet> | open: N].

    Read-only stdlib sqlite3, mode=ro URI (WAL-aware — never immutable=1,
    which misses un-checkpointed writes), 500ms connect / 300ms busy budget:
    this runs on EVERY prompt and must never cost the user anything.
    Omitted entirely when the session has no charter row yet (pre-first-
    compaction — the origin is still in context), the DB/table is missing
    (un-migrated install), or the DB is locked. open counts open+in_progress
    ledger rows; "open: 0" IS shown for a chartered session — a clear ledger
    is signal.
    """
    try:
        root = os.environ.get("GENESIS_REPO_ROOT", "")
        db = (Path(root) if root else Path.home() / "genesis") / "data" / "genesis.db"
        if not db.exists():
            return
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
        try:
            conn.execute("PRAGMA busy_timeout=300")
            row = conn.execute(
                "SELECT mission, origin_prompt FROM session_charters"
                " WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return
            (open_n,) = conn.execute(
                "SELECT COUNT(*) FROM session_ledger WHERE session_id = ?"
                " AND status IN ('open','in_progress')",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        mission, origin = row
        label = (mission or "").strip()
        if label:
            label = label[:80] + ("…" if len(label) > 80 else "")
        else:
            first_line = next(
                (ln for ln in (origin or "").strip().splitlines() if ln.strip()), ""
            )
            if not first_line:
                return
            snippet = first_line[:60] + ("…" if len(first_line) > 60 else "")
            label = f'origin: "{snippet}"'
        print(f"[Charter: {label} | open: {open_n}]")
        sys.stdout.flush()
    except Exception:
        return  # fail-open: a tag miss must never surface as an error


def main() -> None:
    # Skip if Genesis context is disabled
    if not _FLAG.exists():
        return

    # Skip for Genesis-dispatched sessions (they have their own alert path)
    if os.environ.get("GENESIS_CC_SESSION") == "1":
        return

    # Parse hook input from stdin
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        hook_input = {}

    session_id = hook_input.get("session_id", "")
    prompt = hook_input.get("prompt", "")
    now = datetime.now(UTC)

    # 1. Temporal context (always, even if no session_id)
    if session_id:
        _emit_temporal_context(session_id, now)
        # 1b. Charter drift tag (chartered sessions only; fail-open)
        _emit_charter_tag(session_id)

    # 2. Buffer user message for bookmarks
    if session_id and prompt:
        _buffer_message(session_id, prompt, now)

    # 3. Shelve/unshelve hint
    if prompt:
        _check_shelve_hint(prompt)


if __name__ == "__main__":
    main()
