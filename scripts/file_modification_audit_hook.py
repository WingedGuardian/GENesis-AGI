#!/usr/bin/env python3
"""PostToolUse hook: audit trail for file modifications.

Records Write and Edit tool uses to the file_modifications table in
genesis.db, enabling fast diagnosis of "what session modified this file?"

Fires on Write|Edit tool completions. Reads stdin JSON per the
PostToolUse contract. Uses sync sqlite3 (not aiosqlite) since hooks
run as standalone processes outside the async runtime.

Zero tokens — pure shell/DB operation.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

# genesis.db location — same as genesis.env.genesis_db_path()
_DB_PATH = Path.home() / "genesis" / "data" / "genesis.db"


def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        data = json.loads(raw)
        _process(data)
    except Exception:
        # Hooks must never crash
        return


def _process(data: dict) -> None:
    session_id = data.get("session_id", "")
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input") or {}

    if not isinstance(tool_input, dict):
        return

    file_path = tool_input.get("file_path")
    if not file_path:
        return

    # Determine action
    if tool_name == "Write":
        action = "write"
    elif tool_name == "Edit":
        action = "edit"
    else:
        return

    # Compute hash of file after modification (if it exists)
    file_hash = None
    try:
        p = Path(file_path)
        if p.exists() and p.is_file():
            file_hash = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except (OSError, PermissionError):
        pass

    # Insert into DB (sync sqlite3)
    if not _DB_PATH.exists():
        return

    try:
        conn = sqlite3.connect(str(_DB_PATH), timeout=2)
        conn.execute(
            """INSERT INTO file_modifications
               (session_id, file_path, action, tool_name, file_hash, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                session_id or None,
                file_path,
                action,
                tool_name,
                file_hash,
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
        conn.close()
    except sqlite3.OperationalError:
        # Table might not exist yet (pre-migration). Silent fail.
        pass


if __name__ == "__main__":
    main()
