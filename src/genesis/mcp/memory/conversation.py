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
    chat_id: int | None = None,
    before: str | None = None,
) -> list[dict]:
    """Retrieve recent conversation messages ("scroll up"). Supports Telegram and CC CLI.

    ``chat_id`` scopes the telegram view to ONE chat — pass your own chat id
    for a real DM scroll-up (without it, results span ALL chats; that unscoped
    default is intentional for reflection/cross-chat use). ``before`` (ISO
    timestamp, exclusive) pages further back: pass the oldest timestamp from
    the previous page to walk arbitrarily far up the conversation.
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    limit = max(1, min(limit, 200))

    if channel == "telegram":
        from genesis.db.crud import telegram_messages
        if search:
            if chat_id is not None:
                return await telegram_messages.search(
                    memory_mod._db, chat_id, search,
                    limit=limit, thread_id=thread_id, before=before,
                )
            return await telegram_messages.search_all(
                memory_mod._db, search, limit=limit, before=before,
            )
        if chat_id is not None:
            return await telegram_messages.query_recent(
                memory_mod._db, chat_id,
                thread_id=thread_id, limit=limit, before=before,
            )
        if thread_id is not None:
            if before:
                rows = await memory_mod._db.execute_fetchall(
                    """SELECT * FROM telegram_messages
                       WHERE thread_id = ? AND timestamp < ?
                       ORDER BY timestamp DESC LIMIT ?""",
                    (thread_id, before, limit),
                )
            else:
                rows = await memory_mod._db.execute_fetchall(
                    """SELECT * FROM telegram_messages
                       WHERE thread_id = ?
                       ORDER BY timestamp DESC LIMIT ?""",
                    (thread_id, limit),
                )
            return [dict(r) for r in reversed(rows)]
        return await telegram_messages.query_all_recent(
            memory_mod._db, limit=limit, before=before,
        )

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
        # Order globally by timestamp: files are read newest-file-first
        # (files[:2]) and concatenated, so without this the two files' messages
        # interleave out of chronological order and `[-limit:]` can return
        # older-file entries. Sorting oldest→newest matches the telegram
        # branch's contract and makes the `before`/limit window precise. A
        # missing/empty timestamp sorts oldest (and is dropped once `before`
        # is set, below).
        messages.sort(key=lambda m: m.get("timestamp") or "")
        if before:
            # Page further back: honor the `before` cursor on CC too (the
            # telegram branch already does). CC timestamps and `before` share
            # the same ISO-8601 source (the caller pages by feeding back a
            # timestamp this tool emitted), so a lexical compare is correct. A
            # record with no timestamp can't be proven to precede the cursor, so
            # it is excluded once `before` is set.
            messages = [
                m for m in messages
                if m.get("timestamp") and m["timestamp"] < before
            ]
        return messages[-limit:]

    return []
