"""Tests for campaign pre-check registry."""

from __future__ import annotations

from datetime import UTC
from unittest.mock import MagicMock

import pytest

from genesis.campaigns.prechecks import (
    check_budget,
    check_rate_limit,
    check_slots_available,
    run_prechecks,
)


class TestRateLimit:
    @pytest.mark.anyio
    async def test_passes_when_no_last_run(self):
        campaign = {"last_run_at": None, "cron_cadence": "0 */8 * * *"}
        ok, reason = await check_rate_limit(campaign, ctx={})
        assert ok is True

    @pytest.mark.anyio
    async def test_passes_when_enough_time_elapsed(self):
        campaign = {
            "last_run_at": "2026-06-06T00:00:00Z",
            "cron_cadence": "0 */8 * * *",
        }
        ok, reason = await check_rate_limit(campaign, ctx={})
        assert ok is True  # Last run was over a day ago

    @pytest.mark.anyio
    async def test_fails_when_too_recent(self):
        from datetime import datetime

        now = datetime.now(UTC).isoformat()
        campaign = {"last_run_at": now, "cron_cadence": "0 */1 * * *"}
        ok, reason = await check_rate_limit(campaign, ctx={})
        assert ok is False
        assert "rate_limit" in (reason or "").lower() or "too recent" in (reason or "").lower()


class TestBudget:
    @pytest.mark.anyio
    async def test_passes_under_budget(self):
        campaign = {"id": "c1", "max_daily_cost_usd": 1.0}
        ctx = {"daily_cost": 0.50}
        ok, reason = await check_budget(campaign, ctx=ctx)
        assert ok is True

    @pytest.mark.anyio
    async def test_fails_over_budget(self):
        campaign = {"id": "c1", "max_daily_cost_usd": 1.0}
        ctx = {"daily_cost": 1.50}
        ok, reason = await check_budget(campaign, ctx=ctx)
        assert ok is False
        assert "budget" in (reason or "").lower()


class TestSlotsAvailable:
    @pytest.mark.anyio
    async def test_passes_when_slots_free(self):
        runner = MagicMock()
        runner.active_count.return_value = 0
        runner._MAX_CONCURRENT = 2
        campaign = {}
        ok, reason = await check_slots_available(campaign, ctx={"session_runner": runner})
        assert ok is True

    @pytest.mark.anyio
    async def test_fails_when_slots_full(self):
        runner = MagicMock()
        runner.active_count.return_value = 2
        runner._MAX_CONCURRENT = 2
        campaign = {}
        ok, reason = await check_slots_available(campaign, ctx={"session_runner": runner})
        assert ok is False
        assert "slots" in (reason or "").lower()


class TestRunPrechecks:
    @pytest.mark.anyio
    async def test_runs_all_checks_and_passes(self):
        campaign = {
            "id": "c1",
            "last_run_at": None,
            "cron_cadence": "0 */8 * * *",
            "max_daily_cost_usd": 1.0,
            "pre_checks": '["rate_limit", "budget"]',
        }
        ctx = {"daily_cost": 0.0}
        ok, reason = await run_prechecks(campaign, ctx)
        assert ok is True
        assert reason is None

    @pytest.mark.anyio
    async def test_stops_on_first_failure(self):
        campaign = {
            "id": "c1",
            "last_run_at": None,
            "cron_cadence": "0 */8 * * *",
            "max_daily_cost_usd": 0.01,
            "pre_checks": '["rate_limit", "budget"]',
        }
        ctx = {"daily_cost": 5.0}
        ok, reason = await run_prechecks(campaign, ctx)
        assert ok is False
        assert "budget" in (reason or "").lower()

    @pytest.mark.anyio
    async def test_unknown_check_skipped(self):
        campaign = {
            "id": "c1",
            "last_run_at": None,
            "cron_cadence": "0 */8 * * *",
            "max_daily_cost_usd": 1.0,
            "pre_checks": '["rate_limit", "nonexistent_check"]',
        }
        ctx = {"daily_cost": 0.0}
        ok, reason = await run_prechecks(campaign, ctx)
        assert ok is True  # Unknown checks are skipped, not failures
