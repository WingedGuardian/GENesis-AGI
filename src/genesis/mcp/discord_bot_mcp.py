"""Standalone Discord bot MCP server — read-only message access + reply.

Provides fetch_messages, fetch_forum_threads, and send_reply tools via
the Discord bot token. Loaded only by sessions that use the discord-bot MCP
server (not by foreground, ego, reflection, or other session types).

Token + an OPTIONAL best-effort genesis.db connection are injected at bootstrap
via init_discord_bot(). The DB is used ONLY for WS5 capability-shadow recording
(send_reply observes what a gate would decide, never holds); the server is fully
functional without it (db=None => shadow is a no-op).
"""

from __future__ import annotations

import json
import logging

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

mcp = FastMCP("discord-bot")

_bot_token: str | None = None

# WS5: optional best-effort genesis.db connection for capability-shadow recording.
# None => shadow is a no-op (the reply path is never gated on it).
_db = None

# GENesis Discord guild ID (public — visible in access.json)
_GUILD_ID = "1486075405579583538"

# Discord API base
_API = "https://discord.com/api/v10"

# Discord message length limit
_MAX_MESSAGE_LENGTH = 2000


def init_discord_bot(*, bot_token: str, db=None) -> None:
    """Wire the bot token for API calls, plus an OPTIONAL best-effort genesis.db
    connection used only for WS5 capability-shadow recording (send_reply observes,
    never holds). ``db=None`` keeps the server fully functional with shadow disabled."""
    global _bot_token, _db
    _bot_token = bot_token
    _db = db
    logger.info(
        "Discord bot MCP wired (token=%s, shadow_db=%s)", bool(bot_token), bool(db),
    )


def _headers() -> dict[str, str]:
    """Authorization headers for Discord API."""
    if not _bot_token:
        raise RuntimeError("DISCORD_BOT_TOKEN not configured")
    return {"Authorization": f"Bot {_bot_token}"}


def _format_message(msg: dict) -> dict:
    """Extract essential fields from a Discord message object."""
    author = msg.get("author", {})
    return {
        "id": msg["id"],
        "author_name": author.get("username", "unknown"),
        "author_id": author.get("id", ""),
        "is_bot": author.get("bot", False),
        "content": msg.get("content", ""),
        "timestamp": msg.get("timestamp", ""),
    }


def _chunk_text(text: str, limit: int = _MAX_MESSAGE_LENGTH) -> list[str]:
    """Split text into chunks at newline boundaries."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Find last newline before limit
        idx = text.rfind("\n", 0, limit)
        if idx <= 0:
            idx = limit
        chunks.append(text[:idx])
        text = text[idx:].lstrip("\n")
    return chunks


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def fetch_messages(channel_id: str, limit: int = 20) -> str:
    """Fetch recent messages from a Discord text channel.

    Returns oldest-first list of messages with author, content, timestamp.
    Use this for regular text channels (#troubleshooting, #getting-started,
    #general). For forum channels (#bug-reports, #feature-requests), use
    fetch_forum_threads instead.

    Args:
        channel_id: Discord channel ID (numeric string).
        limit: Max messages to fetch (1-100, default 20).
    """
    try:
        headers = _headers()
    except RuntimeError as exc:
        return json.dumps({"error": str(exc)})

    limit = max(1, min(limit, 100))
    url = f"{_API}/channels/{channel_id}/messages?limit={limit}"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)

        if resp.status_code == 429:
            retry_after = resp.json().get("retry_after", "unknown")
            return json.dumps({
                "error": f"Rate limited. Retry after {retry_after}s.",
            })

        if resp.status_code != 200:
            return json.dumps({
                "error": f"Discord API error {resp.status_code}: {resp.text[:200]}",
            })

        messages = resp.json()

    # Discord returns newest-first; reverse to oldest-first
    formatted = [_format_message(m) for m in reversed(messages)]
    return json.dumps({"messages": formatted, "count": len(formatted)})


@mcp.tool()
async def fetch_forum_threads(channel_id: str, limit: int = 10) -> str:
    """Fetch active and recent threads from a Discord forum channel.

    Returns threads with their recent messages. Use this for forum channels
    like #bug-reports and #feature-requests. For regular text channels, use
    fetch_messages instead.

    Args:
        channel_id: Discord forum channel ID (numeric string).
        limit: Max threads to return (default 10).
    """
    try:
        headers = _headers()
    except RuntimeError as exc:
        return json.dumps({"error": str(exc)})

    threads: list[dict] = []

    async with httpx.AsyncClient(timeout=30) as client:
        # Step 1: Get active threads (guild-wide, then filter by parent)
        resp = await client.get(
            f"{_API}/guilds/{_GUILD_ID}/threads/active",
            headers=headers,
        )
        if resp.status_code == 200:
            for t in resp.json().get("threads", []):
                if t.get("parent_id") == channel_id:
                    threads.append(t)

        # Step 2: Get recently archived threads
        resp = await client.get(
            f"{_API}/channels/{channel_id}/threads/archived/public?limit={limit}",
            headers=headers,
        )
        if resp.status_code == 200:
            for t in resp.json().get("threads", []):
                if t["id"] not in {th["id"] for th in threads}:
                    threads.append(t)

        # Trim to limit
        threads = threads[:limit]

        # Step 3: Fetch recent messages for each thread
        result = []
        for t in threads:
            thread_data = {
                "thread_id": t["id"],
                "name": t.get("name", ""),
                "message_count": t.get("message_count", 0),
                "messages": [],
            }

            msg_resp = await client.get(
                f"{_API}/channels/{t['id']}/messages?limit=5",
                headers=headers,
            )
            if msg_resp.status_code == 200:
                msgs = msg_resp.json()
                thread_data["messages"] = [
                    _format_message(m) for m in reversed(msgs)
                ]

            result.append(thread_data)

    return json.dumps({"threads": result, "count": len(result)})


@mcp.tool()
async def send_reply(
    channel_id: str,
    content: str,
    reply_to: str | None = None,
) -> str:
    """Send a message to a Discord channel as the Gen bot.

    Can reply to a specific message (threading) or post standalone.
    Auto-chunks messages exceeding Discord's 2000-char limit.

    Args:
        channel_id: Discord channel or thread ID to post in.
        content: Message text (Discord markdown supported).
        reply_to: Message ID to reply to (creates a threaded reply).
    """
    try:
        headers = _headers()
    except RuntimeError as exc:
        return json.dumps({"error": str(exc)})

    chunks = _chunk_text(content)
    sent_ids: list[str] = []

    async with httpx.AsyncClient(timeout=30) as client:
        for i, chunk in enumerate(chunks):
            payload: dict = {"content": chunk}

            # Only thread the first chunk
            if reply_to and i == 0:
                payload["message_reference"] = {
                    "message_id": reply_to,
                    "fail_if_not_exists": False,
                }

            resp = await client.post(
                f"{_API}/channels/{channel_id}/messages",
                headers=headers,
                json=payload,
            )

            if resp.status_code == 429:
                retry_after = resp.json().get("retry_after", "unknown")
                return json.dumps({
                    "error": f"Rate limited after {len(sent_ids)} chunk(s). "
                    f"Retry after {retry_after}s.",
                    "sent_ids": sent_ids,
                })

            if resp.status_code not in (200, 201):
                return json.dumps({
                    "error": f"Discord API error {resp.status_code}: "
                    f"{resp.text[:200]}",
                    "sent_ids": sent_ids,
                })

            msg_data = resp.json()
            sent_ids.append(msg_data.get("id", ""))

    # WS5 Discord capability SHADOW-gate: observe (never hold) this reply AFTER it is
    # fully sent, so the reply is NEVER delayed by the shadow write. Best-effort; _db is
    # None if the shadow DB is unavailable. Wrapped so a shadow/import problem can never
    # break the reply.
    try:
        from genesis.autonomy.shadow_gate import observe_discord_send

        await observe_discord_send(
            _db, path="reply", verb="reply", risk_class="standard",
            target=channel_id, content=content,
        )
    except Exception:  # noqa: BLE001 — the reply path must never fail on shadow
        logger.debug("send_reply capability shadow observe failed", exc_info=True)

    result_text = (
        f"sent (id: {sent_ids[0]})"
        if len(sent_ids) == 1
        else f"sent {len(sent_ids)} parts (ids: {', '.join(sent_ids)})"
    )
    return json.dumps({"status": "sent", "message": result_text, "sent_ids": sent_ids})
