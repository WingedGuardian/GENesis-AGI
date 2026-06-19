"""Tests for the email channel adapter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from genesis.channels.email_adapter import EmailAdapter


@pytest.fixture
def adapter() -> EmailAdapter:
    return EmailAdapter(
        smtp_host="smtp.gmail.com",
        smtp_port=465,
        username="test@gmail.com",
        password="app-password",
        from_address="test@gmail.com",
    )


class TestEmailAdapter:
    def test_capabilities(self, adapter: EmailAdapter) -> None:
        caps = adapter.get_capabilities()
        assert caps["markdown"] is False
        assert caps["buttons"] is False
        assert caps["voice"] is False
        assert caps["documents"] is False
        assert caps["max_length"] == 50_000

    @pytest.mark.anyio
    async def test_send_message(self, adapter: EmailAdapter) -> None:
        mock_smtp = MagicMock()
        with patch("genesis.channels.email_adapter.smtplib.SMTP_SSL") as smtp_cls:
            smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
            smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

            delivery_id = await adapter.send_message(
                "recipient@example.com",
                "Hello from Genesis",
                subject="Test Subject",
            )

            mock_smtp.login.assert_called_once_with("test@gmail.com", "app-password")
            mock_smtp.send_message.assert_called_once()

            sent_msg = mock_smtp.send_message.call_args[0][0]
            assert sent_msg["To"] == "recipient@example.com"
            assert sent_msg["Subject"] == "Test Subject"
            assert "Genesis" in sent_msg["From"]
            assert delivery_id  # non-empty string

    @pytest.mark.anyio
    async def test_send_message_default_subject(self, adapter: EmailAdapter) -> None:
        mock_smtp = MagicMock()
        with patch("genesis.channels.email_adapter.smtplib.SMTP_SSL") as smtp_cls:
            smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
            smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

            await adapter.send_message("recipient@example.com", "body")

            sent_msg = mock_smtp.send_message.call_args[0][0]
            assert sent_msg["Subject"] == "Message from Genesis"

    @pytest.mark.asyncio
    async def test_send_message_does_not_block_event_loop(self, adapter: EmailAdapter) -> None:
        """A slow SMTP round-trip must not freeze the asyncio event loop.

        Synchronous ``smtplib`` must run in a worker thread; otherwise a blocking
        send starves the loop — the 2026-06-14 incident where retrying ~200
        failed sends froze heartbeats, health checks, and the awareness tick.
        """
        import asyncio
        import time

        class _BlockingSMTP:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args) -> bool:
                return False

            def login(self, *args) -> None:
                pass

            def send_message(self, msg) -> None:
                time.sleep(0.3)  # simulate a slow SMTP round-trip

        progressed = 0

        async def _ticker() -> None:
            nonlocal progressed
            while True:
                await asyncio.sleep(0.01)
                progressed += 1

        with patch("genesis.channels.email_adapter.smtplib.SMTP_SSL", _BlockingSMTP):
            ticker = asyncio.create_task(_ticker())
            await adapter.send_message("recipient@example.com", "body")
            ticker.cancel()

        # Blocking send → loop frozen for 0.3s → ticker can't advance (~0).
        # Non-blocking (to_thread) → ticker runs concurrently (~25-30 times).
        assert progressed >= 5, f"event loop appears blocked during send (ticks={progressed})"

    @pytest.mark.anyio
    async def test_send_message_sets_real_message_id(self, adapter: EmailAdapter) -> None:
        """WS-9a: outbound mail must carry a real RFC Message-ID, not a fake email-<id>.

        Without it, recipients can't thread and our reply-poller can't match replies.
        """
        mock_smtp = MagicMock()
        with patch("genesis.channels.email_adapter.smtplib.SMTP_SSL") as smtp_cls:
            smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
            smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

            delivery_id = await adapter.send_message(
                "recipient@example.com", "body", subject="S",
            )

            sent_msg = mock_smtp.send_message.call_args[0][0]
            mid = sent_msg["Message-ID"]
            assert mid, "no Message-ID header on the sent email"
            assert mid.startswith("<") and mid.endswith(">") and "@" in mid, \
                f"not an RFC-shaped Message-ID: {mid}"
            assert not mid.startswith("<email-"), "fabricated id leaked into the header"
            assert delivery_id == mid, "returned delivery id must equal the real Message-ID"
            expected_domain = adapter._from_address.rsplit("@", 1)[-1]
            assert mid.rstrip(">").endswith("@" + expected_domain), \
                f"Message-ID domain not from from_address: {mid}"

    @pytest.mark.anyio
    async def test_engagement_signals_neutral(self, adapter: EmailAdapter) -> None:
        result = await adapter.get_engagement_signals("any-id")
        assert result["signal"] == "neutral"

    @pytest.mark.anyio
    async def test_start_stop_noop(self, adapter: EmailAdapter) -> None:
        await adapter.start()
        await adapter.stop()

    @pytest.mark.anyio
    async def test_send_typing_noop(self, adapter: EmailAdapter) -> None:
        await adapter.send_typing("any-channel")
