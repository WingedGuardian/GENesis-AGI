"""Conversation history tools."""

from __future__ import annotations

import glob
import json as _json
import logging
import os
from pathlib import Path

from genesis.env import cc_project_dir

from ..memory import mcp


def _memory_mod():
    import genesis.mcp.memory_mcp as memory_mod

    return memory_mod

logger = logging.getLogger(__name__)


@mcp.tool()
async def conversation_history(
    channel: str = "telegram",
    limit: int = 20,
    search: str | None = None,
    thread_id: int | None = None,
) -> list[dict]:
    """Retrieve recent conversation messages ("scroll up"). Supports Telegram and CC CLI."""
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    limit = max(1, min(limit, 200))

    if channel == "telegram":
        from genesis.db.crud import telegram_messages
        if search:
            return await telegram_messages.search_all(memory_mod._db, search, limit=limit)
        if thread_id is not None:
            cursor = await memory_mod._db.execute(
                """SELECT * FROM telegram_messages
                   WHERE thread_id = ?
                   ORDER BY timestamp DESC LIMIT ?""",
                (thread_id, limit),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in reversed(rows)]
        return await telegram_messages.query_all_recent(memory_mod._db, limit=limit)

    if channel == "cc":
        jsonl_dir = str(Path.home() / ".claude" / "projects" / cc_project_dir())
        files = sorted(
            glob.glob(f"{jsonl_dir}/*.jsonl"),
            key=os.path.getmtime,
            reverse=True,
        )
        messages: list[dict] = []
        for fpath in files[:2]:
            try:
                with open(fpath, "rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 500_000))
                    if size > 500_000:
                        f.readline()
                    for line in f:
                        try:
                            d = _json.loads(line)
                            if d.get("type") in ("user", "assistant") and d.get("message"):
                                msg_text = d["message"]
                                if isinstance(msg_text, list):
                                    msg_text = " ".join(
                                        b.get("text", "")
                                        for b in msg_text
                                        if isinstance(b, dict) and b.get("type") == "text"
                                    )
                                if msg_text and (
                                    not search or search.lower() in msg_text.lower()
                                ):
                                    messages.append({
                                        "sender": d["type"],
                                        "content": msg_text[:500],
                                        "timestamp": d.get("timestamp", ""),
                                    })
                        except (_json.JSONDecodeError, KeyError):
                            continue
            except OSError:
                continue
        return messages[-limit:]

    return []
