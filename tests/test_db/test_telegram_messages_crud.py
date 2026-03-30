"""Tests for telegram_messages CRUD operations."""

from genesis.db.crud import telegram_messages


async def test_store_and_query_recent(db):
    """Store a message and retrieve it."""
    await telegram_messages.store(
        db,
        chat_id=100,
        message_id=1,
        sender="user",
        content="hello world",
        timestamp="2026-03-21T10:00:00",
    )
    rows = await telegram_messages.query_recent(db, chat_id=100)
    assert len(rows) == 1
    assert rows[0]["sender"] == "user"
    assert rows[0]["content"] == "hello world"


async def test_duplicate_ignored(db):
    """Storing the same chat_id + message_id twice is silently ignored."""
    await telegram_messages.store(
        db,
        chat_id=100,
        message_id=1,
        sender="user",
        content="first",
        timestamp="2026-03-21T10:00:00",
    )
    await telegram_messages.store(
        db,
        chat_id=100,
        message_id=1,
        sender="user",
        content="duplicate",
        timestamp="2026-03-21T10:00:01",
    )
    rows = await telegram_messages.query_recent(db, chat_id=100)
    assert len(rows) == 1
    assert rows[0]["content"] == "first"  # original preserved


async def test_query_recent_limit(db):
    """Limit parameter works correctly."""
    for i in range(10):
        await telegram_messages.store(
            db,
            chat_id=100,
            message_id=i,
            sender="user",
            content=f"msg {i}",
            timestamp=f"2026-03-21T10:00:{i:02d}",
        )
    rows = await telegram_messages.query_recent(db, chat_id=100, limit=3)
    assert len(rows) == 3
    # Should be chronological (oldest first due to reverse)
    assert rows[0]["content"] == "msg 7"
    assert rows[2]["content"] == "msg 9"


async def test_query_recent_chronological_order(db):
    """Results are returned in chronological order (oldest first)."""
    await telegram_messages.store(
        db, chat_id=100, message_id=1, sender="user",
        content="first", timestamp="2026-03-21T10:00:00",
    )
    await telegram_messages.store(
        db, chat_id=100, message_id=2, sender="genesis",
        content="second", timestamp="2026-03-21T10:00:01",
    )
    rows = await telegram_messages.query_recent(db, chat_id=100)
    assert rows[0]["content"] == "first"
    assert rows[1]["content"] == "second"


async def test_query_recent_by_thread(db):
    """Thread filtering works."""
    await telegram_messages.store(
        db, chat_id=100, message_id=1, sender="user",
        content="thread A", thread_id=42, timestamp="2026-03-21T10:00:00",
    )
    await telegram_messages.store(
        db, chat_id=100, message_id=2, sender="user",
        content="thread B", thread_id=99, timestamp="2026-03-21T10:00:01",
    )
    rows = await telegram_messages.query_recent(db, chat_id=100, thread_id=42)
    assert len(rows) == 1
    assert rows[0]["content"] == "thread A"


async def test_search(db):
    """Keyword search works."""
    await telegram_messages.store(
        db, chat_id=100, message_id=1, sender="user",
        content="the quick brown fox", timestamp="2026-03-21T10:00:00",
    )
    await telegram_messages.store(
        db, chat_id=100, message_id=2, sender="user",
        content="lazy dog", timestamp="2026-03-21T10:00:01",
    )
    rows = await telegram_messages.search(db, chat_id=100, query="brown")
    assert len(rows) == 1
    assert "brown" in rows[0]["content"]


async def test_search_no_results(db):
    """Search returns empty list when nothing matches."""
    rows = await telegram_messages.search(db, chat_id=100, query="nonexistent")
    assert rows == []


async def test_query_all_recent(db):
    """query_all_recent returns messages across all chats."""
    await telegram_messages.store(
        db, chat_id=100, message_id=1, sender="user",
        content="chat 100", timestamp="2026-03-21T10:00:00",
    )
    await telegram_messages.store(
        db, chat_id=200, message_id=1, sender="user",
        content="chat 200", timestamp="2026-03-21T10:00:01",
    )
    rows = await telegram_messages.query_all_recent(db, limit=10)
    assert len(rows) == 2


async def test_search_all(db):
    """search_all returns matches across all chats."""
    await telegram_messages.store(
        db, chat_id=100, message_id=1, sender="user",
        content="hello from chat 100", timestamp="2026-03-21T10:00:00",
    )
    await telegram_messages.store(
        db, chat_id=200, message_id=1, sender="user",
        content="hello from chat 200", timestamp="2026-03-21T10:00:01",
    )
    rows = await telegram_messages.search_all(db, "hello")
    assert len(rows) == 2


async def test_negative_message_id_for_genesis_responses(db):
    """Negative message_ids work for genesis responses (no collision with user IDs)."""
    await telegram_messages.store(
        db, chat_id=100, message_id=5, sender="user",
        content="question", timestamp="2026-03-21T10:00:00",
    )
    await telegram_messages.store(
        db, chat_id=100, message_id=-5, sender="genesis",
        content="answer", timestamp="2026-03-21T10:00:01",
    )
    rows = await telegram_messages.query_recent(db, chat_id=100)
    assert len(rows) == 2
    assert rows[0]["sender"] == "user"
    assert rows[1]["sender"] == "genesis"


async def test_reply_to_message_id(db):
    """reply_to_message_id is stored correctly."""
    await telegram_messages.store(
        db, chat_id=100, message_id=1, sender="user",
        content="original", timestamp="2026-03-21T10:00:00",
    )
    await telegram_messages.store(
        db, chat_id=100, message_id=2, sender="user",
        content="reply", reply_to_message_id=1, timestamp="2026-03-21T10:00:01",
    )
    rows = await telegram_messages.query_recent(db, chat_id=100)
    assert rows[1]["reply_to_message_id"] == 1


async def test_store_defaults_timestamp(db):
    """Timestamp defaults to current time if not provided."""
    await telegram_messages.store(
        db, chat_id=100, message_id=1, sender="user",
        content="no ts",
    )
    rows = await telegram_messages.query_recent(db, chat_id=100)
    assert len(rows) == 1
    assert rows[0]["timestamp"]  # not empty
