"""Tests for task intent -> observation emission."""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables, seed_data


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await seed_data(conn)
        yield conn


@pytest.mark.asyncio
async def test_task_intent_creates_observation(db):
    """When intent parser detects task_requested, observation is stored."""
    from genesis.cc.conversation import ConversationLoop
    from genesis.cc.types import ChannelType, IntentResult

    handler = ConversationLoop.__new__(ConversationLoop)
    handler._db = db
    handler._intent_parser = MagicMock()
    handler._intent_parser.parse.return_value = IntentResult(
        raw_text="please fix the bug",
        task_requested=True,
        cleaned_text="fix the bug",
    )
    handler._session_mgr = AsyncMock()
    handler._session_mgr.get_or_create_foreground = AsyncMock(return_value={
        "id": "sess-1", "model": "sonnet", "effort": "medium",
        "cc_session_id": None,
    })
    handler._assembler = AsyncMock()
    handler._assembler.assemble = AsyncMock(return_value="sys prompt")
    handler._on_message_callbacks = []
    handler._session_locks = {}

    with patch("genesis.cc.conversation.cc_sessions") as mock_sessions:
        mock_sessions.get_active_foreground = AsyncMock(return_value=None)

        handler._get_lock = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(), __aexit__=AsyncMock(),
        ))
        handler._persist_overrides = AsyncMock()
        handler._enrich_with_context = AsyncMock(return_value="enriched")
        handler._try_invoke = AsyncMock(side_effect=Exception("stop here"))

        with contextlib.suppress(Exception):
            await handler.handle_message(
                text="please fix the bug",
                user_id="user-1",
                channel=ChannelType.WEB,
            )

    cursor = await db.execute(
        "SELECT * FROM observations WHERE type = 'task_detected'"
    )
    rows = await cursor.fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["source"] == "conversation_intent"
    assert row["content"] == "fix the bug"


@pytest.mark.asyncio
async def test_no_observation_when_no_task(db):
    """When task_requested is False, no observation is created."""
    from genesis.cc.conversation import ConversationLoop
    from genesis.cc.types import ChannelType, IntentResult

    handler = ConversationLoop.__new__(ConversationLoop)
    handler._db = db
    handler._intent_parser = MagicMock()
    handler._intent_parser.parse.return_value = IntentResult(
        raw_text="hello there",
        task_requested=False,
        cleaned_text="hello there",
    )
    handler._session_mgr = AsyncMock()
    handler._session_mgr.get_or_create_foreground = AsyncMock(return_value={
        "id": "sess-1", "model": "sonnet", "effort": "medium",
        "cc_session_id": None,
    })
    handler._assembler = AsyncMock()
    handler._assembler.assemble = AsyncMock(return_value="sys prompt")
    handler._on_message_callbacks = []
    handler._session_locks = {}

    with patch("genesis.cc.conversation.cc_sessions") as mock_sessions:
        mock_sessions.get_active_foreground = AsyncMock(return_value=None)
        handler._get_lock = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(), __aexit__=AsyncMock(),
        ))
        handler._persist_overrides = AsyncMock()
        handler._enrich_with_context = AsyncMock(return_value="enriched")
        handler._try_invoke = AsyncMock(side_effect=Exception("stop here"))

        with contextlib.suppress(Exception):
            await handler.handle_message(
                text="hello there",
                user_id="user-1",
                channel=ChannelType.WEB,
            )

    cursor = await db.execute(
        "SELECT * FROM observations WHERE type = 'task_detected'"
    )
    rows = await cursor.fetchall()
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_streaming_task_intent_creates_observation(db):
    """handle_message_streaming also creates task_detected observations."""
    from genesis.cc.conversation import ConversationLoop
    from genesis.cc.types import ChannelType, IntentResult

    handler = ConversationLoop.__new__(ConversationLoop)
    handler._db = db
    handler._intent_parser = MagicMock()
    handler._intent_parser.parse.return_value = IntentResult(
        raw_text="/task fix the login bug",
        task_requested=True,
        cleaned_text="fix the login bug",
    )
    handler._session_mgr = AsyncMock()
    handler._session_mgr.get_or_create_foreground = AsyncMock(return_value={
        "id": "sess-1", "model": "sonnet", "effort": "medium",
        "cc_session_id": None, "message_count": 0,
    })
    handler._assembler = AsyncMock()
    handler._assembler.assemble = AsyncMock(return_value="sys prompt")
    handler._on_message_callbacks = []
    handler._session_locks = {}

    with patch("genesis.cc.conversation.cc_sessions") as mock_sessions:
        mock_sessions.get_active_foreground = AsyncMock(return_value=None)
        handler._get_lock = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(), __aexit__=AsyncMock(),
        ))
        handler._persist_overrides = AsyncMock()
        handler._enrich_with_context = AsyncMock(return_value="enriched")
        handler._build_recovery_context = AsyncMock(return_value="")
        handler._try_invoke_streaming = AsyncMock(side_effect=Exception("stop"))

        with contextlib.suppress(Exception):
            await handler.handle_message_streaming(
                text="/task fix the login bug",
                user_id="user-1",
                channel=ChannelType.TELEGRAM,
            )

    cursor = await db.execute(
        "SELECT * FROM observations WHERE type = 'task_detected'"
    )
    rows = await cursor.fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["source"] == "conversation_intent"
    assert row["content"] == "fix the login bug"
