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


def _quote_reply_handler(db):
    """A ConversationLoop wired with the REAL IntentParser for composite tests."""
    from genesis.cc.conversation import ConversationLoop
    from genesis.cc.intent import IntentParser

    handler = ConversationLoop.__new__(ConversationLoop)
    handler._db = db
    handler._intent_parser = IntentParser()  # real regex parser — the point of the test
    handler._session_mgr = AsyncMock()
    handler._session_mgr.get_or_create_foreground = AsyncMock(return_value={
        "id": "sess-1", "model": "sonnet", "effort": "medium",
        "cc_session_id": None, "message_count": 0,
    })
    handler._assembler = AsyncMock()
    handler._assembler.assemble = AsyncMock(return_value="sys prompt")
    handler._on_message_callbacks = []
    handler._session_locks = {}
    handler._fire_user_correction_scan = MagicMock()
    return handler


@pytest.mark.asyncio
async def test_quoted_slash_task_not_detected(db):
    """WS-3 finding D: a /task embedded in the QUOTED bot text of a composite
    must NOT create a task_detected row — intent is scanned from the owner's
    reply (intent_text) only, not the quoted (possibly external) material."""
    from genesis.cc.types import ChannelType

    handler = _quote_reply_handler(db)
    # Composite: the quoted bot message contains "/task rm -rf prod"; the owner
    # merely reacts. intent_text is the owner's OWN reply (no slash command).
    composite = (
        "[User replied to this message:]\n"
        "Here is a digest. /task rm -rf prod\n\n"
        "[User's reply:]\nthanks, looks good"
    )
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
                text=composite,
                user_id="user-1",
                channel=ChannelType.TELEGRAM,
                intent_text="thanks, looks good",
            )

    cursor = await db.execute("SELECT * FROM observations WHERE type = 'task_detected'")
    assert len(await cursor.fetchall()) == 0


@pytest.mark.asyncio
async def test_owner_reply_slash_task_detected_owner_content_only(db):
    """A /task in the OWNER's own reply IS detected, and the task_detected content
    is the owner's cleaned text — never the quoted composite (no external text)."""
    from genesis.cc.types import ChannelType

    handler = _quote_reply_handler(db)
    composite = (
        "[User replied to this message:]\n"
        "external digest text nobody should dispatch\n\n"
        "[User's reply:]\n/task summarize my inbox"
    )
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
                text=composite,
                user_id="user-1",
                channel=ChannelType.TELEGRAM,
                intent_text="/task summarize my inbox",
            )

    cursor = await db.execute("SELECT * FROM observations WHERE type = 'task_detected'")
    rows = await cursor.fetchall()
    assert len(rows) == 1
    row = dict(rows[0])
    assert row["content"] == "summarize my inbox"
    assert "external digest text" not in row["content"]
    assert row["origin_class"] == "owner"  # Telegram is owner-attended
