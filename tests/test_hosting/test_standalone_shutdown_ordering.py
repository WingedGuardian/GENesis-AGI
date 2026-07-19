"""StandaloneAdapter.shutdown ordering guard.

Background senders that push through the Telegram send client (the outreach
recovery worker) must be stopped BEFORE the Telegram adapter closes its httpx
client. Otherwise a late retry fires through a torn-down client, raises the
closed-client error, and after exhausting its retries gets permanently
discarded — the failure that silently dropped two Sentinel approval requests
on 2026-07-15. The runtime is only torn down AFTER the adapter, so the required
order is: stop_outbound_senders -> adapter.stop -> runtime.shutdown.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.hosting.standalone import StandaloneAdapter

pytestmark = pytest.mark.asyncio


async def test_outbound_senders_stopped_before_telegram_client_closes():
    adapter = StandaloneAdapter()

    order: list[str] = []

    runtime = MagicMock()
    runtime.awareness_loop = None  # skip the soft-stop branch
    runtime.stop_outbound_senders = AsyncMock(side_effect=lambda: order.append("stop_senders"))
    runtime.shutdown = AsyncMock(side_effect=lambda: order.append("runtime_shutdown"))

    telegram_adapter = MagicMock()
    telegram_adapter.stop = AsyncMock(side_effect=lambda: order.append("adapter_stop"))

    adapter._runtime = runtime
    adapter._telegram_adapter = telegram_adapter
    # Neutralize the unrelated shutdown steps so the test isolates ordering.
    adapter._voice_last_breath = AsyncMock()
    adapter._shutdown_wyoming_servers = AsyncMock()

    await adapter.shutdown()

    assert order == ["stop_senders", "adapter_stop", "runtime_shutdown"], (
        "outbound senders must stop before the Telegram client closes, and the "
        "runtime tears down last"
    )


async def test_shutdown_survives_absent_telegram_adapter():
    # No Telegram configured: still stop senders, still shut the runtime down.
    adapter = StandaloneAdapter()
    order: list[str] = []

    runtime = MagicMock()
    runtime.awareness_loop = None
    runtime.stop_outbound_senders = AsyncMock(side_effect=lambda: order.append("stop_senders"))
    runtime.shutdown = AsyncMock(side_effect=lambda: order.append("runtime_shutdown"))

    adapter._runtime = runtime
    adapter._telegram_adapter = None
    adapter._voice_last_breath = AsyncMock()
    adapter._shutdown_wyoming_servers = AsyncMock()

    await adapter.shutdown()

    assert order == ["stop_senders", "runtime_shutdown"]
