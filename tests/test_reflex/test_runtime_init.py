"""Tests for runtime/init/reflex.py — gating, wiring, default-bus install."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import genesis.util.tasks as tasks_mod
from genesis.reflex.config import ReflexConfig
from genesis.runtime.init import reflex as reflex_init


@pytest.fixture(autouse=True)
def _reset_default_bus():
    tasks_mod.set_default_event_bus(None)
    yield
    tasks_mod.set_default_event_bus(None)


def _rt():
    rt = MagicMock()
    rt._db = MagicMock()
    rt._event_bus = MagicMock()
    rt._reflex_ingestor = None
    return rt


class TestGating:
    @pytest.mark.asyncio
    async def test_disabled_config_stays_fully_dark(self):
        rt = _rt()
        with patch(
            "genesis.reflex.config.load_reflex_config",
            return_value=ReflexConfig(ingest_enabled=False),
        ):
            await reflex_init.init(rt)
        rt._event_bus.subscribe.assert_not_called()
        assert rt._reflex_ingestor is None
        assert tasks_mod._default_event_bus is None  # nerve NOT wired

    @pytest.mark.asyncio
    async def test_missing_db_skips(self):
        rt = _rt()
        rt._db = None
        with patch(
            "genesis.reflex.config.load_reflex_config",
            return_value=ReflexConfig(ingest_enabled=True),
        ):
            await reflex_init.init(rt)
        rt._event_bus.subscribe.assert_not_called()
        assert tasks_mod._default_event_bus is None

    @pytest.mark.asyncio
    async def test_enabled_wires_everything(self):
        rt = _rt()
        # patch BOTH the init-path read and the ingestor's own bound reference
        # (the ingestor re-reads config for live toggling) so both see enabled
        with (
            patch(
                "genesis.reflex.config.load_reflex_config",
                return_value=ReflexConfig(ingest_enabled=True),
            ),
            patch(
                "genesis.reflex.ingest.load_reflex_config",
                return_value=ReflexConfig(ingest_enabled=True),
            ),
        ):
            await reflex_init.init(rt)
        # subscriber registered at ERROR floor
        rt._event_bus.subscribe.assert_called_once()
        assert rt._event_bus.subscribe.call_args.kwargs["min_severity"].name == "ERROR"
        # ingestor stashed on the runtime, worker started
        assert rt._reflex_ingestor is not None
        assert rt._reflex_ingestor._worker_task is not None
        # THE nerve wiring: default bus installed for all tracked_task sites
        assert tasks_mod._default_event_bus is rt._event_bus
        rt._reflex_ingestor._worker_task.cancel()
