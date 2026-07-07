"""Tests for _check_omi_ingest — the OMI ingest unit-liveness watcher.

OMI transcripts stop SILENTLY if the ingest unit dies, so the awareness tick
watches the unit. The check is config-gated (only alerts once the user has an
enabled ~/.genesis/omi_config.yaml) and best-effort (never breaks the tick).
Everything external — the config load and the systemctl probe — is injected.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from genesis.attention import omi_ingest
from genesis.awareness import loop

_DB = object()  # non-None sentinel; observations calls are mocked


class _Cfg:  # truthy stand-in for an OmiConfig
    pass


@pytest.fixture(autouse=True)
def _mocks(monkeypatch):
    monkeypatch.setattr(loop, "_last_omi_alert_at", None)
    create = AsyncMock()
    resolve = AsyncMock(return_value=0)
    monkeypatch.setattr(loop.observations, "create", create)
    monkeypatch.setattr(loop.observations, "resolve_by_source_and_type", resolve)
    monkeypatch.setattr(omi_ingest, "load_omi_config", lambda: _Cfg())  # configured+enabled
    active = AsyncMock(return_value=True)
    monkeypatch.setattr(loop, "_systemd_unit_active", active)
    return create, resolve, active


@pytest.mark.asyncio
async def test_enabled_but_unit_down_alerts(_mocks):
    create, _, active = _mocks
    active.return_value = False
    await loop._check_omi_ingest(_DB)
    create.assert_awaited_once()
    kw = create.await_args.kwargs
    assert kw["type"] == "infrastructure_alert"
    assert kw["source"] == "omi_ingest_monitor"
    assert kw["skip_if_duplicate"] is True
    assert kw.get("content_hash")
    assert "omi" in kw["content"].lower()


@pytest.mark.asyncio
async def test_enabled_and_active_resolves(_mocks):
    create, resolve, _ = _mocks
    await loop._check_omi_ingest(_DB)
    create.assert_not_awaited()
    resolve.assert_awaited_once()  # healthy → clear any stale alert


@pytest.mark.asyncio
async def test_not_configured_short_circuits(_mocks, monkeypatch):
    create, _, active = _mocks
    monkeypatch.setattr(omi_ingest, "load_omi_config", lambda: None)
    await loop._check_omi_ingest(_DB)
    create.assert_not_awaited()
    active.assert_not_awaited()  # never probe systemctl if OMI isn't configured


@pytest.mark.asyncio
async def test_indeterminate_unit_state_no_alert_no_resolve(_mocks):
    create, resolve, active = _mocks
    active.return_value = None  # systemctl couldn't be determined → don't guess
    await loop._check_omi_ingest(_DB)
    create.assert_not_awaited()
    resolve.assert_not_awaited()


@pytest.mark.asyncio
async def test_cooldown_suppresses_second_alert(_mocks):
    create, _, active = _mocks
    active.return_value = False
    await loop._check_omi_ingest(_DB)
    await loop._check_omi_ingest(_DB)
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_db_none_noop(_mocks):
    create, _, active = _mocks
    await loop._check_omi_ingest(None)
    create.assert_not_awaited()
    active.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_config_does_not_alert(_mocks, monkeypatch):
    create, _, active = _mocks

    def boom():
        raise ValueError("bad yaml")

    monkeypatch.setattr(omi_ingest, "load_omi_config", boom)
    await loop._check_omi_ingest(_DB)
    create.assert_not_awaited()  # malformed config is a separate concern, not "down"


@pytest.mark.asyncio
async def test_check_never_raises(_mocks):
    create, _, active = _mocks
    active.side_effect = RuntimeError("systemctl blew up")
    await loop._check_omi_ingest(_DB)  # must not propagate into the tick
    create.assert_not_awaited()
