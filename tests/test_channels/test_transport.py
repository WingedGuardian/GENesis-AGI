"""Tests for Telegram V2 transport layer."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from telegram.error import NetworkError, TimedOut

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


class TestFTS5Prepare:
    """Tests for _prepare_fts5 — the FTS5 query sanitizer."""

    def test_commas_escaped(self):
        from genesis.db.crud.memory import _prepare_fts5

        result = _prepare_fts5("hello, world, test")
        assert "," not in result
        assert result is not None

    def test_natural_language(self):
        from genesis.db.crud.memory import _prepare_fts5

        result = _prepare_fts5("What's the weather like today?")
        assert result is not None
        assert "?" not in result
        assert "'" not in result

    def test_parentheses_escaped(self):
        from genesis.db.crud.knowledge import _prepare_fts5

        result = _prepare_fts5("function(arg1, arg2)")
        assert "(" not in result
        assert ")" not in result
        assert "," not in result

    def test_empty_after_escape_returns_none(self):
        from genesis.db.crud.memory import _prepare_fts5

        result = _prepare_fts5("!!!")
        assert result is None

    def test_unicode_preserved(self):
        from genesis.db.crud.memory import _prepare_fts5

        result = _prepare_fts5("café résumé naïve")
        assert result is not None
        assert "café" in result

    def test_uppercase_or_and_neutralized(self):
        """Uppercase OR/AND in user queries must not become FTS5 operators."""
        from genesis.db.crud.memory import _prepare_fts5

        result = _prepare_fts5("FTS5 boolean OR AND query")
        assert result is not None
        # Lowercased — or/and are plain search terms, not FTS5 operators
        assert "OR" not in result
        assert "AND" not in result
        assert "or" in result
        assert "and" in result

    def test_boolean_mode_preserves_operators(self):
        """Boolean mode preserves OR/AND/parens for expand_query output."""
        from genesis.db.crud.memory import _prepare_fts5

        result = _prepare_fts5(
            "(configure AND routing) OR setup OR deploy",
            boolean=True,
        )
        assert result is not None
        assert "OR" in result
        assert "AND" in result
        assert "(" in result
        assert ")" in result

    def test_boolean_mode_unbalanced_parens_stripped(self):
        """Unbalanced parentheses in boolean mode are safely stripped."""
        from genesis.db.crud.memory import _prepare_fts5

        result = _prepare_fts5("(test AND query", boolean=True)
        assert result is not None
        assert "(" not in result  # parens stripped due to imbalance
        assert ")" not in result

    def test_boolean_mode_strips_special_chars(self):
        """Boolean mode still strips non-operator special chars."""
        from genesis.db.crud.memory import _prepare_fts5

        result = _prepare_fts5(
            '(test AND "query") OR setup*',
            boolean=True,
        )
        assert '"' not in result
        assert "*" not in result


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


class TestLivenessHTTPXRequest:
    """The getUpdates request pool must report every successful poll
    round-trip — including EMPTY ones — so a quiet chat doesn't read as a
    stalled poller (false stalls restarted the updater every threshold
    window all day on 2026-07-08)."""

    def _make(self, do_request_result=None, do_request_exc=None):
        from unittest.mock import AsyncMock, patch

        from genesis.channels.telegram.transport.polling import (
            LivenessHTTPXRequest,
        )

        calls = []
        req = LivenessHTTPXRequest(
            connection_pool_size=1,
            on_success=lambda: calls.append(1),
        )
        parent = AsyncMock()
        if do_request_exc is not None:
            parent.side_effect = do_request_exc
        else:
            parent.return_value = do_request_result
        patcher = patch(
            "telegram.request.HTTPXRequest.do_request",
            parent,
        )
        return req, calls, patcher

    @pytest.mark.asyncio
    async def test_successful_empty_poll_records(self):
        req, calls, patcher = self._make(do_request_result=(200, b'{"ok":true,"result":[]}'))
        with patcher:
            await req.do_request(url="u", method="post")
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_http_error_does_not_record(self):
        req, calls, patcher = self._make(do_request_result=(409, b"conflict"))
        with patcher:
            await req.do_request(url="u", method="post")
        assert calls == []

    @pytest.mark.asyncio
    async def test_exception_does_not_record(self):
        req, calls, patcher = self._make(do_request_exc=RuntimeError("net down"))
        with patcher, pytest.raises(RuntimeError):
            await req.do_request(url="u", method="post")
        assert calls == []

    @pytest.mark.asyncio
    async def test_no_callback_is_safe(self):
        from unittest.mock import AsyncMock, patch

        from genesis.channels.telegram.transport.polling import (
            LivenessHTTPXRequest,
        )

        req = LivenessHTTPXRequest(connection_pool_size=1)
        with patch("telegram.request.HTTPXRequest.do_request", AsyncMock(return_value=(200, b""))):
            await req.do_request(url="u", method="post")  # must not raise


# ---- send_with_client_heal / _is_closed_client_error ----


def _closed_client_error() -> NetworkError:
    """Build the exact exception PTB raises when the send client is closed.

    ``HTTPXRequest.do_request`` raises RuntimeError("This HTTPXRequest is not
    initialized!"); ``_request_wrapper`` rewraps it as NetworkError(...) from exc.
    """
    inner = RuntimeError("This HTTPXRequest is not initialized!")
    wrapped = NetworkError(f"Unknown error in HTTP implementation: {inner!r}")
    wrapped.__cause__ = inner
    return wrapped


class TestClosedClientMatcher:
    def test_matches_wrapped_error_on_message(self):
        from genesis.channels.telegram.transport.send import _is_closed_client_error

        assert _is_closed_client_error(_closed_client_error())

    def test_matches_cause_chain_only(self):
        # Outer message clean, signature only on the __cause__.
        from genesis.channels.telegram.transport.send import _is_closed_client_error

        e = NetworkError("Unknown error in HTTP implementation")
        e.__cause__ = RuntimeError("This HTTPXRequest is not initialized!")
        assert _is_closed_client_error(e)

    def test_rejects_ordinary_network_errors(self):
        from genesis.channels.telegram.transport.send import _is_closed_client_error

        assert not _is_closed_client_error(TimedOut("Timed out"))
        assert not _is_closed_client_error(NetworkError("Connection reset by peer"))

    def test_no_infinite_loop_on_cyclic_cause(self):
        from genesis.channels.telegram.transport.send import _is_closed_client_error

        a = NetworkError("a")
        b = NetworkError("b")
        a.__cause__ = b
        b.__cause__ = a  # cycle
        assert _is_closed_client_error(a) is False  # terminates, no hang


class TestSendWithClientHeal:
    @pytest.mark.asyncio
    async def test_heals_and_retries_once_then_succeeds(self):
        from genesis.channels.telegram.transport.send import send_with_client_heal

        bot = MagicMock()
        bot.request.initialize = AsyncMock()
        calls = {"n": 0}

        async def send():
            calls["n"] += 1
            if calls["n"] == 1:
                raise _closed_client_error()
            return "delivered"

        result = await send_with_client_heal(bot, send, stopping=lambda: False)
        assert result == "delivered"
        assert calls["n"] == 2  # first fails, heal, second succeeds
        bot.request.initialize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stopping_reraises_without_heal(self):
        from genesis.channels.telegram.transport.send import send_with_client_heal

        bot = MagicMock()
        bot.request.initialize = AsyncMock()

        async def send():
            raise _closed_client_error()

        with pytest.raises(NetworkError):
            await send_with_client_heal(bot, send, stopping=lambda: True)
        bot.request.initialize.assert_not_awaited()  # never resurrect on shutdown

    @pytest.mark.asyncio
    async def test_non_closed_error_reraises_without_heal(self):
        from genesis.channels.telegram.transport.send import send_with_client_heal

        bot = MagicMock()
        bot.request.initialize = AsyncMock()

        async def send():
            raise TimedOut("Timed out")

        with pytest.raises(TimedOut):
            await send_with_client_heal(bot, send, stopping=lambda: False)
        bot.request.initialize.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_heal_retry_still_failing_reraises_bounded(self):
        # Client stays dead: heal once, retry, still fails -> raise (no loop).
        from genesis.channels.telegram.transport.send import send_with_client_heal

        bot = MagicMock()
        bot.request.initialize = AsyncMock()

        async def send():
            raise _closed_client_error()

        with pytest.raises(NetworkError):
            await send_with_client_heal(bot, send, stopping=lambda: False)
        bot.request.initialize.assert_awaited_once()  # exactly one heal attempt


class TestAdapterDocumentHeal:
    @pytest.mark.asyncio
    async def test_document_heal_retry_uses_fresh_stream(self):
        """Regression: on a heal-retry the document must re-stream from the
        start. PTB's InputFile consumes the BytesIO to EOF on the first
        (failing) attempt; a reused stream would upload zero bytes silently."""
        from genesis.channels.telegram.adapter_v2 import TelegramAdapterV2

        adapter = TelegramAdapterV2(token="123:ABC", conversation_loop=MagicMock())
        adapter._app = MagicMock()
        adapter._app.bot.request.initialize = AsyncMock()

        seen: list[bytes] = []
        calls = {"n": 0}

        async def fake_send_document(bot, chat_id, document, **kwargs):
            calls["n"] += 1
            data = document.read()  # PTB consumes the stream at InputFile build
            if calls["n"] == 1:
                raise _closed_client_error()
            seen.append(data)
            return MagicMock(message_id=99)

        with patch(
            "genesis.channels.telegram.adapter_v2.safe_send_document",
            side_effect=fake_send_document,
        ):
            msg_id = await adapter.send_document("123", b"PAYLOAD-BYTES", filename="f.txt")

        assert msg_id == "99"
        assert seen == [b"PAYLOAD-BYTES"]  # retry got the FULL payload, not b""
        adapter._app.bot.request.initialize.assert_awaited_once()


class TestClientHealE2E:
    @pytest.mark.asyncio
    async def test_real_ptb_closed_client_heals_and_delivers(self, caplog):
        """End-to-end through REAL PTB: a shutdown-closed httpx send-client is
        rebuilt by the heal and the message is delivered. Network is mocked via
        httpx.MockTransport, but do_request, initialize(), and the NetworkError
        wrap are all real PTB — this guards against a PTB upgrade changing
        HTTPXRequest.initialize() rebuild semantics (the load-bearing mechanism).
        """
        import logging

        from telegram import Bot
        from telegram.request import HTTPXRequest

        from genesis.channels.telegram.adapter_v2 import TelegramAdapterV2

        def handler(request):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "message_id": 555,
                        "date": 0,
                        "chat": {"id": 123, "type": "private"},
                        "text": "x",
                    },
                },
            )

        req = HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(handler)})
        bot = Bot(token="123:ABC", request=req)
        conv = MagicMock()
        conv._db = None  # skip DB persistence path
        adapter = TelegramAdapterV2(token="123:ABC", conversation_loop=conv)
        app = MagicMock()
        app.bot = bot
        adapter._app = app

        try:
            # Reproduce the 2026-07-15 state: the real send client is closed.
            await bot.request.shutdown()
            assert bot.request._client.is_closed

            # Real adapter path: raises the real closed-client error, heals via
            # real bot.request.initialize(), retries, delivers.
            with caplog.at_level(logging.ERROR):
                mid = await adapter.send_message("123", "after client closed")
            assert mid == "555"
            assert not bot.request._client.is_closed  # rebuilt

            # During shutdown, the heal must NOT resurrect the client.
            adapter._stopping = True
            await bot.request.shutdown()
            with pytest.raises(NetworkError):
                await adapter.send_message("123", "during shutdown")
            assert bot.request._client.is_closed
        finally:
            await req.shutdown()
