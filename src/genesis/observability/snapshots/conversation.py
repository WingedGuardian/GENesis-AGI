"""Conversation activity snapshot from CC session JSONL files."""

from __future__ import annotations

import glob
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from genesis.env import cc_project_dir

logger = logging.getLogger(__name__)

CC_JSONL_DIR = str(Path.home() / ".claude" / "projects" / cc_project_dir())


def conversation_activity() -> dict:
    """Surface conversation activity from CC session JSONL files."""
    jsonl_dir = os.path.expanduser(CC_JSONL_DIR)
    try:
        files = sorted(
            glob.glob(f"{jsonl_dir}/*.jsonl"),
            key=os.path.getmtime,
            reverse=True,
        )
    except OSError:
        return {"status": "error", "error": "cannot read JSONL directory"}
    if not files:
        return {"status": "no_sessions"}

    latest = files[0]
    try:
        stat = os.stat(latest)
        file_age_s = (datetime.now(UTC).timestamp() - stat.st_mtime)
    except OSError:
        return {"status": "error", "error": "cannot stat JSONL file"}

    user_count = 0
    assistant_count = 0
    last_user_ts = None
    cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    try:
        with open(latest, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 100_000))
            if size > 100_000:
                f.readline()
            for line in f:
                try:
                    d = json.loads(line)
                    ts = d.get("timestamp", "")
                    if ts and ts < cutoff:
                        continue
                    t = d.get("type")
                    if t == "user":
                        user_count += 1
                        last_user_ts = ts or d.get("timestamp")
                    elif t == "assistant":
                        assistant_count += 1
                except (json.JSONDecodeError, KeyError):
                    continue
    except OSError:
        pass

    last_user_age_s = None
    if last_user_ts:
        try:
            last_dt = datetime.fromisoformat(last_user_ts)
            last_user_age_s = round(
                (datetime.now(UTC) - last_dt).total_seconds(), 1
            )
        except (ValueError, TypeError):
            pass

    return {
        "status": "active" if file_age_s < 300 else "idle",
        "last_user_message_age_s": last_user_age_s,
        "recent_user_turns": user_count,
        "recent_assistant_turns": assistant_count,
    }
