"""Tests for the Discord webhook channel adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from genesis.channels.discord_adapter import DiscordWebhookAdapter


@pytest.fixture
def adapter() -> DiscordWebhookAdapter:
    return DiscordWebhookAdapter(
        webhooks={
            "dev-discussion": "https://discord.com/api/webhooks/111/token-aaa",
            "showcase": "https://discord.com/api/webhooks/222/token-bbb",
        },
        default_webhook="https://discord.com/api/webhooks/000/token-default",
    )


class TestCapabilities:
    def test_capabilities_values(self, adapter: DiscordWebhookAdapter) -> None:
        caps = adapter.get_capabilities()
        assert caps["markdown"] is True
        assert caps["buttons"] is False
        assert caps["reactions"] is False
        assert caps["voice"] is False
        assert caps["max_length"] == 2000

    def test_capabilities_documents(self, adapter: DiscordWebhookAdapter) -> None:
        caps = adapter.get_capabilities()
        assert caps.get("documents") is False


class TestSendMessage:
    @pytest.mark.anyio
    async def test_sends_to_named_webhook(self, adapter: DiscordWebhookAdapter) -> None:
        """channel_id maps to a named webhook URL."""
        with patch("genesis.channels.discord_adapter.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = AsyncMock(
                status_code=200,
                json=lambda: {"id": "msg-123"},
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            delivery_id = await adapter.send_message("dev-discussion", "Hello Discord!")

            mock_client.post.assert_called_once()
            call_url = mock_client.post.call_args[0][0]
            assert "111/token-aaa" in call_url
            assert delivery_id == "msg-123"

    @pytest.mark.anyio
    async def test_falls_back_to_default_webhook(self, adapter: DiscordWebhookAdapter) -> None:
        """Unknown channel_id uses the default webhook."""
        with patch("genesis.channels.discord_adapter.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = AsyncMock(
                status_code=200,
                json=lambda: {"id": "msg-456"},
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await adapter.send_message("unknown-channel", "Hello!")

            call_url = mock_client.post.call_args[0][0]
            assert "000/token-default" in call_url

    @pytest.mark.anyio
    async def test_sends_json_payload(self, adapter: DiscordWebhookAdapter) -> None:
        """Message content is sent as JSON with 'content' key."""
        with patch("genesis.channels.discord_adapter.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = AsyncMock(
                status_code=200,
                json=lambda: {"id": "msg-789"},
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await adapter.send_message("dev-discussion", "Test content")

            call_kwargs = mock_client.post.call_args
            payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            assert payload["content"] == "Test content"

    @pytest.mark.anyio
    async def test_chunks_long_messages(self, adapter: DiscordWebhookAdapter) -> None:
        """Messages over 2000 chars are split at newline boundaries."""
        long_text = ("A" * 1000 + "\n") * 3  # 3003 chars, 3 lines
        sent_payloads = []

        with patch("genesis.channels.discord_adapter.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            call_count = 0

            async def mock_post(url, **kwargs):
                nonlocal call_count
                call_count += 1
                sent_payloads.append(kwargs.get("json", {}).get("content", ""))
                resp = AsyncMock(status_code=200)
                resp.json = lambda: {"id": f"msg-{call_count}"}
                return resp

            mock_client.post = mock_post
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await adapter.send_message("dev-discussion", long_text)

            assert len(sent_payloads) >= 2
            for payload in sent_payloads:
                assert len(payload) <= 2000

    @pytest.mark.anyio
    async def test_wait_param_for_message_id(self, adapter: DiscordWebhookAdapter) -> None:
        """Webhook URL should include ?wait=true to get message ID back."""
        with patch("genesis.channels.discord_adapter.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = AsyncMock(
                status_code=200,
                json=lambda: {"id": "msg-abc"},
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await adapter.send_message("dev-discussion", "Hello")

            call_url = mock_client.post.call_args[0][0]
            assert "wait=true" in call_url


class TestLifecycle:
    @pytest.mark.anyio
    async def test_start_stop_noop(self, adapter: DiscordWebhookAdapter) -> None:
        await adapter.start()
        await adapter.stop()

    @pytest.mark.anyio
    async def test_send_typing_noop(self, adapter: DiscordWebhookAdapter) -> None:
        await adapter.send_typing("any-channel")


class TestEngagement:
    @pytest.mark.anyio
    async def test_engagement_signals_neutral(self, adapter: DiscordWebhookAdapter) -> None:
        result = await adapter.get_engagement_signals("any-id")
        assert result["signal"] == "neutral"
