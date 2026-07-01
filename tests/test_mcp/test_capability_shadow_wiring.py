"""PATH B/C wiring: outreach_poll and send_reply observe Discord sends (shadow) yet still post."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

import genesis.mcp.discord_bot_mcp as bot_mod
import genesis.mcp.outreach_mcp as outreach_mod
from genesis.db.crud import capability_shadow
from genesis.db.schema import create_all_tables
from genesis.mcp.discord_bot_mcp import mcp as bot_mcp
from genesis.mcp.outreach_mcp import mcp as outreach_mcp


@pytest.fixture(autouse=True)
def _reset_table_cache():
    capability_shadow._table_verified = False
    yield
    capability_shadow._table_verified = False


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


def _mock_httpx(msg_id):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"id": msg_id}
    resp.raise_for_status = MagicMock()
    client = AsyncMock()
    client.post.return_value = resp
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


# ── PATH B: outreach_poll (webhook) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_outreach_poll_records_shadow_and_still_posts(db):
    old_db = outreach_mod._db
    outreach_mod._db = db
    try:
        client = _mock_httpx("poll-1")
        env = {"DISCORD_WEBHOOK_ANNOUNCEMENTS": "https://discord.com/api/webhooks/1/tok"}
        with patch.dict("os.environ", env, clear=False), \
             patch("genesis.mcp.outreach_mcp.httpx.AsyncClient", return_value=client):
            tools = await outreach_mcp.get_tools()
            result = await tools["outreach_poll"].fn(
                channel="announcements", question="What next?", answers=["A", "B"],
            )
        assert json.loads(result)["status"] == "created"
        client.post.assert_called_once()  # the poll still posted
        rows = await capability_shadow.list_recent(db)
        assert len(rows) == 1
        r = rows[0]
        assert r["path"] == "poll" and r["cell_verb"] == "poll"
        assert r["cell_risk_class"] == "bulk" and r["target"] == "announcements"
        assert r["would_hold"] == 1
    finally:
        outreach_mod._db = old_db


# ── PATH C: send_reply (Discord API) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_send_reply_records_shadow_and_still_sends(db):
    old_token, old_db = bot_mod._bot_token, bot_mod._db
    bot_mod._bot_token, bot_mod._db = "test-token", db
    try:
        client = _mock_httpx("reply-1")
        with patch("genesis.mcp.discord_bot_mcp.httpx.AsyncClient", return_value=client):
            tools = await bot_mcp.get_tools()
            result = await tools["send_reply"].fn(channel_id="789", content="thanks!")
        assert json.loads(result)["status"] == "sent"
        client.post.assert_called_once()  # the reply still sent
        rows = await capability_shadow.list_recent(db)
        assert len(rows) == 1
        r = rows[0]
        assert r["path"] == "reply" and r["cell_verb"] == "reply"
        assert r["cell_risk_class"] == "standard" and r["target"] == "789"
        assert r["would_hold"] == 1
    finally:
        bot_mod._bot_token, bot_mod._db = old_token, old_db


@pytest.mark.asyncio
async def test_send_reply_without_shadow_db_still_sends():
    # _db=None (shadow DB unavailable) => shadow is a no-op; the reply MUST still work.
    old_token, old_db = bot_mod._bot_token, bot_mod._db
    bot_mod._bot_token, bot_mod._db = "test-token", None
    try:
        client = _mock_httpx("reply-2")
        with patch("genesis.mcp.discord_bot_mcp.httpx.AsyncClient", return_value=client):
            tools = await bot_mcp.get_tools()
            result = await tools["send_reply"].fn(channel_id="789", content="hi")
        assert json.loads(result)["status"] == "sent"
        client.post.assert_called_once()
    finally:
        bot_mod._bot_token, bot_mod._db = old_token, old_db
