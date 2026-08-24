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


class TestParkAwareSessionStatus:
    """_check_session_status must understand rate-limit parks (2026-08-17
    incident: a campaign session that got parked was captured as
    outcome=error while its resume later delivered successfully — the
    campaign's bookkeeping never learned)."""

    async def _seed_failed_parked_session(self, db, park_status: str):
        from genesis.db.crud import cc_sessions

        # Seed the park NOW-RELATIVE, not with an absolute literal. _check_session_status
        # ages the park's created_at against the REAL wall clock (runner.py:430,
        # `datetime.now(UTC) - created > _PARK_WAIT_BOUND`, bound = 7 days) and there is no
        # injectable now, so a fixed seed like "2026-08-17" silently works only until the
        # calendar crosses the bound, then flips these "still running" tests to failing (a
        # wall-clock time-bomb). A recent seed keeps the park within its wait window on any
        # run date. (The over-bound tests below deliberately UPDATE created_at to an old
        # value afterward, so this recent default does not affect them.)
        recent = datetime.now(UTC).isoformat()
        await cc_sessions.create(
            db, id="camp-sess-1", session_type="background_task",
            model="sonnet", effort="medium", source_tag="campaign",
            started_at=recent,
            last_activity_at=recent,
        )
        await db.execute(
            "UPDATE cc_sessions SET status='failed', metadata=? WHERE id=?",
            (json.dumps({"park_id": "rlp-test0001"}), "camp-sess-1"),
        )
        await db.execute(
            """INSERT INTO cc_rate_limit_parks
               (id, kind, dedup_key, payload_json, status, created_at, updated_at)
               VALUES (?, 'direct_session', 'dk1', '{}', ?, ?, ?)""",
            ("rlp-test0001", park_status, recent, recent),
        )
        await db.commit()

    @pytest.mark.asyncio
    async def test_parked_session_reads_as_still_running(self, db):
        from genesis.campaigns.runner import _check_session_status

        await self._seed_failed_parked_session(db, "parked")
        assert await _check_session_status(db, "camp-sess-1") is None

    @pytest.mark.asyncio
    async def test_resuming_session_reads_as_still_running(self, db):
        from genesis.campaigns.runner import _check_session_status

        await self._seed_failed_parked_session(db, "resuming")
        assert await _check_session_status(db, "camp-sess-1") is None

    @pytest.mark.asyncio
    async def test_resumed_park_captures_delivering_session_result(self, db):
        from genesis.campaigns.runner import _check_session_status
        from genesis.db.crud import cc_sessions

        await self._seed_failed_parked_session(db, "resumed")
        # The re-dispatched session that actually delivered:
        await cc_sessions.create(
            db, id="resumed-sess-9", session_type="background_task",
            model="sonnet", effort="medium", source_tag="campaign",
            started_at="2026-08-17T22:20:00+00:00",
            last_activity_at="2026-08-17T22:20:00+00:00",
        )
        await db.execute(
            "UPDATE cc_sessions SET status='completed', cost_usd=0.42, metadata=? "
            "WHERE id=?",
            (json.dumps({
                "caller_context": "rate_limit_resume:rlp-test0001",
                "output_text": "digest delivered after resume",
            }), "resumed-sess-9"),
        )
        await db.commit()
        result = await _check_session_status(db, "camp-sess-1")
        assert result is not None
        assert result["success"] is True
        assert "digest delivered after resume" in result["output_text"]

    @pytest.mark.asyncio
    async def test_resumed_park_without_delivering_row_fails_closed(self, db):
        from genesis.campaigns.runner import _check_session_status

        # A park is marked resumed only AFTER its delivering session is
        # persisted — a missing row means deleted/corrupted, never in-flight.
        # Returning None here would skip campaign ticks forever (Codex P2).
        await self._seed_failed_parked_session(db, "resumed")
        result = await _check_session_status(db, "camp-sess-1")
        assert result is not None
        assert result["success"] is False
        assert "delivering session row is missing" in result["output_text"]

    @pytest.mark.asyncio
    async def test_terminal_park_reads_as_failure(self, db):
        from genesis.campaigns.runner import _check_session_status

        await self._seed_failed_parked_session(db, "needs_user")
        result = await _check_session_status(db, "camp-sess-1")
        assert result is not None
        assert result["success"] is False
        assert "needs_user" in result["output_text"] or "needs_user" in str(result)


@pytest.mark.asyncio
async def test_store_result_stamps_extra_metadata(db):
    """The production writer for the park stamp: _store_result(extra_metadata=
    {"park_id": ...}) must land the key in cc_sessions.metadata — without it
    the park-aware reaper above has nothing to follow."""
    from types import SimpleNamespace

    from genesis.cc.direct_session import (
        DirectSessionRequest,
        DirectSessionResult,
        DirectSessionRunner,
    )
    from genesis.db.crud import cc_sessions

    await cc_sessions.create(
        db, id="stamp-sess", session_type="background_task",
        model="sonnet", effort="medium",
        started_at="2026-08-17T10:00:00+00:00",
        last_activity_at="2026-08-17T10:00:00+00:00",
    )
    runner = DirectSessionRunner.__new__(DirectSessionRunner)
    runner._rt = SimpleNamespace(_db=db)
    await runner._store_result(
        "stamp-sess",
        DirectSessionRequest(prompt="p"),
        DirectSessionResult(
            session_id="stamp-sess", success=False,
            error="rate_limited: parked for resume (rlp-x1)", duration_s=1.0,
        ),
        extra_metadata={"park_id": "rlp-x1"},
    )
    row = await cc_sessions.get_by_id(db, "stamp-sess")
    assert json.loads(row["metadata"])["park_id"] == "rlp-x1"


@pytest.mark.asyncio
async def test_open_park_past_wait_bound_reads_as_failure(db):
    """Audit F1: a park stuck open (parked/resuming) past the wait bound must
    surface as a failure — a stuck resume loop never reaches needs_user, and
    without the bound the campaign would skip its ticks forever."""
    from genesis.campaigns.runner import _check_session_status

    helper = TestParkAwareSessionStatus()
    await helper._seed_failed_parked_session(db, "parked")
    # Backdate the park's created_at beyond the 7-day bound.
    await db.execute(
        "UPDATE cc_rate_limit_parks SET created_at='2026-08-01T00:00:00+00:00' "
        "WHERE id='rlp-test0001'",
    )
    await db.commit()
    result = await _check_session_status(db, "camp-sess-1")
    assert result is not None
    assert result["success"] is False
    assert "wait bound exceeded" in result["output_text"]
    # Codex P2 lock: the abandoned park must be CLOSED — an open park would
    # let the resume engine run the same campaign action again later.
    row = await (await db.execute(
        "SELECT status FROM cc_rate_limit_parks WHERE id='rlp-test0001'",
    )).fetchone()
    assert str(row["status"]) == "cancelled"


@pytest.mark.asyncio
async def test_missing_park_row_falls_through_to_session_failure(db):
    """Audit F3a: session failed with a park_id whose park row is GONE —
    the sentinel fallback must return the session's own failure, never a
    permanent still-running None (that would be an invisible stall)."""
    from genesis.campaigns.runner import _check_session_status
    from genesis.db.crud import cc_sessions

    await cc_sessions.create(
        db, id="camp-sess-2", session_type="background_task",
        model="sonnet", effort="medium", source_tag="campaign",
        started_at="2026-08-17T10:00:00+00:00",
        last_activity_at="2026-08-17T10:00:00+00:00",
    )
    await db.execute(
        "UPDATE cc_sessions SET status='failed', metadata=? WHERE id=?",
        (json.dumps({"park_id": "rlp-gone", "output_text": "boom"}), "camp-sess-2"),
    )
    await db.commit()
    result = await _check_session_status(db, "camp-sess-2")
    assert result is not None
    assert result["success"] is False


@pytest.mark.asyncio
async def test_failed_park_cancel_keeps_campaign_attached(db, monkeypatch):
    """Codex P2 lock: when the over-bound park's cancel FAILS transiently,
    the campaign must stay attached (None = retry next tick) — returning the
    failure verdict would detach it while the park stays runnable."""
    from genesis.campaigns.runner import _check_session_status

    helper = TestParkAwareSessionStatus()
    await helper._seed_failed_parked_session(db, "parked")
    await db.execute(
        "UPDATE cc_rate_limit_parks SET created_at='2026-08-01T00:00:00+00:00' "
        "WHERE id='rlp-test0001'",
    )
    await db.commit()

    async def boom(
        _db, _pid, _status, *,
        expected_status=None, expected_claimed_at=None, expected_updated_at=None,
    ):
        raise RuntimeError("db lock")

    monkeypatch.setattr(
        "genesis.db.crud.cc_rate_limit_parks.mark_terminal_if_unchanged", boom,
    )
    assert await _check_session_status(db, "camp-sess-1") is None
    row = await (await db.execute(
        "SELECT status FROM cc_rate_limit_parks WHERE id='rlp-test0001'",
    )).fetchone()
    assert str(row["status"]) == "parked", "park must remain owned/attached"


@pytest.mark.asyncio
async def test_over_bound_park_with_active_claim_holds(db):
    """Codex round-3 lock: an over-bound RESUMING park with a FRESH claim may
    have a live queued delivery — cancelling the row wouldn't stop it, so the
    campaign must HOLD until the attempt terminates on its own."""
    from datetime import UTC, datetime

    from genesis.campaigns.runner import _check_session_status

    helper = TestParkAwareSessionStatus()
    await helper._seed_failed_parked_session(db, "resuming")
    await db.execute(
        "UPDATE cc_rate_limit_parks SET created_at='2026-08-01T00:00:00+00:00', "
        "claimed_at=? WHERE id='rlp-test0001'",
        (datetime.now(UTC).isoformat(),),
    )
    await db.commit()
    assert await _check_session_status(db, "camp-sess-1") is None
    row = await (await db.execute(
        "SELECT status FROM cc_rate_limit_parks WHERE id='rlp-test0001'",
    )).fetchone()
    assert str(row["status"]) == "resuming", "actively-claimed park must survive"


@pytest.mark.asyncio
async def test_over_bound_cancel_noops_when_claim_refreshed_in_window(db, monkeypatch):
    """TOCTOU lock (red-team): if a resume re-claim lands between _follow_park's
    read and its cancel — status stays 'resuming' but claimed_at refreshes — the
    (status, claimed_at)-versioned cancel must NO-OP, so a live resume is never
    clobbered to 'cancelled' (which would double-run its delivery)."""
    from datetime import UTC, datetime

    from genesis.campaigns.runner import _check_session_status
    from genesis.db.crud import cc_rate_limit_parks as parks

    helper = TestParkAwareSessionStatus()
    await helper._seed_failed_parked_session(db, "resuming")
    # Over-bound + STALE claim → takes the abandon (cancel) path.
    await db.execute(
        "UPDATE cc_rate_limit_parks SET created_at='2026-08-01T00:00:00+00:00', "
        "claimed_at='2026-08-01T01:00:00+00:00' WHERE id='rlp-test0001'",
    )
    await db.commit()

    real_guard = parks.mark_terminal_if_unchanged

    async def refresh_then_guard(
        _db, park_id, new_status, *,
        expected_status, expected_claimed_at, expected_updated_at,
    ):
        # Simulate the concurrent resume re-claim inside the TOCTOU window:
        # same status, fresh claimed_at.
        await _db.execute(
            "UPDATE cc_rate_limit_parks SET claimed_at=? WHERE id=?",
            (datetime.now(UTC).isoformat(), park_id),
        )
        await _db.commit()
        return await real_guard(
            _db, park_id, new_status,
            expected_status=expected_status,
            expected_claimed_at=expected_claimed_at,
            expected_updated_at=expected_updated_at,
        )

    monkeypatch.setattr(parks, "mark_terminal_if_unchanged", refresh_then_guard)

    result = await _check_session_status(db, "camp-sess-1")
    assert result is None, "guard no-op → campaign stays attached, no failure verdict"
    row = await (await db.execute(
        "SELECT status FROM cc_rate_limit_parks WHERE id='rlp-test0001'",
    )).fetchone()
    assert str(row["status"]) == "resuming", "live re-claim must survive the race"


@pytest.mark.asyncio
async def test_over_bound_park_with_stale_claim_abandons(db):
    """A stale claim (crashed attempt, past the 2h reclaim bound) is safe to
    abandon — nothing live can execute it."""
    from genesis.campaigns.runner import _check_session_status

    helper = TestParkAwareSessionStatus()
    await helper._seed_failed_parked_session(db, "resuming")
    await db.execute(
        "UPDATE cc_rate_limit_parks SET created_at='2026-08-01T00:00:00+00:00', "
        "claimed_at='2026-08-01T01:00:00+00:00' WHERE id='rlp-test0001'",
    )
    await db.commit()
    result = await _check_session_status(db, "camp-sess-1")
    assert result is not None and result["success"] is False
    row = await (await db.execute(
        "SELECT status FROM cc_rate_limit_parks WHERE id='rlp-test0001'",
    )).fetchone()
    assert str(row["status"]) == "cancelled"
