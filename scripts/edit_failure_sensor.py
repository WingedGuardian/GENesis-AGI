#!/usr/bin/env python3
"""PostToolUse + PostToolUseFailure hook: track Edit/Write success and failure rates.

Records every Edit and Write tool call outcome to the tool_call_outcomes
table, enabling measurement of edit failure rates and identification of
patterns in failed edits (file size, match complexity, etc.).

Registered for BOTH events (see .claude/settings.json):
- PostToolUse fires only on SUCCESSFUL tool calls — it never sees a failed
  Edit, so on this event the row is a success record (the marker scan below
  is a belt-and-braces guard for soft-error text in nominally-successful
  output).
- PostToolUseFailure fires when the tool call FAILS (e.g. "old_string not
  found") and carries the error in ``tool_error``. This is the only path
  that can produce success=0 rows; without it the sensor is blind to
  failures (observed: 12,737 rows / 0 failures over 7 weeks before this
  event was registered).

Reads stdin JSON per the hook contract; ``hook_event_name`` distinguishes
the two events. Zero tokens — pure shell/DB operation.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

# GENESIS_DB_PATH override exists for tests/verification only; production
# hooks rely on the default. Keep this script stdlib-only (no genesis imports).
_DB_PATH = Path(
    os.environ.get("GENESIS_DB_PATH", "")
    or Path.home() / "genesis" / "data" / "genesis.db"
)

# Markers in tool_output that indicate an Edit failure surfaced in a
# nominally-successful call (defensive; hard failures arrive via
# PostToolUseFailure instead).
_FAILURE_MARKERS = [
    "old_string not found",
    "not unique in the file",
    "no changes were made",
    "old_string and new_string are the same",
    "Error:",
]


def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        data = json.loads(raw)
        _process(data)
    except Exception:
        return


def _extract_error(data: dict) -> str:
    """Best-effort error text from a PostToolUseFailure payload.

    ``tool_error`` is the documented key; the fallbacks tolerate payload
    drift across Claude Code versions rather than silently recording an
    empty snippet.
    """
    for key in ("tool_error", "error", "tool_output", "tool_response"):
        value = data.get(key)
        if value:
            return str(value)
    code = data.get("tool_error_code")
    if code is not None:
        return f"tool failed (error code {code}, no error text)"
    return "tool failed (no error text in payload)"


def _process(data: dict) -> None:
    tool_name = data.get("tool_name", "")
    if tool_name not in ("Edit", "Write"):
        return

    session_id = data.get("session_id", "")
    tool_input = data.get("tool_input") or {}

    if not isinstance(tool_input, dict):
        return

    file_path = tool_input.get("file_path", "")

    if data.get("hook_event_name") == "PostToolUseFailure":
        success = 0
        error_snippet = _extract_error(data)[:200]
    else:
        tool_output = data.get("tool_output", "") or ""
        success = 1
        error_snippet = None
        output_lower = str(tool_output).lower()
        for marker in _FAILURE_MARKERS:
            if marker.lower() in output_lower:
                success = 0
                error_snippet = str(tool_output)[:200]
                break

    if not _DB_PATH.exists():
        return

    try:
        conn = sqlite3.connect(str(_DB_PATH), timeout=2)
        try:
            conn.execute(
                """INSERT INTO tool_call_outcomes
                   (session_id, tool_name, file_path, success, error_snippet, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    session_id or None,
                    tool_name,
                    file_path or None,
                    success,
                    error_snippet,
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        pass


if __name__ == "__main__":
    main()
