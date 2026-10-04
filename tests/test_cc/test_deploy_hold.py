from __future__ import annotations

import pytest

from genesis.cc import deploy_hold


@pytest.mark.asyncio
async def test_wait_for_deploy_clear_waits_then_allows(monkeypatch):
    states = iter([True, True, False])
    monkeypatch.setattr(deploy_hold, "update_in_progress", lambda: next(states))
    monkeypatch.setattr(deploy_hold, "_POLL_INTERVAL_S", 0)

    assert await deploy_hold.wait_for_deploy_clear(timeout_s=1)


@pytest.mark.asyncio
async def test_wait_for_deploy_clear_times_out(monkeypatch):
    monkeypatch.setattr(deploy_hold, "update_in_progress", lambda: True)
    monkeypatch.setattr(deploy_hold, "_POLL_INTERVAL_S", 0)

    assert not await deploy_hold.wait_for_deploy_clear(timeout_s=0)


def test_spawn_wait_timeout_invalid_value_uses_default(monkeypatch):
    monkeypatch.setenv("GENESIS_DEPLOY_SPAWN_WAIT_S", "not-a-number")

    assert deploy_hold.spawn_wait_timeout() == deploy_hold.DEFAULT_SPAWN_WAIT_S
