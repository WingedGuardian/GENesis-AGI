"""Tests for campaign CRUD operations."""

from __future__ import annotations

import json


async def test_create_campaign(db):
    from genesis.db.crud import campaigns as crud

    campaign_id = await crud.create_campaign(
        db,
        id="c1",
        name="test-campaign",
        strategy_doc_path="/tmp/strategy.md",
        cron_cadence="0 */8 * * *",
        created_at="2026-06-07T00:00:00Z",
    )
    assert campaign_id == "c1"


async def test_create_campaign_defaults_to_interact_profile(db):
    """Default session_profile must not silently grant recon (idx 37 follow-through).

    The 'research' profile now loads genesis-recon (read+write); campaigns run
    unattended, so they must not inherit that surface by default. The runner's
    own fallback is already 'interact'.
    """
    from genesis.db.crud import campaigns as crud

    await crud.create_campaign(
        db,
        id="c-default",
        name="default-profile",
        strategy_doc_path="/tmp/strategy.md",
        cron_cadence="0 */8 * * *",
        created_at="2026-06-07T00:00:00Z",
    )
    row = await crud.get_campaign_by_name(db, "default-profile")
    assert row["session_profile"] == "interact"


async def test_get_campaign_by_name(db):
    from genesis.db.crud import campaigns as crud

    await crud.create_campaign(
        db,
        id="c2",
        name="weekly-digest",
        strategy_doc_path="/tmp/strat.md",
        cron_cadence="0 */8 * * *",
        created_at="2026-06-07T00:00:00Z",
    )
    row = await crud.get_campaign_by_name(db, "weekly-digest")
    assert row is not None
    assert row["id"] == "c2"
    assert row["status"] == "active"
    assert row["model"] == "sonnet"
    assert row["effort"] == "medium"


async def test_get_campaign_by_name_missing(db):
    from genesis.db.crud import campaigns as crud

    assert await crud.get_campaign_by_name(db, "nonexistent") is None


async def test_list_campaigns_all(db):
    from genesis.db.crud import campaigns as crud

    await crud.create_campaign(
        db, id="c3", name="a", strategy_doc_path="/a",
        cron_cadence="* * * * *", created_at="2026-06-07T00:00:00Z",
    )
    await crud.create_campaign(
        db, id="c4", name="b", strategy_doc_path="/b",
        cron_cadence="* * * * *", created_at="2026-06-07T00:00:00Z",
    )
    rows = await crud.list_campaigns(db)
    assert len(rows) == 2


async def test_list_campaigns_by_status(db):
    from genesis.db.crud import campaigns as crud

    await crud.create_campaign(
        db, id="c5", name="active-one", strategy_doc_path="/a",
        cron_cadence="* * * * *", created_at="2026-06-07T00:00:00Z",
    )
    await crud.create_campaign(
        db, id="c6", name="paused-one", strategy_doc_path="/b",
        cron_cadence="* * * * *", created_at="2026-06-07T00:00:00Z",
        status="paused",
    )
    active = await crud.list_campaigns(db, status_filter="active")
    assert len(active) == 1
    assert active[0]["name"] == "active-one"


async def test_update_campaign_state(db):
    from genesis.db.crud import campaigns as crud

    await crud.create_campaign(
        db, id="c7", name="stateful", strategy_doc_path="/s",
        cron_cadence="* * * * *", created_at="2026-06-07T00:00:00Z",
    )
    new_state = {"posts_today": 2, "last_channel": "showcase"}
    await crud.update_campaign_state(db, "c7", json.dumps(new_state))

    row = await crud.get_campaign_by_name(db, "stateful")
    assert json.loads(row["state_json"]) == new_state


async def test_update_campaign_status(db):
    from genesis.db.crud import campaigns as crud

    await crud.create_campaign(
        db, id="c8", name="pausable", strategy_doc_path="/p",
        cron_cadence="* * * * *", created_at="2026-06-07T00:00:00Z",
    )
    await crud.update_campaign(
        db, "c8",
        status="paused",
        paused_at="2026-06-07T01:00:00Z",
    )
    row = await crud.get_campaign_by_name(db, "pausable")
    assert row["status"] == "paused"
    assert row["paused_at"] == "2026-06-07T01:00:00Z"


async def test_create_run(db):
    from genesis.db.crud import campaigns as crud

    await crud.create_campaign(
        db, id="c9", name="runner", strategy_doc_path="/r",
        cron_cadence="* * * * *", created_at="2026-06-07T00:00:00Z",
    )
    run_id = await crud.create_run(
        db,
        id="r1",
        campaign_id="c9",
        started_at="2026-06-07T00:30:00Z",
        trigger_type="scheduled",
    )
    assert run_id == "r1"


async def test_list_runs(db):
    from genesis.db.crud import campaigns as crud

    await crud.create_campaign(
        db, id="c10", name="with-runs", strategy_doc_path="/wr",
        cron_cadence="* * * * *", created_at="2026-06-07T00:00:00Z",
    )
    await crud.create_run(
        db, id="r2", campaign_id="c10",
        started_at="2026-06-07T00:30:00Z", trigger_type="scheduled",
    )
    await crud.create_run(
        db, id="r3", campaign_id="c10",
        started_at="2026-06-07T08:30:00Z", trigger_type="manual",
    )
    runs = await crud.list_runs(db, "c10", limit=5)
    assert len(runs) == 2
    # Most recent first
    assert runs[0]["id"] == "r3"


async def test_complete_run(db):
    from genesis.db.crud import campaigns as crud

    await crud.create_campaign(
        db, id="c11", name="completable", strategy_doc_path="/c",
        cron_cadence="* * * * *", created_at="2026-06-07T00:00:00Z",
    )
    await crud.create_run(
        db, id="r4", campaign_id="c11",
        started_at="2026-06-07T00:30:00Z", trigger_type="scheduled",
    )
    await crud.complete_run(
        db, "r4",
        outcome="success",
        summary="Posted to Discord",
        cost_usd=0.05,
        session_id="sess-abc",
        finished_at="2026-06-07T00:35:00Z",
    )
    runs = await crud.list_runs(db, "c11")
    assert runs[0]["outcome"] == "success"
    assert runs[0]["summary"] == "Posted to Discord"
    assert runs[0]["cost_usd"] == 0.05


async def test_get_daily_cost(db):
    from genesis.db.crud import campaigns as crud

    await crud.create_campaign(
        db, id="c12", name="costly", strategy_doc_path="/co",
        cron_cadence="* * * * *", created_at="2026-06-07T00:00:00Z",
    )
    await crud.create_run(
        db, id="r5", campaign_id="c12",
        started_at="2026-06-07T00:30:00Z", trigger_type="scheduled",
    )
    await crud.complete_run(
        db, "r5", outcome="success", cost_usd=0.10,
        finished_at="2026-06-07T00:35:00Z",
    )
    await crud.create_run(
        db, id="r6", campaign_id="c12",
        started_at="2026-06-07T08:30:00Z", trigger_type="scheduled",
    )
    await crud.complete_run(
        db, "r6", outcome="success", cost_usd=0.20,
        finished_at="2026-06-07T08:35:00Z",
    )
    cost = await crud.get_daily_cost(db, "c12", "2026-06-07")
    assert abs(cost - 0.30) < 0.001
