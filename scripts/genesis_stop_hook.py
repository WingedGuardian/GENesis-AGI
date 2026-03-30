#!/usr/bin/env python3
"""Stop hook: detect resume signals + flag unreviewed code changes.

Runs when Claude finishes responding (via .claude/settings.json Stop hook).

1. Checks the user's last message for natural language signals that they want
   to return to this session later ("let's revisit", "park this", etc.).
   If detected, writes ~/.genesis/last_resume_signal.json.

2. Checks for unreviewed code changes. If found, outputs a reminder that
   gets injected into context for the next turn.

Reads hook input from stdin as JSON:
  {"session_id": "...", "last_assistant_message": "...", ...}

Skips background sessions (GENESIS_CC_SESSION=1).
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

_FLAG = Path.home() / ".genesis" / "cc_context_enabled"
_GENESIS_DIR = Path.home() / ".genesis"
_RESUME_SIGNAL_FILE = _GENESIS_DIR / "last_resume_signal.json"

# Patterns that suggest the user wants to come back to this session.
# Intentionally broad — false positives are cheap (just a note on next start),
# false negatives lose a signal the user actually wanted.
_RESUME_PATTERNS = re.compile(
    r"(?:"
    r"(?:let(?:'s)?|we\s+should)\s+(?:come\s+back|revisit|return|pick\s+(?:this|it)\s+up)"
    r"|park\s+this"
    r"|shelve\s+this"
    r"|continue\s+(?:this\s+)?(?:tomorrow|later|next\s+time)"
    r"|pick\s+(?:this|it)\s+up\s+(?:tomorrow|later|next)"
    r"|think\s+on\s+this"
    r"|sleep\s+on\s+(?:this|it)"
    r"|come\s+back\s+to\s+(?:this|it)"
    r"|resume\s+(?:this\s+)?later"
    r"|save\s+(?:this|our)\s+(?:place|progress|spot)"
    r")",
    re.IGNORECASE,
)


def main() -> None:
    if not _FLAG.exists():
        return

    if os.environ.get("GENESIS_CC_SESSION") == "1":
        return

    # Parse hook input from stdin
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        hook_input = {}

    session_id = hook_input.get("session_id", "")
    if not session_id:
        return

    # Read last user message from session-scoped buffer
    session_dir = _GENESIS_DIR / "sessions" / session_id
    messages_file = session_dir / "messages.jsonl"

    last_user_msg = ""
    if messages_file.exists():
        try:
            lines = messages_file.read_text().strip().splitlines()
            if lines:
                last = json.loads(lines[-1])
                last_user_msg = last.get("text", "")
        except (json.JSONDecodeError, OSError):
            pass

    if not last_user_msg:
        return

    # Check for resume signal
    match = _RESUME_PATTERNS.search(last_user_msg)
    if match:
        _GENESIS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            signal_data = {
                "session_id": session_id,
                "signal": match.group(0),
                "timestamp": datetime.now(UTC).isoformat(),
            }
            _RESUME_SIGNAL_FILE.write_text(json.dumps(signal_data))
        except OSError:
            pass

    # Check for unreviewed code changes
    _check_review_state()


def _check_review_state() -> None:
    """Output review reminder if unreviewed code changes exist."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, script_dir)

    try:
        from review_state import has_code_changes, is_review_current
    except ImportError:
        return  # review_state.py not available — skip silently

    if not has_code_changes():
        return

    if is_review_current():
        return

    print(
        "CODE REVIEW PENDING: Code changes were made without /review. "
        "Next turn MUST begin with /review + superpowers:code-reviewer agent."
    )


if __name__ == "__main__":
    main()
