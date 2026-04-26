"""Tests for the ego runtime init module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.runtime.init import ego


def _make_runtime(**overrides):
    """Minimal mock runtime with all ego dependencies available."""
    rt = MagicMock()
    rt._db = overrides.get("_db", AsyncMock())
    rt._router = overrides.get("_router", MagicMock())
    rt._cc_invoker = overrides.get("_cc_invoker", MagicMock())
    rt._session_manager = overrides.get("_session_manager", MagicMock())
    rt._health_data = overrides.get("_health_data", MagicMock())
    rt._event_bus = overrides.get("_event_bus", MagicMock())
    rt._idle_detector = overrides.get("_idle_detector", MagicMock())
    rt._autonomous_dispatcher = overrides.get("_autonomous_dispatcher", MagicMock())
    return rt


class TestEgoInitSkips:
    """Ego init should exit early when hard dependencies are missing."""

    @pytest.mark.asyncio
    async def test_skips_when_db_missing(self):
        rt = _make_runtime(_db=None)
        await ego.init(rt)
        # _ego_session should NOT have been set (MagicMock would
        # auto-create it on read, so we check the setter wasn't called)
        calls = [c for c in rt.mock_calls if "_ego_session" in str(c)]
        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_skips_when_router_missing(self):
        rt = _make_runtime(_router=None)
        await ego.init(rt)
        calls = [c for c in rt.mock_calls if "_ego_session" in str(c)]
        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_skips_when_cc_invoker_missing(self):
        rt = _make_runtime(_cc_invoker=None)
        await ego.init(rt)
        calls = [c for c in rt.mock_calls if "_ego_session" in str(c)]
        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_skips_when_session_manager_missing(self):
        rt = _make_runtime(_session_manager=None)
        await ego.init(rt)
        calls = [c for c in rt.mock_calls if "_ego_session" in str(c)]
        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_skips_when_disabled_by_config(self):
        rt = _make_runtime()
        with patch("genesis.ego.config.load_ego_config") as mock_load:
            mock_config = MagicMock()
            mock_config.enabled = False
            mock_load.return_value = mock_config
            await ego.init(rt)
            calls = [c for c in rt.mock_calls if "_ego_session" in str(c)]
            assert len(calls) == 0


class TestEgoInitWiring:
    """Ego init should create session, cadence, and wire dependencies."""

    @pytest.mark.asyncio
    async def test_creates_session_and_cadence(self):
        rt = _make_runtime()
        with (
            patch("genesis.ego.config.load_ego_config") as mock_load,
            patch("genesis.ego.session.EgoSession") as mock_session_cls,
            patch("genesis.ego.cadence.EgoCadenceManager") as mock_cadence_cls,
            patch("genesis.ego.compaction.CompactionEngine"),
            patch("genesis.ego.context.EgoContextBuilder"),
            patch("genesis.ego.proposals.ProposalWorkflow"),
            patch("genesis.ego.dispatch.EgoDispatcher"),
        ):
            mock_config = MagicMock()
            mock_config.enabled = True
            mock_config.cadence_minutes = 60
            mock_config.model = "opus"
            mock_config.daily_budget_cap_usd = 10.0
            mock_load.return_value = mock_config

            mock_cadence = AsyncMock()
            mock_cadence_cls.return_value = mock_cadence

            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session

            await ego.init(rt)

            # Session created
            mock_session_cls.assert_called_once()

            # Cadence created and started
            mock_cadence_cls.assert_called_once()
            mock_cadence.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_wires_autonomous_dispatcher(self):
        rt = _make_runtime()
        with (
            patch("genesis.ego.config.load_ego_config") as mock_load,
            patch("genesis.ego.session.EgoSession") as mock_session_cls,
            patch("genesis.ego.cadence.EgoCadenceManager") as mock_cadence_cls,
            patch("genesis.ego.compaction.CompactionEngine"),
            patch("genesis.ego.context.EgoContextBuilder"),
            patch("genesis.ego.proposals.ProposalWorkflow"),
            patch("genesis.ego.dispatch.EgoDispatcher"),
        ):
            mock_config = MagicMock()
            mock_config.enabled = True
            mock_config.cadence_minutes = 60
            mock_config.model = "opus"
            mock_config.daily_budget_cap_usd = 10.0
            mock_load.return_value = mock_config

            mock_cadence = AsyncMock()
            mock_cadence_cls.return_value = mock_cadence

            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session

            await ego.init(rt)

            mock_session.set_autonomous_dispatcher.assert_called_once_with(
                rt._autonomous_dispatcher,
            )

    @pytest.mark.asyncio
    async def test_skips_dispatcher_when_none(self):
        rt = _make_runtime(_autonomous_dispatcher=None)
        with (
            patch("genesis.ego.config.load_ego_config") as mock_load,
            patch("genesis.ego.session.EgoSession") as mock_session_cls,
            patch("genesis.ego.cadence.EgoCadenceManager") as mock_cadence_cls,
            patch("genesis.ego.compaction.CompactionEngine"),
            patch("genesis.ego.context.EgoContextBuilder"),
            patch("genesis.ego.proposals.ProposalWorkflow"),
            patch("genesis.ego.dispatch.EgoDispatcher"),
        ):
            mock_config = MagicMock()
            mock_config.enabled = True
            mock_config.cadence_minutes = 60
            mock_config.model = "opus"
            mock_config.daily_budget_cap_usd = 10.0
            mock_load.return_value = mock_config

            mock_cadence = AsyncMock()
            mock_cadence_cls.return_value = mock_cadence

            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session

            await ego.init(rt)

            mock_session.set_autonomous_dispatcher.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_no_idle_detector(self):
        """Ego should initialize even if surplus didn't provide idle_detector."""
        rt = _make_runtime(_idle_detector=None)
        with (
            patch("genesis.ego.config.load_ego_config") as mock_load,
            patch("genesis.ego.session.EgoSession"),
            patch("genesis.ego.cadence.EgoCadenceManager") as mock_cadence_cls,
            patch("genesis.ego.compaction.CompactionEngine"),
            patch("genesis.ego.context.EgoContextBuilder"),
            patch("genesis.ego.proposals.ProposalWorkflow"),
            patch("genesis.ego.dispatch.EgoDispatcher"),
        ):
            mock_config = MagicMock()
            mock_config.enabled = True
            mock_config.cadence_minutes = 60
            mock_config.model = "opus"
            mock_config.daily_budget_cap_usd = 10.0
            mock_load.return_value = mock_config

            mock_cadence = AsyncMock()
            mock_cadence_cls.return_value = mock_cadence

            await ego.init(rt)

            call_kwargs = mock_cadence_cls.call_args[1]
            assert call_kwargs["idle_detector"] is None
            mock_cadence.start.assert_awaited_once()
