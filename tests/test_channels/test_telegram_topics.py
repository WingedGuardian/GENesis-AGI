"""Tests for TopicManager — persistent category-based forum topics."""

from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.channels.telegram.topics import TopicManager


@pytest.fixture
def bot():
    b = AsyncMock()
    topic = MagicMock()
    topic.message_thread_id = 42
    b.create_forum_topic = AsyncMock(return_value=topic)
    b.close_forum_topic = AsyncMock()
    msg = MagicMock()
    msg.message_id = 100
    b.send_message = AsyncMock(return_value=msg)
    return b


@pytest.fixture
def manager(bot):
    return TopicManager(bot, forum_chat_id=12345)


@pytest.mark.asyncio
async def test_get_or_create_persistent(manager, bot):
    thread_id = await manager.get_or_create_persistent("conversation", "Conversation")
    assert thread_id == 42
    bot.create_forum_topic.assert_called_once_with(
        chat_id=12345, name="Conversation",
    )


@pytest.mark.asyncio
async def test_get_or_create_reuses(manager, bot):
    t1 = await manager.get_or_create_persistent("conversation")
    t2 = await manager.get_or_create_persistent("conversation")
    assert t1 == t2
    assert bot.create_forum_topic.call_count == 1


@pytest.mark.asyncio
async def test_send_to_category(manager, bot):
    msg_id = await manager.send_to_category("alert", "System alert!")
    assert msg_id == "100"
    bot.send_message.assert_called_once_with(
        chat_id=12345, text="System alert!",
        message_thread_id=42, parse_mode="HTML",
    )


@pytest.mark.asyncio
async def test_send_to_unknown_category_creates(manager, bot):
    """Sending to an uncreated category auto-creates the topic."""
    msg_id = await manager.send_to_category("background", "Reflection output")
    assert msg_id == "100"
    bot.create_forum_topic.assert_called_once()


@pytest.mark.asyncio
async def test_get_thread_id_before_create(manager):
    assert manager.get_thread_id("conversation") is None


@pytest.mark.asyncio
async def test_get_thread_id_after_create(manager):
    await manager.get_or_create_persistent("conversation")
    assert manager.get_thread_id("conversation") == 42


@pytest.mark.asyncio
async def test_create_no_admin(bot):
    bot.create_forum_topic.side_effect = Exception("Forbidden")
    manager = TopicManager(bot, forum_chat_id=12345)
    result = await manager.get_or_create_persistent("conversation")
    assert result is None


@pytest.mark.asyncio
async def test_name_truncation(manager, bot):
    long_name = "A" * 200
    await manager.get_or_create_persistent("test", long_name)
    call_name = bot.create_forum_topic.call_args[1]["name"]
    assert len(call_name) == 128


@pytest.mark.asyncio
async def test_permission_warning_once(bot):
    """Permission error logs warning only on first failure."""
    bot.create_forum_topic.side_effect = Exception("Forbidden")
    manager = TopicManager(bot, forum_chat_id=12345)

    await manager.get_or_create_persistent("cat1")
    await manager.get_or_create_persistent("cat2")

    assert manager._permission_warned is True


@pytest.mark.asyncio
async def test_resolve_outreach_category(manager):
    assert manager.resolve_outreach_category("blocker") == "alert"
    assert manager.resolve_outreach_category("alert") == "alert"
    assert manager.resolve_outreach_category("surplus") == "surplus"
    assert manager.resolve_outreach_category("recon") == "recon"
    assert manager.resolve_outreach_category("unknown") == "surplus"


@pytest.mark.asyncio
async def test_default_categories(manager):
    """Default categories include per-depth reflections, surplus, recon."""
    cats = manager._categories
    assert "conversation" in cats
    assert "morning_report" in cats
    assert "alert" in cats
    assert "reflection_micro" in cats
    assert "reflection_light" in cats
    assert "reflection_deep" in cats
    assert "reflection_strategic" in cats
    assert "surplus" in cats
    assert "recon" in cats


@pytest.mark.asyncio
async def test_db_persistence_round_trip(bot):
    """Topics survive restart: persist to DB, load into a new TopicManager."""
    from genesis.db.schema import create_all_tables

    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_all_tables(db)
    await db.commit()

    # First manager: create a topic and persist it
    mgr1 = TopicManager(bot, forum_chat_id=12345, db=db)
    thread_id = await mgr1.get_or_create_persistent("conversation", "Conversation")
    assert thread_id == 42

    # Second manager: load from DB without creating
    mgr2 = TopicManager(bot, forum_chat_id=12345, db=db)
    assert mgr2.get_thread_id("conversation") is None  # Not loaded yet
    await mgr2.load_persisted()
    assert mgr2.get_thread_id("conversation") == 42  # Survived restart

    # Verify no extra create_forum_topic call (only 1 from mgr1)
    assert bot.create_forum_topic.call_count == 1

    await db.close()
