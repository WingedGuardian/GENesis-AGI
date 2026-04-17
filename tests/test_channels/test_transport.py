"""Tests for Telegram V2 transport layer."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from telegram.error import NetworkError

from genesis.channels.telegram.transport.offset_store import (
    delete_offset,
    read_offset,
    write_offset,
)
from genesis.channels.telegram.transport.polling import PollingWatchdog
from genesis.channels.telegram.transport.send_safety import (
    classify_send_error,
    is_safe_to_retry_send,
)
from genesis.channels.telegram.transport.streaming import DraftStreamer, generate_draft_id
from genesis.channels.telegram.transport.typing_breaker import TypingCircuitBreaker
from genesis.channels.telegram.transport.update_dedupe import (
    TelegramUpdateDedupe,
    callback_key,
    message_key,
    update_key,
)

# ---- UpdateDedupe ----

class TestUpdateDedupe:
    def test_first_seen_not_duplicate(self):
        d = TelegramUpdateDedupe()
        assert not d.is_duplicate("test:1")

    def test_second_seen_is_duplicate(self):
        d = TelegramUpdateDedupe()
        d.mark_seen("test:1")
        assert d.is_duplicate("test:1")

    def test_should_skip_idempotent(self):
        d = TelegramUpdateDedupe()
        assert not d.should_skip("key1")  # first time: mark + return False
        assert d.should_skip("key1")  # second time: duplicate

    def test_ttl_eviction(self):
        d = TelegramUpdateDedupe(ttl=0.01)  # 10ms TTL
        d.mark_seen("key1")
        time.sleep(0.02)
        assert not d.is_duplicate("key1")

    def test_lru_eviction(self):
        d = TelegramUpdateDedupe(max_entries=2)
        d.mark_seen("a")
        d.mark_seen("b")
        d.mark_seen("c")  # evicts "a"
        assert not d.is_duplicate("a")
        assert d.is_duplicate("b")
        assert d.is_duplicate("c")

    def test_key_builders(self):
        assert update_key(123) == "update:123"
        assert message_key(456, 789) == "msg:456:789"
        assert callback_key("abc") == "cb:abc"


# ---- OffsetStore ----

class TestOffsetStore:
    def test_read_nonexistent(self, tmp_path):
        with patch(
            "genesis.channels.telegram.transport.offset_store._BASE_DIR",
            tmp_path / "telegram",
        ):
            assert read_offset("bot123") is None

    def test_write_then_read(self, tmp_path):
        with patch(
            "genesis.channels.telegram.transport.offset_store._BASE_DIR",
            tmp_path / "telegram",
        ):
            write_offset("bot123", 42)
            assert read_offset("bot123") == 42

    def test_delete(self, tmp_path):
        with patch(
            "genesis.channels.telegram.transport.offset_store._BASE_DIR",
            tmp_path / "telegram",
        ):
            write_offset("bot123", 99)
            delete_offset("bot123")
            assert read_offset("bot123") is None


# ---- SendSafety ----

class TestSendSafety:
    def test_connect_error_is_safe(self):
        exc = Exception()
        exc.__cause__ = httpx.ConnectError("connection refused")
        assert is_safe_to_retry_send(exc)

    def test_connect_timeout_is_safe(self):
        exc = Exception()
        exc.__cause__ = httpx.ConnectTimeout("timed out during connect")
        assert is_safe_to_retry_send(exc)

    def test_read_timeout_is_not_safe(self):
        exc = Exception()
        exc.__cause__ = httpx.ReadTimeout("timed out reading response")
        assert not is_safe_to_retry_send(exc)

    def test_write_timeout_is_not_safe(self):
        exc = Exception()
        exc.__cause__ = httpx.WriteTimeout("timed out writing")
        assert not is_safe_to_retry_send(exc)

    def test_unknown_error_is_not_safe(self):
        assert not is_safe_to_retry_send(ValueError("unknown"))

    def test_ptb_network_error_wrapping_connect(self):
        inner = httpx.ConnectError("refused")
        ptb_err = NetworkError("network error")
        ptb_err.__cause__ = inner
        outer = Exception()
        outer.__cause__ = ptb_err
        assert is_safe_to_retry_send(outer)

    def test_classify_connect_error(self):
        exc = Exception()
        exc.__cause__ = httpx.ConnectError("refused")
        assert classify_send_error(exc) == "pre_connect"

    def test_classify_read_timeout(self):
        exc = Exception()
        exc.__cause__ = httpx.ReadTimeout("timeout")
        assert classify_send_error(exc) == "read_timeout"


# ---- TypingCircuitBreaker ----

class TestTypingBreaker:
    def test_initially_should_send(self):
        cb = TypingCircuitBreaker()
        assert cb.should_send(123)

    def test_failures_below_threshold_allow_send(self):
        cb = TypingCircuitBreaker(max_failures=3)
        cb.record_failure(123)
        cb.record_failure(123)
        assert cb.should_send(123)

    def test_failures_at_threshold_suspends(self):
        cb = TypingCircuitBreaker(max_failures=2, min_backoff_s=100.0)
        cb.record_failure(123)
        cb.record_failure(123)
        assert not cb.should_send(123)

    def test_success_resets(self):
        cb = TypingCircuitBreaker(max_failures=2, min_backoff_s=100.0)
        cb.record_failure(123)
        cb.record_failure(123)
        cb.record_success(123)
        assert cb.should_send(123)

    def test_per_chat_isolation(self):
        cb = TypingCircuitBreaker(max_failures=1, min_backoff_s=100.0)
        cb.record_failure(111)
        assert not cb.should_send(111)
        assert cb.should_send(222)


# ---- PollingWatchdog ----

class TestPollingWatchdog:
    @pytest.mark.asyncio
    async def test_stall_triggers_callback(self):
        callback = AsyncMock()
        wd = PollingWatchdog(
            on_stall=callback,
            stall_threshold_s=0.05,
            check_interval_s=0.02,
        )
        wd.start()
        await asyncio.sleep(0.15)
        await wd.stop()
        assert callback.call_count >= 1

    @pytest.mark.asyncio
    async def test_activity_prevents_stall(self):
        callback = AsyncMock()
        wd = PollingWatchdog(
            on_stall=callback,
            stall_threshold_s=0.1,
            check_interval_s=0.03,
        )
        wd.start()
        for _ in range(5):
            await asyncio.sleep(0.03)
            wd.record_activity()
        await wd.stop()
        assert callback.call_count == 0


# ---- DraftStreamer ----

class TestDraftStreamer:
    @pytest.mark.asyncio
    async def test_accumulates_text(self):
        bot = AsyncMock()
        streamer = DraftStreamer(bot, chat_id=123, draft_id=1, throttle_s=0)
        await streamer.on_text("Hello ")
        await streamer.on_text("world")
        assert streamer.accumulated_text == "Hello world"

    @pytest.mark.asyncio
    async def test_sends_draft_on_text(self):
        bot = AsyncMock()
        streamer = DraftStreamer(bot, chat_id=123, draft_id=42, throttle_s=0)
        await streamer.on_text("test")
        bot.send_message_draft.assert_called()
        call_kwargs = bot.send_message_draft.call_args[1]
        assert call_kwargs["chat_id"] == 123
        assert call_kwargs["draft_id"] == 42

    @pytest.mark.asyncio
    async def test_tool_lines_in_draft(self):
        bot = AsyncMock()
        streamer = DraftStreamer(bot, chat_id=123, draft_id=1, throttle_s=0)
        await streamer.on_tool_use("Read", "src/main.py")
        draft_text = bot.send_message_draft.call_args[1]["text"]
        assert "Read" in draft_text

    @pytest.mark.asyncio
    async def test_self_disables_after_consecutive_errors(self):
        """Streamer tolerates transient errors but disables after 3 consecutive."""
        bot = AsyncMock()
        bot.send_message_draft.side_effect = Exception("API error")
        streamer = DraftStreamer(bot, chat_id=123, draft_id=1, throttle_s=0)
        # First two failures: still enabled (transient tolerance)
        await streamer.on_text("a")
        assert streamer.enabled
        await streamer.on_text("b")
        assert streamer.enabled
        # Third failure: disabled
        await streamer.on_text("c")
        assert not streamer.enabled

    @pytest.mark.asyncio
    async def test_thinking_indicator(self):
        bot = AsyncMock()
        streamer = DraftStreamer(bot, chat_id=123, draft_id=1, throttle_s=0)
        await streamer.on_thinking()
        draft_text = bot.send_message_draft.call_args[1]["text"]
        assert "Thinking" in draft_text

    def test_generate_draft_id_positive(self):
        for _ in range(100):
            did = generate_draft_id()
            assert did > 0


# ---- FTS5 Escape Fix ----

class TestFTS5Escape:
    def test_commas_escaped(self):
        from genesis.db.crud.memory import _escape_fts5
        result = _escape_fts5("hello, world, test")
        assert "," not in result
        assert result is not None

    def test_natural_language(self):
        from genesis.db.crud.memory import _escape_fts5
        result = _escape_fts5("What's the weather like today?")
        assert result is not None
        # No special chars should remain
        assert "?" not in result
        assert "'" not in result

    def test_parentheses_escaped(self):
        from genesis.db.crud.knowledge import _escape_fts5
        result = _escape_fts5("function(arg1, arg2)")
        assert "(" not in result
        assert ")" not in result
        assert "," not in result

    def test_empty_after_escape_returns_none(self):
        from genesis.db.crud.memory import _escape_fts5
        result = _escape_fts5("!!!")
        assert result is None

    def test_unicode_preserved(self):
        from genesis.db.crud.memory import _escape_fts5
        result = _escape_fts5("café résumé naïve")
        assert result is not None
        assert "café" in result


# ---- Adapter offset persistence debounce ----


class TestAdapterOffsetDebounce:
    """Regression coverage for the None-sentinel fix on _last_offset_write.

    Previously `_last_offset_write: float = 0.0` paired with a 30s debounce
    meant the first offset persist was suppressed on fresh processes where
    system uptime (time.monotonic) was under 30 seconds — same bug class as
    _should_log_failure in litellm_delegate. The fix uses None as the
    "never persisted" sentinel.
    """

    def _make_adapter(self):
        from genesis.channels.telegram.adapter_v2 import TelegramAdapterV2
        return TelegramAdapterV2(token="t", conversation_loop=AsyncMock())

    def test_sentinel_initialized_to_none(self):
        adapter = self._make_adapter()
        assert adapter._last_offset_write is None

    def test_first_persist_not_suppressed_on_fresh_process(self):
        """With system uptime < _OFFSET_PERSIST_INTERVAL_S (30s), first
        _persist_offset call must still write — not be debounced away."""
        adapter = self._make_adapter()
        # Simulate an _app/updater with a non-zero offset
        adapter._app = AsyncMock()
        adapter._app.updater._last_update_id = 42
        adapter._app.bot.id = 1234

        with (
            patch("genesis.channels.telegram.adapter_v2.time.monotonic", return_value=5.0),
            patch("genesis.channels.telegram.adapter_v2.write_offset") as mock_write,
        ):
            adapter._persist_offset()
            # First call must persist even though monotonic=5.0 < interval=30s
            mock_write.assert_called_once_with("1234", 42)
            assert adapter._last_offset_write == 5.0

    def test_second_persist_within_interval_is_debounced(self):
        adapter = self._make_adapter()
        adapter._app = AsyncMock()
        adapter._app.updater._last_update_id = 42
        adapter._app.bot.id = 1234

        with (
            patch("genesis.channels.telegram.adapter_v2.time.monotonic", return_value=5.0),
            patch("genesis.channels.telegram.adapter_v2.write_offset") as mock_write,
        ):
            adapter._persist_offset()
            assert mock_write.call_count == 1
            # Second call at same monotonic time → within debounce window
            adapter._persist_offset()
            assert mock_write.call_count == 1  # still 1, suppressed

    def test_persist_after_interval_writes_again(self):
        adapter = self._make_adapter()
        adapter._app = AsyncMock()
        adapter._app.updater._last_update_id = 42
        adapter._app.bot.id = 1234

        with (
            patch("genesis.channels.telegram.adapter_v2.time.monotonic", return_value=5.0),
            patch("genesis.channels.telegram.adapter_v2.write_offset") as mock_write,
        ):
            adapter._persist_offset()
            assert mock_write.call_count == 1

        interval = adapter._OFFSET_PERSIST_INTERVAL_S
        with (
            patch(
                "genesis.channels.telegram.adapter_v2.time.monotonic",
                return_value=5.0 + interval + 1.0,
            ),
            patch("genesis.channels.telegram.adapter_v2.write_offset") as mock_write,
        ):
            adapter._persist_offset()
            assert mock_write.call_count == 1  # second persist after interval

    def test_force_always_persists(self):
        adapter = self._make_adapter()
        adapter._app = AsyncMock()
        adapter._app.updater._last_update_id = 42
        adapter._app.bot.id = 1234

        with (
            patch("genesis.channels.telegram.adapter_v2.time.monotonic", return_value=5.0),
            patch("genesis.channels.telegram.adapter_v2.write_offset") as mock_write,
        ):
            adapter._persist_offset(force=True)
            assert mock_write.call_count == 1
            # force=True bypasses the debounce check
            adapter._persist_offset(force=True)
            assert mock_write.call_count == 2
