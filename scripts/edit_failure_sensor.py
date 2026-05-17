#!/usr/bin/env python3
"""PostToolUse hook: track Edit/Write success and failure rates.

Records every Edit and Write tool call outcome to the tool_call_outcomes
table, enabling measurement of edit failure rates and identification of
patterns in failed edits (file size, match complexity, etc.).

Fires on Edit|Write completions. Reads stdin JSON per the PostToolUse
contract (includes tool_result/tool_output).

Zero tokens — pure shell/DB operation.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

_DB_PATH = Path.home() / "genesis" / "data" / "genesis.db"

# Markers in tool_output that indicate an Edit failure
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


def _process(data: dict) -> None:
    tool_name = data.get("tool_name", "")
    if tool_name not in ("Edit", "Write"):
        return

    session_id = data.get("session_id", "")
    tool_input = data.get("tool_input") or {}
    tool_output = data.get("tool_output", "") or ""

    if not isinstance(tool_input, dict):
        return

    file_path = tool_input.get("file_path", "")

    # Determine success/failure from tool_output
    success = 1
    error_snippet = None

    output_lower = tool_output.lower()
    for marker in _FAILURE_MARKERS:
        if marker.lower() in output_lower:
            success = 0
            error_snippet = tool_output[:200]
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
