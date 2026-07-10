"""The server-side dual-poller belt in StandaloneAdapter._start_telegram.

The primary guard is bridge-side (the bridge yields when the server lock is
held); this belt covers the reverse direction — a pre-guard bridge binary or
a probe-race survivor — by logging CRITICAL while still starting the adapter
(the server is the owner).
"""

import logging
from unittest.mock import patch

import pytest

from genesis.hosting.standalone import StandaloneAdapter

pytestmark = pytest.mark.asyncio


async def test_bridge_running_logs_critical(caplog):
    adapter = StandaloneAdapter()
    with patch(
        "genesis.util.process_lock.ProcessLock.is_locked", return_value=True,
    ), patch(
        "genesis.channels.bridge._load_bridge_config", return_value=None,
    ), caplog.at_level(logging.CRITICAL, logger="genesis.hosting.standalone"):
        await adapter._start_telegram()
    assert any(
        "genesis-bridge is running" in r.message for r in caplog.records
    ), "belt must announce the dual-poller condition loudly"


async def test_no_bridge_no_noise(caplog):
    adapter = StandaloneAdapter()
    with patch(
        "genesis.util.process_lock.ProcessLock.is_locked", return_value=False,
    ), patch(
        "genesis.channels.bridge._load_bridge_config", return_value=None,
    ), caplog.at_level(logging.CRITICAL, logger="genesis.hosting.standalone"):
        await adapter._start_telegram()
    assert not any(
        "genesis-bridge is running" in r.message for r in caplog.records
    )
