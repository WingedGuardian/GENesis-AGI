#!/usr/bin/env python3
"""PostToolUse hook: track files the session touches.

Writes recent file paths to ~/.genesis/sessions/{session_id}/recent_files.json
so the proactive memory hook can use them as retrieval signals.

Fires on Read|Edit|Write|Glob|Grep tool completions. Reads stdin JSON per
the PostToolUse contract.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Max files to track per session (most recent first)
_MAX_FILES = 20


def main() -> None:
    # Hooks must never crash — silent return on any error
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        data = json.loads(raw)
        _process(data)
    except Exception:
        return


def _process(data: dict) -> None:
    """Inner logic — separated so main() can wrap in try/except."""
    session_id = data.get("session_id", "")
    tool_input = data.get("tool_input") or {}
    tool_name = data.get("tool_name", "")
    if not session_id or not isinstance(tool_input, dict):
        return

    # Extract file path from tool input
    file_path = None
    if tool_name in ("Read", "Edit", "Write"):
        file_path = tool_input.get("file_path")
    elif tool_name in ("Glob", "Grep"):
        file_path = tool_input.get("path")

    if not file_path:
        return

    # Filter to project files only (skip system/temp paths)
    if not file_path.startswith("${HOME}/genesis"):
        return

    # Validate session_id is a safe path component (CC uses UUIDs)
    if "/" in session_id or "\\" in session_id or ".." in session_id:
        return

    # Write to session state directory
    session_dir = Path(os.path.expanduser("~/.genesis/sessions")) / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    state_file = session_dir / "recent_files.json"

    # Load existing, prepend new, deduplicate, truncate
    try:
        existing = json.loads(state_file.read_text()) if state_file.exists() else []
    except Exception:
        existing = []

    # Remove if already present (will be re-added at front)
    existing = [f for f in existing if f != file_path]
    # Prepend (most recent first)
    existing = [file_path, *existing][:_MAX_FILES]

    state_file.write_text(json.dumps(existing))


if __name__ == "__main__":
    main()
