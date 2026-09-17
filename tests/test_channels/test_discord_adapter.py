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
    async def test_unconfigured_named_channel_is_REFUSED(
        self,
        adapter: DiscordWebhookAdapter,
    ) -> None:
        """A named channel with no webhook must RAISE, not post somewhere else.

        REPLACES test_falls_back_to_default_webhook, which asserted the opposite
        (owner ruling 2026-09-10). That fallback is the defect PR #1854 exists to
        remove: it posts to the default channel and reports success, and the log
        line records the REQUESTED name, so the redirect is undetectable. It
        applied to a third of the names this PR lets a caller request —
        bug-reports / feature-requests / troubleshooting had no webhook on the
        install where it was measured.

        No HTTP mock here on purpose: the refusal must happen BEFORE any POST, so
        a real httpx call would be the failure, not the setup.
        """
        with pytest.raises(ValueError, match="No Discord webhook configured"):
            await adapter.send_message("announcements", "Release notes!")

    @pytest.mark.anyio
    async def test_refusal_names_the_setting_to_configure(self) -> None:
        """The error must be actionable — it names the exact setting.

        The adapter holds no env-var literal (see its docstring: that would make
        it an egress door under scripts/check_external_io.py), so the naming rule
        arrives as an injected callable. This asserts the adapter USES that hint;
        the rule's own correctness is pinned separately against the real shipped
        function, in test_discord_webhook_env_matches_the_discovery_rule.
        """
        adapter = DiscordWebhookAdapter(
            webhooks={},
            default_webhook="https://discord.com/api/webhooks/000/token-default",
            default_channel="dev-discussion",
            config_hint=lambda name: f"SETTING_FOR_{name}",
        )
        with pytest.raises(ValueError) as exc:
            await adapter.send_message("bug-reports", "x")
        assert "SETTING_FOR_bug-reports" in str(exc.value)

    @pytest.mark.anyio
    async def test_refusal_without_a_hint_lists_what_is_configured(
        self,
        adapter: DiscordWebhookAdapter,
    ) -> None:
        """No hint injected → still actionable, by naming the channels that work.

        The fixture passes no config_hint, so this is the degraded path; it must
        not produce a bare 'not configured' with nothing to act on.
        """
        with pytest.raises(ValueError) as exc:
            await adapter.send_message("bug-reports", "x")
        msg = str(exc.value)
        assert "dev-discussion" in msg and "showcase" in msg

    @pytest.mark.anyio
    async def test_the_configured_default_channel_still_falls_back(self) -> None:
        """The DEFAULT recipient keeps using default_webhook — not a regression.

        'The default channel' IS whatever DISCORD_WEBHOOK_URL points at, so it
        need not also appear in the per-channel map. This is the case the refusal
        must NOT break, and it is the one a too-broad refusal would break first:
        every bare channel="discord" send resolves to this name.
        """
        adapter = DiscordWebhookAdapter(
            webhooks={"showcase": "https://discord.com/api/webhooks/222/token-bbb"},
            default_webhook="https://discord.com/api/webhooks/000/token-default",
            default_channel="dev-discussion",
        )
        with patch("genesis.channels.discord_adapter.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = AsyncMock(
                status_code=200,
                json=lambda: {"id": "msg-456"},
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await adapter.send_message("dev-discussion", "Hello!")

            assert "000/token-default" in mock_client.post.call_args[0][0]

    @pytest.mark.anyio
    async def test_empty_channel_id_still_falls_back(
        self,
        adapter: DiscordWebhookAdapter,
    ) -> None:
        """No name at all is the default case, not an unconfigured name."""
        with patch("genesis.channels.discord_adapter.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = AsyncMock(
                status_code=200,
                json=lambda: {"id": "m"},
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await adapter.send_message("", "Hello!")

            assert "000/token-default" in mock_client.post.call_args[0][0]

    @pytest.mark.anyio
    async def test_poll_refuses_an_unconfigured_channel(
        self,
        adapter: DiscordWebhookAdapter,
    ) -> None:
        """send_poll shares the resolver — a poll in the wrong channel collects
        the wrong votes, and is as undetectable as a misdirected message."""
        with pytest.raises(ValueError, match="No Discord webhook configured"):
            await adapter.send_poll("feature-requests", "Ship it?", ["Yes", "No"])

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


class TestSendPoll:
    @pytest.mark.anyio
    async def test_sends_poll_payload(self, adapter: DiscordWebhookAdapter) -> None:
        """send_poll posts correct poll structure to webhook."""
        with patch("genesis.channels.discord_adapter.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = AsyncMock(
                status_code=200,
                json=lambda: {"id": "poll-msg-1"},
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            msg_id = await adapter.send_poll(
                "dev-discussion",
                "What's your favorite?",
                ["Option A", "Option B", "Option C"],
                duration_hours=24,
            )

            assert msg_id == "poll-msg-1"
            call_kwargs = mock_client.post.call_args
            payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            assert "poll" in payload
            assert payload["poll"]["question"]["text"] == "What's your favorite?"
            assert len(payload["poll"]["answers"]) == 3
            assert payload["poll"]["duration"] == 24
            assert payload["poll"]["allow_multiselect"] is False

    @pytest.mark.anyio
    async def test_poll_truncates_long_values(self, adapter: DiscordWebhookAdapter) -> None:
        """Poll question and answers are truncated to Discord limits."""
        with patch("genesis.channels.discord_adapter.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = AsyncMock(
                status_code=200,
                json=lambda: {"id": "poll-msg-2"},
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            long_question = "Q" * 500
            long_answer = "A" * 100

            await adapter.send_poll("dev-discussion", long_question, [long_answer])

            call_kwargs = mock_client.post.call_args
            payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            assert len(payload["poll"]["question"]["text"]) == 300
            assert len(payload["poll"]["answers"][0]["poll_media"]["text"]) == 55

    @pytest.mark.anyio
    async def test_poll_uses_named_webhook(self, adapter: DiscordWebhookAdapter) -> None:
        """send_poll resolves webhook by channel name."""
        with patch("genesis.channels.discord_adapter.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = AsyncMock(
                status_code=200,
                json=lambda: {"id": "poll-msg-3"},
            )
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            await adapter.send_poll("showcase", "Test?", ["Yes", "No"])

            call_url = mock_client.post.call_args[0][0]
            assert "222/token-bbb" in call_url
            assert "wait=true" in call_url


class TestEngagement:
    @pytest.mark.anyio
    async def test_engagement_signals_neutral(self, adapter: DiscordWebhookAdapter) -> None:
        result = await adapter.get_engagement_signals("any-id")
        assert result["signal"] == "neutral"


class TestWebhookEnvNamingRule:
    """The naming rule lives in runtime/init/outreach.py, so test it THERE.

    The adapter receives it as an injected callable and cannot know whether the
    real one is correct — so an adapter-level test with a stub hint proves only
    that the hint is used. These drive the REAL shipped function.
    """

    def test_discord_webhook_env_matches_the_discovery_rule(self) -> None:
        """Round-trip: env name -> channel name -> env name must be identity.

        The discovery loop in init() strips the prefix, lowercases and maps
        ``_`` -> ``-``; _discord_webhook_env must invert exactly that. If the two
        ever disagree, the refusal message names a variable that would not in fact
        configure the channel -- worse than no hint, because it is confidently
        wrong.
        """
        from genesis.runtime.init.outreach import (
            _discord_channel_from_env,
            _discord_webhook_env,
        )

        for env_name in (
            "DISCORD_WEBHOOK_BUG_REPORTS",
            "DISCORD_WEBHOOK_FEATURE_REQUESTS",
            "DISCORD_WEBHOOK_TROUBLESHOOTING",
            "DISCORD_WEBHOOK_ANNOUNCEMENTS",
            "DISCORD_WEBHOOK_GENERAL",
        ):
            # Forward: the SHIPPED function init()'s discovery loop calls.
            # Transcribing the transform here instead would read as verification
            # without being one — a change to the discovery rule would leave this
            # green while the refusal message named the wrong variable.
            channel = _discord_channel_from_env(env_name)
            # Inverse: what the refusal message tells the operator to set.
            assert _discord_webhook_env(channel) == env_name, (
                f"{channel!r} -> {_discord_webhook_env(channel)!r}, expected {env_name!r}"
            )

    def test_every_known_subchannel_maps_to_a_plausible_env_name(self) -> None:
        """Whole-set, not just the names that bit us: every DISCORD_CHANNELS entry
        must produce a valid shell identifier, or the hint is unusable for it."""
        import re

        from genesis.outreach.types import DISCORD_CHANNELS
        from genesis.runtime.init.outreach import _discord_webhook_env

        for name in sorted(DISCORD_CHANNELS):
            env = _discord_webhook_env(name)
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*", env), f"{name!r} -> {env!r}"
