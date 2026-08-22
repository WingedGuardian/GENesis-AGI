"""Regression guard for follow-up 69f30078.

TelegramAdapterV2.send_message persists Genesis's own outbound messages. It must
store them with direction="outbound" — the CRUD default is "inbound", and the
canonical handler path (_handler_messages.py / topics.py) already passes
direction="outbound", so this adapter store must match. Nothing branches on the
column today, but a truthful label matters for analytics and future consumers.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.channels.telegram.adapter_v2 import TelegramAdapterV2


@pytest.mark.asyncio
async def test_send_message_persists_outbound_direction():
    adapter = TelegramAdapterV2(token="123:abc", conversation_loop=MagicMock())
    adapter._app = MagicMock()  # truthy so send_message proceeds
    fake_msg = MagicMock(message_id=999)
    store_mock = AsyncMock()

    with (
        patch(
            "genesis.channels.telegram.adapter_v2.send_with_client_heal",
            new=AsyncMock(return_value=fake_msg),
        ),
        patch("genesis.db.crud.telegram_messages.store", new=store_mock),
    ):
        await adapter.send_message("12345", "hello from genesis")

    store_mock.assert_awaited_once()
    _, kwargs = store_mock.call_args
    assert kwargs["sender"] == "genesis"
    assert kwargs["direction"] == "outbound"
