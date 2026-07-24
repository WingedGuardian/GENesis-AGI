"""Tests for the CampaignRunner tick logic."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_session_runner():
    runner = MagicMock()
    runner.active_count.return_value = 0
    runner._MAX_CONCURRENT = 2
    runner.spawn = AsyncMock(return_value="sess-001")
    return runner


@pytest.fixture
async def campaign_in_db(db):
    """Create a test campaign in the DB and return its ID."""
    from genesis.db.crud import campaigns as crud

    await crud.create_campaign(
        db,
        id="test-camp",
        name="test-campaign",
        strategy_doc_path="/tmp/test-strategy.md",
        cron_cadence="0 */8 * * *",
        created_at="2026-06-07T00:00:00Z",
        pre_checks='["rate_limit", "budget"]',
        max_daily_cost_usd=1.0,
        state_json='{"posts_today": 0, "total_posts": 0}',
    )
    return "test-camp"


class TestCampaignTick:
    @pytest.mark.anyio
    async def test_tick_skips_when_budget_exceeded(self, db, campaign_in_db, mock_session_runner):
        from genesis.campaigns.runner import CampaignRunner

        runner = CampaignRunner(
            db=db,
            session_runner=mock_session_runner,
        )

        # Burn through the budget
        from genesis.db.crud import campaigns as crud

        await crud.create_run(
            db,
            id="r-budget",
            campaign_id=campaign_in_db,
            started_at=datetime.now(UTC).isoformat(),
            trigger_type="scheduled",
        )
        await crud.complete_run(
            db,
            "r-budget",
            outcome="success",
            cost_usd=5.0,
            finished_at=datetime.now(UTC).isoformat(),
        )

        result = await runner.campaign_tick(campaign_in_db)
        assert result["outcome"] == "skip"
        assert "budget" in result.get("skip_reason", "").lower()
        mock_session_runner.spawn.assert_not_called()

    @pytest.mark.anyio
    async def test_tick_dispatches_session_when_prechecks_pass(
        self, db, campaign_in_db, mock_session_runner
    ):
        from genesis.campaigns.runner import CampaignRunner

        runner = CampaignRunner(
            db=db,
            session_runner=mock_session_runner,
        )

        # Write a minimal strategy doc
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write("# Test Strategy\nPost something useful.\n")
            strategy_path = f.name

        try:
            from genesis.db.crud import campaigns as crud

            await crud.update_campaign(db, campaign_in_db, strategy_doc_path=strategy_path)

            result = await runner.campaign_tick(campaign_in_db)

            assert result["outcome"] == "dispatched"
            mock_session_runner.spawn.assert_called_once()

            # Verify the spawn call has the right structure
            call_args = mock_session_runner.spawn.call_args
            request = call_args[0][0]  # First positional arg
            assert "campaign:test-camp" in (request.caller_context or "")
        finally:
            os.unlink(strategy_path)

    @pytest.mark.anyio
    async def test_tick_skips_when_session_pending(self, db, campaign_in_db, mock_session_runner):
        """If a previous session is still running, skip this tick."""
        from genesis.campaigns.runner import CampaignRunner

        runner = CampaignRunner(
            db=db,
            session_runner=mock_session_runner,
        )

        # Set state to indicate a pending session
        from genesis.db.crud import campaigns as crud

        state = {"posts_today": 0, "total_posts": 0, "_pending_session_id": "sess-old"}
        await crud.update_campaign_state(db, campaign_in_db, json.dumps(state))

        # Mock the session as still running (not completed)
        with patch("genesis.campaigns.runner._check_session_status") as mock_check:
            mock_check.return_value = None  # None = still running
            result = await runner.campaign_tick(campaign_in_db)

        assert result["outcome"] == "skip"
        assert "pending" in result.get("skip_reason", "").lower()

    @pytest.mark.anyio
    async def test_tick_captures_completed_session_results(
        self, db, campaign_in_db, mock_session_runner
    ):
        """When a previous session completed, capture results before new tick."""
        from genesis.campaigns.runner import CampaignRunner

        runner = CampaignRunner(
            db=db,
            session_runner=mock_session_runner,
        )

        # Set state with a completed pending session
        from genesis.db.crud import campaigns as crud

        state = {
            "posts_today": 0,
            "total_posts": 0,
            "_pending_session_id": "sess-done",
            "_pending_run_id": "run-done",
        }
        await crud.update_campaign_state(db, campaign_in_db, json.dumps(state))

        # Create the pending run in DB
        await crud.create_run(
            db,
            id="run-done",
            campaign_id=campaign_in_db,
            started_at="2026-06-07T00:30:00Z",
            trigger_type="scheduled",
        )

        # Mock session as completed with structured output
        completed_result = {
            "success": True,
            "output_text": json.dumps(
                {
                    "state_updates": {"posts_today": 1, "total_posts": 1},
                    "summary": "Posted to #dev-discussion",
                }
            ),
            "cost_usd": 0.03,
        }

        with patch("genesis.campaigns.runner._check_session_status") as mock_check:
            mock_check.return_value = completed_result

            import os
            import tempfile

            with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
                f.write("# Test\n")
                strategy_path = f.name

            try:
                await crud.update_campaign(db, campaign_in_db, strategy_doc_path=strategy_path)
                await runner.campaign_tick(campaign_in_db)
            finally:
                os.unlink(strategy_path)

        # Previous session results should be captured and state updated
        campaign = await crud.get_campaign(db, campaign_in_db)
        state = json.loads(campaign["state_json"])
        assert state["posts_today"] == 1
        assert state["total_posts"] == 1
        # After capture, the runner dispatches a NEW session, so there
        # will be a new _pending_session_id (not the old one)
        if "_pending_session_id" in state:
            assert state["_pending_session_id"] != "sess-done"


class TestJsonExtraction:
    def test_extracts_direct_json(self):
        from genesis.campaigns.runner import _extract_json

        raw = '{"state_updates": {"x": 1}, "summary": "done"}'
        result = _extract_json(raw)
        assert result is not None
        assert result["state_updates"]["x"] == 1

    def test_extracts_from_code_block(self):
        from genesis.campaigns.runner import _extract_json

        raw = 'Some text\n```json\n{"state_updates": {"y": 2}, "summary": "ok"}\n```\nMore text'
        result = _extract_json(raw)
        assert result is not None
        assert result["state_updates"]["y"] == 2

    def test_extracts_from_braces(self):
        from genesis.campaigns.runner import _extract_json

        raw = 'Here is the output: {"state_updates": {"z": 3}, "summary": "fine"} end.'
        result = _extract_json(raw)
        assert result is not None
        assert result["state_updates"]["z"] == 3

    def test_returns_none_on_garbage(self):
        from genesis.campaigns.runner import _extract_json

        assert _extract_json("no json here") is None
        assert _extract_json("") is None


class TestStateValidation:
    def test_rejects_unknown_keys(self):
        from genesis.campaigns.runner import _validate_state_updates

        current = {"posts_today": 0, "total_posts": 0}
        updates = {"posts_today": 1, "new_key": "bad"}
        valid = _validate_state_updates(updates, current)
        assert "posts_today" in valid
        assert "new_key" not in valid

    def test_rejects_type_changes(self):
        from genesis.campaigns.runner import _validate_state_updates

        current = {"posts_today": 0, "name": "test"}
        updates = {"posts_today": "not_a_number"}
        valid = _validate_state_updates(updates, current)
        assert "posts_today" not in valid

    def test_allows_valid_updates(self):
        from genesis.campaigns.runner import _validate_state_updates

        current = {"posts_today": 0, "total_posts": 5}
        updates = {"posts_today": 2, "total_posts": 7}
        valid = _validate_state_updates(updates, current)
        assert valid == {"posts_today": 2, "total_posts": 7}

    def test_allows_none_to_value(self):
        from genesis.campaigns.runner import _validate_state_updates

        current = {"last_channel": None, "count": 0}
        updates = {"last_channel": "showcase"}
        valid = _validate_state_updates(updates, current)
        assert valid["last_channel"] == "showcase"


class TestDayBoundaryReset:
    def test_resets_today_keys(self):
        from genesis.campaigns.runner import _day_boundary_reset

        state = {"posts_today": 3, "cost_daily": 0.5, "total_posts": 10}
        reset = _day_boundary_reset(state, "2026-06-06T23:00:00Z", "2026-06-07T01:00:00Z")
        assert reset["posts_today"] == 0
        assert reset["cost_daily"] == 0
        assert reset["total_posts"] == 10  # Not a daily counter

    def test_no_reset_same_day(self):
        from genesis.campaigns.runner import _day_boundary_reset

        state = {"posts_today": 3, "total_posts": 10}
        reset = _day_boundary_reset(state, "2026-06-07T08:00:00Z", "2026-06-07T16:00:00Z")
        assert reset["posts_today"] == 3  # Same day, no reset


class TestTickWrapperJobHealth:
    """_tick_wrapper records job health so tick crashes are observable (#15)."""

    @pytest.mark.anyio
    async def test_records_failure_on_exception(self, db, mock_session_runner):
        from genesis.campaigns.runner import CampaignRunner

        runner = CampaignRunner(db=db, session_runner=mock_session_runner)
        runner.campaign_tick = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("genesis.runtime.GenesisRuntime") as rt_cls:
            inst = MagicMock()
            rt_cls.instance.return_value = inst
            # Must NOT propagate — the tick-level swallow contract is preserved.
            await runner._tick_wrapper("camp-1", "campaign_test")

        inst.record_job_failure.assert_called_once()
        call = inst.record_job_failure.call_args
        assert call.args[0] == "campaign_test"
        # The exception is now threaded through (exc=), not pre-stringified.
        assert "boom" in str(call.kwargs["exc"])
        inst.record_job_success.assert_not_called()

    @pytest.mark.anyio
    async def test_records_success_on_clean_tick(self, db, mock_session_runner):
        from genesis.campaigns.runner import CampaignRunner

        runner = CampaignRunner(db=db, session_runner=mock_session_runner)
        runner.campaign_tick = AsyncMock(return_value={"outcome": "skip"})

        with patch("genesis.runtime.GenesisRuntime") as rt_cls:
            inst = MagicMock()
            rt_cls.instance.return_value = inst
            await runner._tick_wrapper("camp-1", "campaign_test")

        inst.record_job_success.assert_called_once_with("campaign_test")
        inst.record_job_failure.assert_not_called()

    @pytest.mark.anyio
    async def test_success_record_errors_are_suppressed(self, db, mock_session_runner):
        """A failure recording the SUCCESS heartbeat must never propagate."""
        from genesis.campaigns.runner import CampaignRunner

        runner = CampaignRunner(db=db, session_runner=mock_session_runner)
        runner.campaign_tick = AsyncMock(return_value={"outcome": "skip"})

        with patch("genesis.runtime.GenesisRuntime") as rt_cls:
            rt_cls.instance.side_effect = RuntimeError("runtime not ready")
            # Must not raise even though instance() blows up.
            await runner._tick_wrapper("camp-1", "campaign_test")

    @pytest.mark.anyio
    async def test_failure_record_errors_are_suppressed(self, db, mock_session_runner):
        """A failed tick whose record_job_failure ALSO raises must not propagate."""
        from genesis.campaigns.runner import CampaignRunner

        runner = CampaignRunner(db=db, session_runner=mock_session_runner)
        runner.campaign_tick = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("genesis.runtime.GenesisRuntime") as rt_cls:
            inst = MagicMock()
            inst.record_job_failure.side_effect = RuntimeError("record blew up")
            rt_cls.instance.return_value = inst
            # tick raised AND record_job_failure raised — still must not propagate.
            await runner._tick_wrapper("camp-1", "campaign_test")
