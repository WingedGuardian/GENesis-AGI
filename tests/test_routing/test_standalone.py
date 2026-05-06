"""Tests for standalone router bootstrap (MCP child processes)."""

from __future__ import annotations

from genesis.routing.standalone import NullCostTracker
from genesis.routing.types import BudgetStatus


class TestNullCostTracker:
    async def test_check_budget_always_under_limit(self):
        ct = NullCostTracker()
        status = await ct.check_budget()
        assert status == BudgetStatus.UNDER_LIMIT

    async def test_record_is_noop(self):
        ct = NullCostTracker()
        await ct.record("test_site", "test_provider", None)

    async def test_db_attribute_is_none(self):
        ct = NullCostTracker()
        assert ct.db is None


class TestCreateStandaloneRouter:
    async def test_skips_when_router_exists(self):
        """If rt._router is already set, create_standalone_router is a no-op."""
        from genesis.routing.standalone import create_standalone_router
        from genesis.runtime._core import GenesisRuntime

        GenesisRuntime.reset()
        rt = GenesisRuntime.instance()
        sentinel = object()
        rt._router = sentinel

        create_standalone_router()

        assert rt._router is sentinel
        GenesisRuntime.reset()

    async def test_sets_router_when_none(self):
        """When rt._router is None, creates and sets a Router."""
        from genesis.routing.standalone import create_standalone_router
        from genesis.runtime._core import GenesisRuntime

        GenesisRuntime.reset()
        rt = GenesisRuntime.instance()
        assert rt._router is None

        create_standalone_router()

        assert rt._router is not None
        GenesisRuntime.reset()


class TestNullCostTrackerExtended:
    async def test_check_budget_with_task_id(self):
        ct = NullCostTracker()
        status = await ct.check_budget(task_id="some-task")
        assert status == BudgetStatus.UNDER_LIMIT


class TestCreateStandaloneRouterFailure:
    async def test_bootstrap_failure_leaves_router_none(self):
        """If config is missing, router stays None without raising."""
        from unittest.mock import patch

        from genesis.routing.standalone import create_standalone_router
        from genesis.runtime._core import GenesisRuntime

        GenesisRuntime.reset()
        rt = GenesisRuntime.instance()
        assert rt._router is None

        with patch("genesis.env.repo_root", return_value="/nonexistent"):
            create_standalone_router()

        assert rt._router is None  # failed gracefully
        GenesisRuntime.reset()
