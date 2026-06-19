"""Regression tests for TelegramAdapterV2 polling configuration.

Guards the #666 regression: the bot's Telegram ``allowed_updates`` is a sticky,
bot-global setting. The main poller must (re-)assert a non-narrowing value on
EVERY ``start_polling`` so a filter narrowed by a secondary poller sharing the
token cannot silently drop ``callback_query`` (inline-button) delivery. We send
an empty list — Telegram's default update set, which includes ``callback_query``.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.channels.telegram.adapter_v2 import TelegramAdapterV2


def _adapter() -> TelegramAdapterV2:
    return TelegramAdapterV2(token="123:abc", conversation_loop=MagicMock())


@pytest.mark.asyncio
async def test_stall_restart_reasserts_allowed_updates() -> None:
    """A watchdog stall-restart must pass a non-narrowing allowed_updates so it
    never inherits a narrowed sticky filter (which would drop callback_query)."""
    adapter = _adapter()
    adapter._app = MagicMock()
    adapter._app.updater = AsyncMock()

    await adapter._handle_polling_stall()

    start_polling = adapter._app.updater.start_polling
    start_polling.assert_awaited_once()
    kwargs = start_polling.await_args.kwargs
    # [] == Telegram's default update set (includes message + callback_query);
    # the one thing we must never do is narrow it (e.g. to ["message"]).
    assert kwargs["allowed_updates"] == []
    assert kwargs["drop_pending_updates"] is False


@pytest.mark.asyncio
async def test_stall_restart_retry_also_asserts_allowed_updates() -> None:
    """If the first restart attempt fails, the retry must STILL pass the
    non-narrowing allowed_updates — the kwarg must be on every attempt, not just
    the happy path."""
    adapter = _adapter()
    adapter._app = MagicMock()
    updater = AsyncMock()
    # First start_polling attempt raises; the retry succeeds.
    updater.start_polling.side_effect = [RuntimeError("boom"), None]
    adapter._app.updater = updater

    # Patch the inter-attempt backoff so the test doesn't actually sleep 5s.
    with patch(
        "genesis.channels.telegram.adapter_v2.asyncio.sleep", new=AsyncMock()
    ):
        await adapter._handle_polling_stall()

    assert updater.start_polling.await_count == 2
    for call in updater.start_polling.await_args_list:
        assert call.kwargs["allowed_updates"] == []


@pytest.mark.asyncio
async def test_start_updater_polling_never_narrows() -> None:
    """The single polling-start helper — used by BOTH the initial ``start()`` and
    every stall-restart — must always pass a non-narrowing ``allowed_updates=[]``,
    so neither call site can drop callback_query (inline-button) delivery."""
    adapter = _adapter()
    adapter._app = MagicMock()
    adapter._app.updater = AsyncMock()

    await adapter._start_updater_polling()

    kwargs = adapter._app.updater.start_polling.await_args.kwargs
    assert kwargs["allowed_updates"] == []
    assert kwargs["drop_pending_updates"] is False
