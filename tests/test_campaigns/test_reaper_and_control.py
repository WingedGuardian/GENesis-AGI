"""Tests for the pending-session reaper, idempotent capture, orphan
reconciliation, jitter scheduling, and the shared control service."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


async def _make_campaign(db, *, name="reap-camp", cid="reap-camp", state=None):
    from genesis.db.crud import campaigns as crud

    await crud.create_campaign(
        db,
        id=cid,
        name=name,
        strategy_doc_path="/tmp/strategy.md",
        cron_cadence="0 9 * * *",
        created_at=datetime.now(UTC).isoformat(),
        state_json=json.dumps(state or {}),
    )
    return cid


# ── CRUD idempotency primitives ─────────────────────────────────────────────


class TestCrudIdempotency:
    @pytest.mark.anyio
    async def test_complete_run_only_if_pending_claims_once(self, db):
        from genesis.db.crud import campaigns as crud

        cid = await _make_campaign(db)
        await crud.create_run(
            db, id="run-1", campaign_id=cid,
            started_at=datetime.now(UTC).isoformat(),
        )
        first = await crud.complete_run(
            db, "run-1", outcome="success", cost_usd=0.1,
            finished_at=datetime.now(UTC).isoformat(), only_if_pending=True,
        )
        second = await crud.complete_run(
            db, "run-1", outcome="success", cost_usd=0.1,
            finished_at=datetime.now(UTC).isoformat(), only_if_pending=True,
        )
        assert first == 1  # claimed
        assert second == 0  # already completed

    @pytest.mark.anyio
    async def test_increment_totals_is_additive(self, db):
        from genesis.db.crud import campaigns as crud

        cid = await _make_campaign(db)
        await crud.increment_campaign_totals(db, cid, 0.25)
        await crud.increment_campaign_totals(db, cid, 0.25)
        camp = await crud.get_campaign(db, cid)
        assert camp["total_runs"] == 2
        assert abs(camp["total_cost_usd"] - 0.5) < 1e-9

    @pytest.mark.anyio
    async def test_count_runs_by_outcome(self, db):
        from genesis.db.crud import campaigns as crud

        cid = await _make_campaign(db)
        for i, outcome in enumerate(["pending", "success", "skip"]):
            await crud.create_run(
                db, id=f"r{i}", campaign_id=cid,
                started_at=datetime.now(UTC).isoformat(),
            )
            if outcome != "pending":
                await crud.complete_run(
                    db, f"r{i}", outcome=outcome,
                    finished_at=datetime.now(UTC).isoformat(),
                )
        counts = await crud.count_runs_by_outcome(db, cid)
        assert counts == {"pending": 1, "success": 1, "skip": 1}

    @pytest.mark.anyio
    async def test_batched_aggregates_match_per_campaign(self, db):
        from genesis.db.crud import campaigns as crud

        c1 = await _make_campaign(db, name="agg1", cid="agg1")
        c2 = await _make_campaign(db, name="agg2", cid="agg2")
        today = datetime.now(UTC).isoformat()[:10]
        # c1: one success ($0.2), one pending; c2: one success ($0.5)
        await crud.create_run(db, id="a1", campaign_id=c1, started_at=today + "T01:00:00+00:00")
        await crud.complete_run(db, "a1", outcome="success", cost_usd=0.2, finished_at=today)
        await crud.create_run(db, id="a2", campaign_id=c1, started_at=today + "T02:00:00+00:00")
        await crud.create_run(db, id="b1", campaign_id=c2, started_at=today + "T03:00:00+00:00")
        await crud.complete_run(db, "b1", outcome="success", cost_usd=0.5, finished_at=today)

        counts_all = await crud.count_runs_by_outcome_all(db)
        cost_all = await crud.get_daily_cost_all(db, today)
        assert counts_all[c1] == {"success": 1, "pending": 1}
        assert counts_all[c2] == {"success": 1}
        assert abs(cost_all[c1] - 0.2) < 1e-9
        assert abs(cost_all[c2] - 0.5) < 1e-9
        # batched values equal the per-campaign helpers
        assert cost_all[c1] == await crud.get_daily_cost(db, c1, today)
        assert counts_all[c2] == await crud.count_runs_by_outcome(db, c2)

    @pytest.mark.anyio
    async def test_mark_orphan_respects_keep_and_grace(self, db):
        from genesis.db.crud import campaigns as crud

        cid = await _make_campaign(db)
        old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        recent = datetime.now(UTC).isoformat()
        # old orphan (eligible), the kept active run, and a recent run (grace)
        await crud.create_run(db, id="orphan", campaign_id=cid, started_at=old)
        await crud.create_run(db, id="keep", campaign_id=cid, started_at=old)
        await crud.create_run(db, id="recent", campaign_id=cid, started_at=recent)

        grace_cutoff = (datetime.now(UTC) - timedelta(seconds=300)).isoformat()
        n = await crud.mark_orphan_runs_superseded(
            db, cid, keep_run_id="keep",
            older_than=grace_cutoff, finished_at=recent,
        )
        assert n == 1  # only "orphan"
        assert (await crud.get_run(db, "orphan"))["outcome"] == "error"
        assert (await crud.get_run(db, "keep"))["outcome"] == "pending"
        assert (await crud.get_run(db, "recent"))["outcome"] == "pending"


# ── Idempotent capture under concurrency ────────────────────────────────────


class TestCaptureIdempotency:
    @pytest.mark.anyio
    async def test_double_capture_counts_once(self, db):
        from genesis.campaigns.runner import _capture_session_results
        from genesis.db.crud import campaigns as crud

        state = {"_pending_session_id": "sess-x", "_pending_run_id": "run-x"}
        cid = await _make_campaign(db, state=state)
        await crud.create_run(
            db, id="run-x", campaign_id=cid,
            started_at=datetime.now(UTC).isoformat(),
        )
        campaign = await crud.get_campaign(db, cid)
        result = {"success": True, "output_text": '{"summary": "done"}', "cost_usd": 0.3}

        # Two capture paths (cron tick + reaper) for the SAME run.
        await _capture_session_results(db, campaign, dict(state), result, "run-x")
        await _capture_session_results(db, campaign, dict(state), result, "run-x")

        camp = await crud.get_campaign(db, cid)
        assert camp["total_runs"] == 1
        assert abs(camp["total_cost_usd"] - 0.3) < 1e-9
        run = await crud.get_run(db, "run-x")
        assert run["outcome"] == "success"
        # pending markers cleared
        assert "_pending_session_id" not in json.loads(camp["state_json"])

    @pytest.mark.anyio
    async def test_malformed_state_updates_does_not_stall(self, db):
        """LLM emits `state_updates: null` — capture must still complete the run
        and clear markers (not crash after claiming, leaking pending state)."""
        from genesis.campaigns.runner import _capture_session_results
        from genesis.db.crud import campaigns as crud

        state = {"k": 1, "_pending_session_id": "s", "_pending_run_id": "run-bad"}
        cid = await _make_campaign(db, state=state)
        await crud.create_run(
            db, id="run-bad", campaign_id=cid,
            started_at=datetime.now(UTC).isoformat(),
        )
        campaign = await crud.get_campaign(db, cid)
        # state_updates explicitly null + a non-dict variant both must be safe
        result = {"success": True, "output_text": '{"summary":"x","state_updates":null}', "cost_usd": 0.1}

        await _capture_session_results(db, campaign, dict(state), result, "run-bad")

        camp = await crud.get_campaign(db, cid)
        assert (await crud.get_run(db, "run-bad"))["outcome"] == "success"
        assert camp["total_runs"] == 1
        # markers cleared → no permanent stall
        assert "_pending_session_id" not in json.loads(camp["state_json"])


# ── Reaper ──────────────────────────────────────────────────────────────────


class TestReaper:
    @pytest.mark.anyio
    async def test_reap_captures_finished_session(self, db):
        from genesis.campaigns.runner import CampaignRunner
        from genesis.db.crud import campaigns as crud

        state = {"_pending_session_id": "sess-done", "_pending_run_id": "run-done"}
        cid = await _make_campaign(db, state=state)
        await crud.create_run(
            db, id="run-done", campaign_id=cid,
            started_at=datetime.now(UTC).isoformat(),
        )
        runner = CampaignRunner(db=db, session_runner=MagicMock())

        completed = {"success": True, "output_text": '{"summary": "ok"}', "cost_usd": 0.4}
        with patch(
            "genesis.campaigns.runner._check_session_status",
            new=AsyncMock(return_value=completed),
        ):
            await runner._reap_pending_sessions()

        camp = await crud.get_campaign(db, cid)
        assert camp["total_runs"] == 1
        assert (await crud.get_run(db, "run-done"))["outcome"] == "success"

    @pytest.mark.anyio
    async def test_reap_reconciles_orphan_when_no_pending(self, db):
        from genesis.campaigns.runner import CampaignRunner
        from genesis.db.crud import campaigns as crud

        cid = await _make_campaign(db, state={})  # no active pending
        old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        await crud.create_run(db, id="lost", campaign_id=cid, started_at=old)

        runner = CampaignRunner(db=db, session_runner=MagicMock())
        await runner._reap_pending_sessions()

        assert (await crud.get_run(db, "lost"))["outcome"] == "error"

    @pytest.mark.anyio
    async def test_reap_leaves_running_session(self, db):
        from genesis.campaigns.runner import CampaignRunner
        from genesis.db.crud import campaigns as crud

        state = {"_pending_session_id": "sess-run", "_pending_run_id": "run-run"}
        cid = await _make_campaign(db, state=state)
        await crud.create_run(
            db, id="run-run", campaign_id=cid,
            started_at=datetime.now(UTC).isoformat(),
        )
        runner = CampaignRunner(db=db, session_runner=MagicMock())

        with patch(
            "genesis.campaigns.runner._check_session_status",
            new=AsyncMock(return_value=None),  # still running
        ):
            await runner._reap_pending_sessions()

        # Untouched: still pending, no totals bumped.
        assert (await crud.get_run(db, "run-run"))["outcome"] == "pending"
        assert (await crud.get_campaign(db, cid))["total_runs"] == 0


# ── Jitter scheduling ───────────────────────────────────────────────────────


class TestJitterScheduling:
    def test_schedule_sets_jitter_on_trigger(self):
        from genesis.campaigns.runner import CampaignRunner

        runner = CampaignRunner(db=MagicMock(), session_runner=MagicMock())
        runner._scheduler = MagicMock()
        camp = {
            "id": "c1", "name": "c1", "cron_cadence": "0 9 * * *",
            "jitter_seconds": 120,
        }
        runner._schedule_campaign(camp)
        trigger = runner._scheduler.add_job.call_args.args[1]
        assert getattr(trigger, "jitter", None) == 120

    def test_schedule_no_jitter_when_unset(self):
        from genesis.campaigns.runner import CampaignRunner

        runner = CampaignRunner(db=MagicMock(), session_runner=MagicMock())
        runner._scheduler = MagicMock()
        camp = {"id": "c2", "name": "c2", "cron_cadence": "0 9 * * *", "jitter_seconds": None}
        runner._schedule_campaign(camp)
        trigger = runner._scheduler.add_job.call_args.args[1]
        assert getattr(trigger, "jitter", None) is None


# ── Control service ─────────────────────────────────────────────────────────


class TestControl:
    @pytest.mark.anyio
    async def test_update_rejects_bad_model(self, db):
        from genesis.campaigns import control

        await _make_campaign(db, name="ctl1", cid="ctl1")
        res = await control.update_campaign_config(db, None, "ctl1", model="gpt4")
        assert "error" in res

    @pytest.mark.anyio
    async def test_update_rejects_bad_cadence(self, db):
        from genesis.campaigns import control

        await _make_campaign(db, name="ctl2", cid="ctl2")
        res = await control.update_campaign_config(db, None, "ctl2", cron_cadence="not a cron")
        assert "error" in res

    @pytest.mark.anyio
    async def test_update_persists_and_normalizes_jitter_zero(self, db):
        from genesis.campaigns import control
        from genesis.db.crud import campaigns as crud

        await _make_campaign(db, name="ctl3", cid="ctl3")
        res = await control.update_campaign_config(
            db, None, "ctl3", model="opus", effort="high", jitter_seconds=0,
        )
        assert "error" not in res
        camp = await crud.get_campaign(db, "ctl3")
        assert camp["model"] == "opus"
        assert camp["effort"] == "high"
        assert camp["jitter_seconds"] is None  # 0 normalized to NULL

    @pytest.mark.anyio
    async def test_jitter_only_change_hot_reschedules(self, db):
        from genesis.campaigns import control

        await _make_campaign(db, name="ctl4", cid="ctl4")  # status active
        runner = MagicMock()
        runner.remove_campaign = AsyncMock()
        runner.add_campaign = AsyncMock()
        res = await control.update_campaign_config(db, runner, "ctl4", jitter_seconds=90)
        assert "error" not in res
        runner.remove_campaign.assert_awaited_once()
        runner.add_campaign.assert_awaited_once()

    @pytest.mark.anyio
    async def test_pause_resume_roundtrip(self, db):
        from genesis.campaigns import control
        from genesis.db.crud import campaigns as crud

        await _make_campaign(db, name="ctl5", cid="ctl5")
        runner = MagicMock()
        runner.remove_campaign = AsyncMock()
        runner.add_campaign = AsyncMock()

        await control.pause_campaign(db, runner, "ctl5")
        assert (await crud.get_campaign(db, "ctl5"))["status"] == "paused"
        await control.resume_campaign(db, runner, "ctl5")
        assert (await crud.get_campaign(db, "ctl5"))["status"] == "active"

    @pytest.mark.anyio
    async def test_trigger_without_runner_errors(self, db):
        from genesis.campaigns import control

        res = await control.trigger_campaign(db, None, "whatever")
        assert "error" in res
