"""The Telegram header names the model that actually ran (#4850)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.cc.types import StreamEvent
from genesis.channels.telegram._handler_context import HandlerContext
from genesis.channels.telegram.transport.streaming import DraftStreamer


def _streamer(prefix="[Opus / High]", effort="High"):
    bot = MagicMock()
    bot.send_message_draft = AsyncMock()
    return DraftStreamer(bot=bot, chat_id=1, draft_id=3, prefix=prefix, prefix_effort=effort)


def test_the_prefix_takes_the_running_model():
    s = _streamer()
    s.set_running_model("Sonnet 5.5")
    assert s._prefix == "[Sonnet 5.5 / High]"


def test_no_prefix_stays_no_prefix():
    s = _streamer(prefix="", effort="")
    s.set_running_model("Sonnet 5.5")
    assert s._prefix == ""


@pytest.mark.asyncio
async def test_the_init_event_retitles_the_header():
    ctx = HandlerContext.__new__(HandlerContext)
    s = _streamer()
    on_event = HandlerContext.make_on_event(ctx, asyncio.Event(), s)
    await on_event(StreamEvent(event_type="init", raw={"model": "claude-sonnet-5-5"}))
    assert s._prefix == "[Sonnet 5.5 / High]"
    await on_event(StreamEvent(event_type="init", raw={}))  # no model: unchanged
    assert s._prefix == "[Sonnet 5.5 / High]"


@pytest.mark.asyncio
async def test_a_failover_peers_init_retitles_a_disabled_streamer():
    """The review's repro: home init, a rate limit (which disables the draft), then
    the peer's init. The final header names the peer."""
    ctx = HandlerContext.__new__(HandlerContext)
    s = _streamer()
    on_event = HandlerContext.make_on_event(ctx, asyncio.Event(), s)
    await on_event(StreamEvent(event_type="init", raw={"model": "claude-opus-5-5"}))
    await on_event(StreamEvent(event_type="rate_limit_event", raw={}))
    assert not s.enabled
    await on_event(StreamEvent(event_type="init", raw={"model": "glm-5.3[1m]"}))
    assert s._prefix == "[glm-5.3 / High]"


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("[Sonnet 5.5 / high]\nThe answer.", "[Sonnet 5.5 / High]\nThe answer."),
        ("[Opus / High]\n\nThe answer.", "[Sonnet 5.5 / High]\nThe answer."),
        ("The answer.", "[Sonnet 5.5 / High]\nThe answer."),
        ("[see notes] and more\nx", "[Sonnet 5.5 / High]\n[see notes] and more\nx"),
        ("[glm-5.3 / max]\nok", "[Sonnet 5.5 / High]\nok"),
        # Ordinary bracketed content is not a header (review finding).
        ("[Input / Output]\nx", "[Sonnet 5.5 / High]\n[Input / Output]\nx"),
        ("[Before / After]\nx", "[Sonnet 5.5 / High]\n[Before / After]\nx"),
        ("[Plan A / high]\nx", "[Sonnet 5.5 / High]\n[Plan A / high]\nx"),
        ("[Step 2 / max]\nx", "[Sonnet 5.5 / High]\n[Step 2 / max]\nx"),
        ("[Priority 1 / High]\nx", "[Sonnet 5.5 / High]\n[Priority 1 / High]\nx"),
        ("[Opus 5.5 / xHigh]\nok", "[Sonnet 5.5 / High]\nok"),
        ("[haiku 4.5 / low]\nok", "[Sonnet 5.5 / High]\nok"),
        ("[Fable 5.1 / max]\nok", "[Sonnet 5.5 / High]\nok"),
    ],
)
def test_the_relay_header_replaces_a_self_written_one(reply, expected):
    from genesis.channels.telegram._handler_messages import _with_model_prefix

    s = _streamer(prefix="[Sonnet 5.5 / High]")
    assert _with_model_prefix(s, reply) == expected


def test_without_a_relay_prefix_the_models_header_stays():
    from genesis.channels.telegram._handler_messages import _with_model_prefix

    s = _streamer(prefix="", effort="")
    assert _with_model_prefix(s, "[Sonnet 5.5 / high]\nHi") == "[Sonnet 5.5 / high]\nHi"
