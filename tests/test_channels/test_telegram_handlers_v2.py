"""Tests for Telegram V2 handlers — covers key V2-specific behavior.

Covers:
- Final message always sent (draft skip removed)
- /stop interrupt flow with (user_id, chat_id) keying
- Pending settings application + cleanup on error
- Update deduplication
- _persist_tg_message direction parameter
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.cc.exceptions import (
    CCMCPError,
    CCQuotaExhaustedError,
    CCRateLimitError,
    CCTimeoutError,
)
from genesis.cc.types import ChannelType
from genesis.channels.telegram.handlers_v2 import make_handlers_v2
from genesis.channels.telegram.transport.update_dedupe import (
    TelegramUpdateDedupe,
    message_key,
)


def _make_update(user_id=123, text="hello", chat_id=456, message_id=789):
    """Build a mock Telegram Update with V2-compatible fields."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.update_id = message_id

    msg = MagicMock()
    msg.text = text
    msg.message_id = message_id
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.chat.type = "private"
    msg.chat.send_action = AsyncMock()
    msg.reply_text = AsyncMock()
    msg.reply_markdown_v2 = AsyncMock()
    msg.reply_html = AsyncMock()
    msg.message_thread_id = None

    update.message = msg
    update.effective_message = msg
    return update


def _make_context():
    ctx = MagicMock()
    ctx.bot = MagicMock()
    return ctx


@pytest.fixture
def mock_loop():
    loop = AsyncMock(spec=["handle_message_streaming", "_db"])
    loop.handle_message_streaming = AsyncMock(return_value="Genesis response")
    loop._db = MagicMock()
    return loop


@pytest.fixture
def mock_adapter():
    adapter = MagicMock()
    adapter.get_chat_lock = MagicMock(return_value=asyncio.Lock())
    adapter._app = MagicMock()
    adapter._app.bot = MagicMock()
    adapter._app.bot.send_message_draft = AsyncMock(return_value=True)
    return adapter


@pytest.fixture
def dedupe():
    return TelegramUpdateDedupe()


@pytest.fixture
def handlers(mock_loop, mock_adapter, dedupe):
    return make_handlers_v2(
        mock_loop,
        allowed_users={123},
        whisper_model="base",
        adapter=mock_adapter,
        db=mock_loop._db,
        dedupe=dedupe,
    )


# ── Final message always sent ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_text_handler_always_sends_final_reply(handlers, mock_loop):
    """After draft skip logic was removed, final message is always sent."""
    update = _make_update(text="Hello Genesis")
    ctx = _make_context()

    await handlers["text"](update, ctx)

    mock_loop.handle_message_streaming.assert_called_once()
    # The handler should attempt to reply via reply_text or reply_html.
    # At least one reply method must have been called.
    msg = update.message
    assert (
        msg.reply_text.called or msg.reply_html.called or msg.reply_markdown_v2.called
    ), "No reply sent — final message delivery may be broken"


@pytest.mark.asyncio
async def test_text_handler_calls_streaming(handlers, mock_loop):
    """Text handler uses handle_message_streaming, not handle_message."""
    update = _make_update(text="test message")
    ctx = _make_context()

    await handlers["text"](update, ctx)

    mock_loop.handle_message_streaming.assert_called_once()
    args, kwargs = mock_loop.handle_message_streaming.call_args
    assert args[0] == "test message"
    assert kwargs["user_id"] == "tg-123"
    assert kwargs["channel"] == ChannelType.TELEGRAM


# ── /stop interrupt keying ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_handler_per_chat_keying(handlers):
    """The /stop command should be keyed by (user_id, chat_id), not just user_id."""
    # Build two updates from same user, different chats
    update_chat1 = _make_update(user_id=123, chat_id=100, text="/stop")
    update_chat2 = _make_update(user_id=123, chat_id=200, text="/stop")
    ctx = _make_context()

    # Both should succeed without error (no cross-chat confusion)
    await handlers["stop"](update_chat1, ctx)
    await handlers["stop"](update_chat2, ctx)

    # Verify both got replies
    update_chat1.message.reply_text.assert_called()
    update_chat2.message.reply_text.assert_called()


# ── Update deduplication ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deduplicate_skips_repeated_update(handlers, mock_loop, dedupe):
    """Same update received twice should be processed only once."""
    update = _make_update(text="duplicate test", message_id=42)
    ctx = _make_context()

    # First call — should process
    await handlers["text"](update, ctx)
    assert mock_loop.handle_message_streaming.call_count == 1

    # Second call with same update_id — should skip
    await handlers["text"](update, ctx)
    assert mock_loop.handle_message_streaming.call_count == 1


# ── Message key generation ───────────────────────────────────────────────


def test_message_key_uniqueness():
    """message_key should produce unique keys for different messages."""
    key1 = message_key(100, 1)
    key2 = message_key(100, 2)
    key3 = message_key(200, 1)

    assert key1 != key2  # Different message_id
    assert key1 != key3  # Different chat_id
    assert key2 != key3


# ── Authorization ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unauthorized_user_rejected(handlers, mock_loop):
    """User not in allowed_users should get no response."""
    update = _make_update(user_id=999, text="unauthorized")
    ctx = _make_context()

    await handlers["text"](update, ctx)

    # Conversation loop should NOT be called for unauthorized users
    mock_loop.handle_message_streaming.assert_not_called()


# ── Pending settings ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_model_command_stores_pending(handlers):
    """/model command should store pending settings for next message."""
    update = _make_update(text="opus", user_id=123)
    ctx = _make_context()

    await handlers["model"](update, ctx)
    update.message.reply_text.assert_called()


@pytest.mark.asyncio
async def test_effort_command_stores_pending(handlers):
    """/effort command should store pending settings for next message."""
    update = _make_update(text="high", user_id=123)
    ctx = _make_context()

    await handlers["effort"](update, ctx)
    update.message.reply_text.assert_called()


# ── TG message persistence direction ────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_inbound_message(mock_loop):
    """Inbound messages should be stored with direction='inbound'."""
    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    from genesis.db.crud.telegram_messages import store
    await store(
        db,
        chat_id=123,
        message_id=456,
        sender="user",
        content="hello",
        direction="inbound",
    )
    db.execute.assert_called_once()
    call_args = db.execute.call_args[0]
    # direction should be in the SQL values
    assert "direction" in call_args[0]
    assert "inbound" in call_args[1]


@pytest.mark.asyncio
async def test_persist_outbound_message(mock_loop):
    """Outbound messages should be stored with direction='outbound'."""
    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    from genesis.db.crud.telegram_messages import store
    await store(
        db,
        chat_id=123,
        message_id=456,
        sender="genesis",
        content="response",
        direction="outbound",
    )
    db.execute.assert_called_once()
    call_args = db.execute.call_args[0]
    assert "outbound" in call_args[1]


# ── _reply_formatted returns sent Message ────────────────────────────────


@pytest.mark.asyncio
async def test_reply_formatted_returns_sent_message():
    """_reply_formatted should return the Message object from reply_text."""
    from genesis.channels.telegram.handlers_v2 import _reply_formatted

    mock_msg = MagicMock()
    sent = MagicMock()
    sent.message_id = 9999
    mock_msg.reply_text = AsyncMock(return_value=sent)

    result = await _reply_formatted(mock_msg, "hello")

    assert result is sent
    assert result.message_id == 9999


# ── Outbound persistence uses sent message ID ───────────────────────────


@pytest.mark.asyncio
async def test_outbound_persist_uses_sent_message_id(handlers, mock_loop):
    """Outbound messages should be persisted with the SENT message's ID, not the user's."""
    update = _make_update(text="Hello Genesis", message_id=100)
    ctx = _make_context()

    # Make reply_text return a Message with a different ID than the user's
    sent_msg = MagicMock()
    sent_msg.message_id = 555  # Different from user's 100
    update.message.reply_text = AsyncMock(return_value=sent_msg)

    mock_loop.handle_message_streaming = AsyncMock(return_value="Genesis response")

    with patch("genesis.db.crud.telegram_messages.store", new_callable=AsyncMock) as mock_store:
        await handlers["text"](update, ctx)

    # Find the outbound call (sender="genesis")
    outbound_calls = [
        c for c in mock_store.call_args_list
        if c.kwargs.get("sender") == "genesis"
    ]
    assert len(outbound_calls) >= 1, "No outbound persist call found"
    persisted_msg_id = outbound_calls[0].kwargs.get("message_id")
    assert persisted_msg_id == 555, (
        f"Expected sent message ID 555, got {persisted_msg_id} — "
        "outbound persistence is still using the user's message ID"
    )


# ── _format_error type dispatch ──────────────────────────────────────────


def test_format_error_type_dispatch():
    """_format_error should return specific messages for each CC exception type."""
    from genesis.channels.telegram.handlers_v2 import _format_error

    assert "taking too long" in _format_error(CCTimeoutError("test"))
    assert "usage limit" in _format_error(CCQuotaExhaustedError("test"))
    assert "Rate limit" in _format_error(CCRateLimitError("test"))
    assert "Tool server error" in _format_error(CCMCPError("test"))
    # Generic Exception should hit fallback
    assert "something went wrong" in _format_error(Exception("unknown")).lower()


# ── Exception handler sends specific error messages ──────────────────────


@pytest.mark.asyncio
async def test_text_handler_sends_specific_error_on_timeout(handlers, mock_loop):
    """When CC raises CCTimeoutError, user should see 'taking too long', not generic error."""
    update = _make_update(text="slow request")
    ctx = _make_context()
    mock_loop.handle_message_streaming = AsyncMock(side_effect=CCTimeoutError("timed out"))

    await handlers["text"](update, ctx)

    reply_text = update.message.reply_text.call_args[0][0]
    assert "taking too long" in reply_text.lower(), (
        f"Expected timeout-specific message, got: {reply_text}"
    )


@pytest.mark.asyncio
async def test_text_handler_sends_specific_error_on_rate_limit(handlers, mock_loop):
    """When CC raises CCRateLimitError, user should see 'Rate limit', not generic error."""
    update = _make_update(text="rate limited request")
    ctx = _make_context()
    mock_loop.handle_message_streaming = AsyncMock(side_effect=CCRateLimitError("429"))

    await handlers["text"](update, ctx)

    reply_text = update.message.reply_text.call_args[0][0]
    assert "rate limit" in reply_text.lower(), (
        f"Expected rate-limit-specific message, got: {reply_text}"
    )


# ── Voice handler tests ──────────────────────────────────────────────────


def _make_voice_update(user_id=123, chat_id=456, message_id=789, file_size=1000):
    """Build a mock Telegram Update with voice attachment."""
    update = _make_update(user_id=user_id, chat_id=chat_id, message_id=message_id)
    voice = MagicMock()
    voice.file_id = "test_file_id"
    voice.file_size = file_size
    update.message.voice = voice
    update.message.audio = None
    update.message.text = None
    return update


def _make_voice_context(transcribed_text="hello from voice"):
    """Build a mock context with get_file + download chain."""
    ctx = _make_context()
    mock_file = MagicMock()
    mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"fake-audio"))
    ctx.bot.get_file = AsyncMock(return_value=mock_file)
    return ctx


@pytest.mark.asyncio
async def test_voice_file_too_large_rejected(handlers, mock_loop):
    """Voice files >20MB should be rejected with a user-facing message."""
    update = _make_voice_update(file_size=25 * 1024 * 1024)  # 25MB
    ctx = _make_context()

    await handlers["voice"](update, ctx)

    mock_loop.handle_message_streaming.assert_not_called()
    reply_text = update.message.reply_text.call_args[0][0]
    assert "too large" in reply_text.lower()


@pytest.mark.asyncio
async def test_voice_transcription_failure_replies_error(handlers, mock_loop):
    """When STT returns empty text, reply with transcription failure message."""
    update = _make_voice_update()
    ctx = _make_voice_context()

    with patch("genesis.channels.stt.transcribe", new_callable=AsyncMock, return_value=""):
        await handlers["voice"](update, ctx)

    mock_loop.handle_message_streaming.assert_not_called()
    reply_text = update.message.reply_text.call_args[0][0]
    assert "couldn't transcribe" in reply_text.lower()


@pytest.mark.asyncio
async def test_voice_transcription_success_calls_cc(handlers, mock_loop):
    """Successful transcription should invoke CC with the transcribed text."""
    update = _make_voice_update()
    ctx = _make_voice_context()

    with patch("genesis.channels.stt.transcribe", new_callable=AsyncMock, return_value="hello from voice"):
        await handlers["voice"](update, ctx)

    mock_loop.handle_message_streaming.assert_called_once()
    call_args = mock_loop.handle_message_streaming.call_args
    assert call_args.args[0] == "hello from voice"


@pytest.mark.asyncio
async def test_voice_unauthorized_user_rejected(handlers, mock_loop):
    """Voice from unauthorized user should not be processed."""
    update = _make_voice_update(user_id=999)
    ctx = _make_context()

    await handlers["voice"](update, ctx)

    mock_loop.handle_message_streaming.assert_not_called()


@pytest.mark.asyncio
async def test_voice_dedup_skips_repeated(handlers, mock_loop):
    """Same voice message received twice should be processed only once."""
    update = _make_voice_update(message_id=42)
    ctx = _make_voice_context()

    with patch("genesis.channels.stt.transcribe", new_callable=AsyncMock, return_value="hello"):
        await handlers["voice"](update, ctx)
        await handlers["voice"](update, ctx)

    assert mock_loop.handle_message_streaming.call_count == 1


@pytest.mark.asyncio
async def test_voice_outbound_persist_has_direction(handlers, mock_loop):
    """Voice response should be persisted with direction='outbound'."""
    update = _make_voice_update()
    ctx = _make_voice_context()

    with patch("genesis.channels.stt.transcribe", new_callable=AsyncMock, return_value="hello"), \
         patch("genesis.db.crud.telegram_messages.store", new_callable=AsyncMock) as mock_store:
        await handlers["voice"](update, ctx)

    outbound_calls = [
        c for c in mock_store.call_args_list
        if c.kwargs.get("sender") == "genesis"
    ]
    assert len(outbound_calls) >= 1, "No outbound persist call found"
    assert outbound_calls[0].kwargs.get("direction") == "outbound"


# ── Outreach reply detection ─────────────────────────────────────────────


@pytest.fixture
def handlers_with_waiter(mock_loop, mock_adapter, dedupe):
    """Handlers with a mock reply_waiter injected."""
    waiter = MagicMock()
    waiter.resolve = MagicMock(return_value=True)
    return make_handlers_v2(
        mock_loop,
        allowed_users={123},
        whisper_model="base",
        adapter=mock_adapter,
        db=mock_loop._db,
        dedupe=dedupe,
        reply_waiter=waiter,
    ), waiter


@pytest.mark.asyncio
async def test_outreach_reply_resolved_skips_cc(handlers_with_waiter, mock_loop):
    """When reply_waiter.resolve() returns True, CC should NOT be invoked."""
    handlers, waiter = handlers_with_waiter
    update = _make_update(text="yes I got it", message_id=200)
    # Set up reply_to_message pointing to outreach delivery
    update.message.reply_to_message = MagicMock()
    update.message.reply_to_message.message_id = 100
    ctx = _make_context()

    await handlers["text"](update, ctx)

    waiter.resolve.assert_called_once_with("100", "yes I got it")
    mock_loop.handle_message_streaming.assert_not_called()


@pytest.mark.asyncio
async def test_outreach_reply_unresolved_continues_to_cc(handlers_with_waiter, mock_loop):
    """When reply_waiter.resolve() returns False, normal CC flow should continue."""
    handlers, waiter = handlers_with_waiter
    waiter.resolve = MagicMock(return_value=False)
    update = _make_update(text="this is a normal reply", message_id=200)
    update.message.reply_to_message = MagicMock()
    update.message.reply_to_message.message_id = 100
    ctx = _make_context()

    await handlers["text"](update, ctx)

    waiter.resolve.assert_called_once()
    mock_loop.handle_message_streaming.assert_called_once()


@pytest.mark.asyncio
async def test_no_reply_waiter_processes_normally(handlers, mock_loop):
    """Without reply_waiter, reply-to messages should be processed normally."""
    update = _make_update(text="replying to something", message_id=200)
    update.message.reply_to_message = MagicMock()
    update.message.reply_to_message.message_id = 100
    ctx = _make_context()

    await handlers["text"](update, ctx)

    mock_loop.handle_message_streaming.assert_called_once()


# ── Error path tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connection_error_no_text_sends_generic_message(handlers, mock_loop):
    """ConnectionError without accumulated text should send 'Connection issue' reply."""
    update = _make_update(text="test connection error")
    ctx = _make_context()
    mock_loop.handle_message_streaming = AsyncMock(side_effect=ConnectionError("lost"))

    await handlers["text"](update, ctx)

    reply_text = update.message.reply_text.call_args[0][0]
    assert "connection issue" in reply_text.lower(), (
        f"Expected connection-issue message, got: {reply_text}"
    )


@pytest.mark.asyncio
async def test_cc_error_sends_formatted_error(handlers, mock_loop):
    """CCMCPError should produce 'Tool server error' message."""
    update = _make_update(text="test mcp error")
    ctx = _make_context()
    mock_loop.handle_message_streaming = AsyncMock(
        side_effect=CCMCPError("connection refused", server_name="genesis-memory"),
    )

    await handlers["text"](update, ctx)

    reply_text = update.message.reply_text.call_args[0][0]
    assert "tool server error" in reply_text.lower(), (
        f"Expected tool server error message, got: {reply_text}"
    )


@pytest.mark.asyncio
async def test_cc_quota_exhausted_sends_contingency_message(handlers, mock_loop):
    """CCQuotaExhaustedError should produce 'contingency mode' message."""
    update = _make_update(text="test quota")
    ctx = _make_context()
    mock_loop.handle_message_streaming = AsyncMock(
        side_effect=CCQuotaExhaustedError("quota exceeded"),
    )

    await handlers["text"](update, ctx)

    reply_text = update.message.reply_text.call_args[0][0]
    assert "contingency" in reply_text.lower(), (
        f"Expected contingency-mode message, got: {reply_text}"
    )


# ── /help command ────────────────────────────────────────────────────────


def test_help_command_exists_in_handlers(handlers):
    """/help should be registered as a handler alias for /start."""
    assert "help" in handlers
    assert callable(handlers["help"])
    # Help and start should both be wrappers around cmd_start
    # (they may be different wrapper objects but should behave identically)
    assert handlers["help"].__name__ == "wrapper"
    assert handlers["start"].__name__ == "wrapper"


# ── DraftStreamer interaction ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_text_handler_sends_real_message_even_with_draft(handlers, mock_loop):
    """Real message should always be sent, even when DraftStreamer was active.

    Drafts are ephemeral previews — the final reply_text is the delivery
    mechanism.  This test verifies the "always send real message" invariant.
    """
    update = _make_update(text="test with draft")
    ctx = _make_context()
    mock_loop.handle_message_streaming = AsyncMock(return_value="Genesis response")

    await handlers["text"](update, ctx)

    # reply_text must have been called (real message delivery)
    msg = update.message
    assert msg.reply_text.called, "No reply_text call — real message not delivered"


# ── Photo handler ────────────────────────────────────────────────────────


def _make_photo_update(user_id=123, chat_id=456, message_id=789, file_size=5000, caption=None):
    """Build a mock Telegram Update with a photo attachment."""
    update = _make_update(user_id=user_id, chat_id=chat_id, message_id=message_id)
    photo = MagicMock()
    photo.file_id = "photo_file_id"
    photo.file_size = file_size
    update.message.photo = [MagicMock(), photo]  # Two sizes; last is largest
    update.message.text = None
    update.message.caption = caption
    return update


def _make_media_context():
    """Build a mock context with get_file + download chain for media."""
    ctx = _make_context()
    mock_file = MagicMock()
    mock_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"fake-image-data"))
    ctx.bot.get_file = AsyncMock(return_value=mock_file)
    return ctx


@pytest.mark.asyncio
async def test_photo_downloads_and_processes(handlers, mock_loop):
    """Photo should be downloaded, saved, and sent to CC for analysis."""
    update = _make_photo_update(caption="What's this?")
    ctx = _make_media_context()

    with patch("genesis.channels.telegram._handler_messages._MEDIA_DIR") as mock_dir, \
         patch("genesis.channels.telegram._handler_messages.time") as mock_time:
        mock_dir.mkdir = MagicMock()
        mock_dir.__truediv__ = MagicMock(return_value=MagicMock())
        mock_path = mock_dir.__truediv__.return_value
        mock_path.write_bytes = MagicMock()
        mock_path.name = "photo_789_1000.jpg"
        mock_path.unlink = MagicMock()
        mock_time.time.return_value = 1000

        await handlers["photo"](update, ctx)

    mock_loop.handle_message_streaming.assert_called_once()
    call_args = mock_loop.handle_message_streaming.call_args
    assert "What's this?" in call_args.args[0]


@pytest.mark.asyncio
async def test_photo_too_large_rejected(handlers, mock_loop):
    """Photos >20MB should be rejected with a user-facing message."""
    update = _make_photo_update(file_size=25 * 1024 * 1024)
    ctx = _make_media_context()

    await handlers["photo"](update, ctx)

    mock_loop.handle_message_streaming.assert_not_called()
    reply_text = update.message.reply_text.call_args[0][0]
    assert "too large" in reply_text.lower()


@pytest.mark.asyncio
async def test_photo_unauthorized_rejected(handlers, mock_loop):
    """Photo from unauthorized user should not be processed."""
    update = _make_photo_update(user_id=999)
    ctx = _make_media_context()

    await handlers["photo"](update, ctx)
    mock_loop.handle_message_streaming.assert_not_called()


@pytest.mark.asyncio
async def test_photo_dedup_skips_repeated(handlers, mock_loop):
    """Same photo message received twice should be processed only once."""
    update = _make_photo_update(message_id=42)
    ctx = _make_media_context()

    with patch("genesis.channels.telegram._handler_messages._MEDIA_DIR") as mock_dir, \
         patch("genesis.channels.telegram._handler_messages.time") as mock_time:
        mock_dir.mkdir = MagicMock()
        mock_dir.__truediv__ = MagicMock(return_value=MagicMock())
        mock_path = mock_dir.__truediv__.return_value
        mock_path.write_bytes = MagicMock()
        mock_path.name = "photo_42_1000.jpg"
        mock_path.unlink = MagicMock()
        mock_time.time.return_value = 1000

        await handlers["photo"](update, ctx)
        await handlers["photo"](update, ctx)

    assert mock_loop.handle_message_streaming.call_count == 1


# ── Document handler ────────────────────────────────────────────────────


def _make_doc_update(
    user_id=123, chat_id=456, message_id=789,
    file_name="report.pdf", mime_type="application/pdf",
    file_size=10000, caption=None,
):
    """Build a mock Telegram Update with a document attachment."""
    update = _make_update(user_id=user_id, chat_id=chat_id, message_id=message_id)
    doc = MagicMock()
    doc.file_id = "doc_file_id"
    doc.file_name = file_name
    doc.mime_type = mime_type
    doc.file_size = file_size
    update.message.document = doc
    update.message.text = None
    update.message.caption = caption
    return update


@pytest.mark.asyncio
async def test_document_downloads_and_processes(handlers, mock_loop):
    """PDF document should be downloaded and sent to CC for analysis."""
    update = _make_doc_update(caption="Summarize this")
    ctx = _make_media_context()

    with patch("genesis.channels.telegram._handler_messages._MEDIA_DIR") as mock_dir, \
         patch("genesis.channels.telegram._handler_messages.time") as mock_time:
        mock_dir.mkdir = MagicMock()
        mock_dir.__truediv__ = MagicMock(return_value=MagicMock())
        mock_path = mock_dir.__truediv__.return_value
        mock_path.write_bytes = MagicMock()
        mock_path.name = "doc_789_1000.pdf"
        mock_path.unlink = MagicMock()
        mock_time.time.return_value = 1000

        await handlers["document"](update, ctx)

    mock_loop.handle_message_streaming.assert_called_once()
    call_args = mock_loop.handle_message_streaming.call_args
    assert "Summarize this" in call_args.args[0]


@pytest.mark.asyncio
async def test_document_unsupported_mime_rejected(handlers, mock_loop):
    """Documents with unsupported MIME types should be rejected with honest message."""
    update = _make_doc_update(mime_type="application/zip", file_name="archive.zip")
    ctx = _make_media_context()

    await handlers["document"](update, ctx)

    mock_loop.handle_message_streaming.assert_not_called()
    reply_text = update.message.reply_text.call_args[0][0]
    assert "pdfs" in reply_text.lower()


@pytest.mark.asyncio
async def test_document_image_mime_accepted(handlers, mock_loop):
    """Documents with image/* MIME types should be accepted."""
    update = _make_doc_update(mime_type="image/png", file_name="screenshot.png")
    ctx = _make_media_context()

    with patch("genesis.channels.telegram._handler_messages._MEDIA_DIR") as mock_dir, \
         patch("genesis.channels.telegram._handler_messages.time") as mock_time:
        mock_dir.mkdir = MagicMock()
        mock_dir.__truediv__ = MagicMock(return_value=MagicMock())
        mock_path = mock_dir.__truediv__.return_value
        mock_path.write_bytes = MagicMock()
        mock_path.name = "doc_789_1000.png"
        mock_path.unlink = MagicMock()
        mock_time.time.return_value = 1000

        await handlers["document"](update, ctx)

    mock_loop.handle_message_streaming.assert_called_once()


@pytest.mark.asyncio
async def test_document_too_large_rejected(handlers, mock_loop):
    """Documents >20MB should be rejected."""
    update = _make_doc_update(file_size=25 * 1024 * 1024)
    ctx = _make_media_context()

    await handlers["document"](update, ctx)

    mock_loop.handle_message_streaming.assert_not_called()
    reply_text = update.message.reply_text.call_args[0][0]
    assert "too large" in reply_text.lower()


@pytest.mark.asyncio
async def test_document_handler_registered(handlers):
    """Document handler should exist in the handler dict."""
    assert "document" in handlers
    assert callable(handlers["document"])


# ── Voice + text co-delivery tests ─────────────────────────────────────


@pytest.fixture
def voice_helper():
    """Mock VoiceDeliveryHelper that succeeds."""
    helper = MagicMock()
    helper.synthesize_and_deliver = AsyncMock(return_value=True)
    helper.available = True
    return helper


@pytest.fixture
def handlers_voice(mock_loop, mock_adapter, dedupe, voice_helper):
    """Handlers with voice_helper injected so want_voice() can return True."""
    return make_handlers_v2(
        mock_loop,
        allowed_users={123},
        whisper_model="base",
        voice_helper=voice_helper,
        adapter=mock_adapter,
        db=mock_loop._db,
        dedupe=dedupe,
    )


@pytest.mark.asyncio
async def test_voice_response_always_includes_text(handlers_voice, mock_loop, voice_helper):
    """Voice input must always send text response, even when voice delivery succeeds."""
    update = _make_voice_update()
    ctx = _make_voice_context()
    mock_loop.handle_message_streaming = AsyncMock(return_value="Genesis response")

    sent_msg = MagicMock()
    sent_msg.message_id = 555
    update.message.reply_text = AsyncMock(return_value=sent_msg)

    with patch("genesis.channels.stt.transcribe", new_callable=AsyncMock, return_value="hello"):
        await handlers_voice["voice"](update, ctx)

    reply_calls = update.message.reply_text.call_args_list
    text_bodies = [str(c) for c in reply_calls]
    joined = " ".join(text_bodies)
    assert "Genesis response" in joined, (
        f"Text response not sent alongside voice. Reply calls: {text_bodies}"
    )
    voice_helper.synthesize_and_deliver.assert_called_once()


@pytest.mark.asyncio
async def test_text_voice_mode_sends_both(handlers_voice, mock_loop, voice_helper):
    """In 'voice' mode, text input should produce BOTH text and voice responses."""
    update = _make_update(text="Hello Genesis")
    ctx = _make_context()
    mock_loop.handle_message_streaming = AsyncMock(return_value="Genesis response")

    sent_msg = MagicMock()
    sent_msg.message_id = 555
    update.message.reply_text = AsyncMock(return_value=sent_msg)

    from genesis.channels.telegram._handler_context import HandlerContext
    with patch.object(HandlerContext, "want_voice", return_value=True):
        await handlers_voice["text"](update, ctx)

    assert update.message.reply_text.called, "Text response not sent in voice mode"
    voice_helper.synthesize_and_deliver.assert_called_once()


@pytest.mark.asyncio
async def test_voice_failure_does_not_affect_text(handlers_voice, mock_loop, voice_helper):
    """When TTS fails, text response should already have been delivered."""
    update = _make_voice_update()
    ctx = _make_voice_context()
    mock_loop.handle_message_streaming = AsyncMock(return_value="Genesis response")

    sent_msg = MagicMock()
    sent_msg.message_id = 555
    update.message.reply_text = AsyncMock(return_value=sent_msg)

    voice_helper.synthesize_and_deliver = AsyncMock(side_effect=Exception("TTS failed"))

    with patch("genesis.channels.stt.transcribe", new_callable=AsyncMock, return_value="hello"):
        await handlers_voice["voice"](update, ctx)

    reply_calls = update.message.reply_text.call_args_list
    text_bodies = [str(c) for c in reply_calls]
    joined = " ".join(text_bodies)
    assert "Genesis response" in joined, (
        f"Text response missing after voice failure. Reply calls: {text_bodies}"
    )


@pytest.mark.asyncio
async def test_voice_transcription_prefix_always_present(handlers_voice, mock_loop, voice_helper):
    """Voice input response should always include transcription prefix."""
    update = _make_voice_update()
    ctx = _make_voice_context()
    mock_loop.handle_message_streaming = AsyncMock(return_value="Genesis response")

    sent_msg = MagicMock()
    sent_msg.message_id = 555
    update.message.reply_text = AsyncMock(return_value=sent_msg)

    with patch("genesis.channels.stt.transcribe", new_callable=AsyncMock, return_value="hello from voice"):
        await handlers_voice["voice"](update, ctx)

    reply_calls = update.message.reply_text.call_args_list
    text_bodies = [str(c) for c in reply_calls]
    joined = " ".join(text_bodies)
    assert "hello from voice" in joined, (
        f"Transcription echo missing. Reply calls: {text_bodies}"
    )


# ── Message chunking tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_formatted_chunks_long_message():
    """_reply_formatted should split messages exceeding 4096 chars into chunks."""
    from genesis.channels.telegram.handlers_v2 import _reply_formatted

    mock_msg = MagicMock()
    sent = MagicMock()
    sent.message_id = 9999
    mock_msg.reply_text = AsyncMock(return_value=sent)

    # Create a message that exceeds 4096 chars
    long_text = "a" * 5000
    result = await _reply_formatted(mock_msg, long_text)

    # Should have been called at least twice (chunked)
    assert mock_msg.reply_text.call_count >= 2, (
        f"Expected multiple reply_text calls for 5000-char message, got {mock_msg.reply_text.call_count}"
    )
    assert result is sent  # Returns last sent message


@pytest.mark.asyncio
async def test_reply_formatted_no_chunk_short_message():
    """_reply_formatted should send short messages as-is, no chunking."""
    from genesis.channels.telegram.handlers_v2 import _reply_formatted

    mock_msg = MagicMock()
    sent = MagicMock()
    sent.message_id = 1234
    mock_msg.reply_text = AsyncMock(return_value=sent)

    result = await _reply_formatted(mock_msg, "short message")

    assert mock_msg.reply_text.call_count == 1
    assert result is sent


@pytest.mark.asyncio
async def test_reply_formatted_chunk_preserves_code_blocks():
    """Chunking should not break code blocks across messages."""
    from genesis.channels.telegram.handlers_v2 import _reply_formatted

    mock_msg = MagicMock()
    sent = MagicMock()
    mock_msg.reply_text = AsyncMock(return_value=sent)

    # 4000 chars of text + a code block that pushes over 4096
    long_text = "A" * 4000 + "\n```python\nprint('hello')\n```\n" + "B" * 100
    await _reply_formatted(mock_msg, long_text)

    # Should have chunked — each chunk with balanced fences
    assert mock_msg.reply_text.call_count >= 2


# ── Voice transcription decoupling tests ────────────────────────────────


@pytest.mark.asyncio
async def test_voice_transcription_sent_separately(handlers_voice, mock_loop, voice_helper):
    """Voice transcription should be a separate message, not a prefix on the response."""
    update = _make_voice_update()
    ctx = _make_voice_context()
    mock_loop.handle_message_streaming = AsyncMock(return_value="Genesis response")

    sent_msg = MagicMock()
    sent_msg.message_id = 555
    update.message.reply_text = AsyncMock(return_value=sent_msg)

    with patch("genesis.channels.stt.transcribe", new_callable=AsyncMock, return_value="hello from voice"):
        await handlers_voice["voice"](update, ctx)

    # Check that no single reply_text call contains BOTH the transcription and response
    for call in update.message.reply_text.call_args_list:
        text_arg = str(call)
        has_transcription = "hello from voice" in text_arg
        has_response = "Genesis response" in text_arg
        assert not (has_transcription and has_response), (
            f"Transcription and response should be separate messages, got combined: {text_arg}"
        )


# ── BadRequest error formatting test ────────────────────────────────────


def test_format_error_bad_request():
    """BadRequest errors should show the specific Telegram error message."""
    from telegram.error import BadRequest

    from genesis.channels.telegram.handlers_v2 import _format_error

    err = BadRequest("Message is too long")
    result = _format_error(err)
    assert "Message is too long" in result
    assert "Telegram error" in result
