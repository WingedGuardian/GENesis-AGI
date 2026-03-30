"""Tests for ContentPlanner."""

from __future__ import annotations

import pytest

from genesis.modules.content_pipeline.planner import ContentPlanner


@pytest.mark.asyncio
async def test_create_plan(db):
    planner = ContentPlanner(db)
    ideas = [
        {"idea_id": "id1", "platform": "linkedin", "scheduled_date": "2026-04-01"},
        {"idea_id": "id2", "scheduled_date": "2026-04-03"},
    ]
    plan = await planner.create_plan(ideas, "2026-04-01", "2026-04-07")
    assert plan.id
    assert plan.status == "draft"
    assert len(plan.items) == 2
    assert plan.items[0].platform == "linkedin"
    assert plan.items[1].platform == "generic"


@pytest.mark.asyncio
async def test_create_plan_with_default_platforms(db):
    planner = ContentPlanner(db)
    ideas = [{"idea_id": "id1"}]
    plan = await planner.create_plan(
        ideas, "2026-04-01", "2026-04-07", platforms=["twitter"],
    )
    assert plan.items[0].platform == "twitter"


@pytest.mark.asyncio
async def test_get_plan(db):
    planner = ContentPlanner(db)
    plan = await planner.create_plan(
        [{"idea_id": "x"}], "2026-04-01", "2026-04-07",
    )
    fetched = await planner.get_plan(plan.id)
    assert fetched is not None
    assert fetched.id == plan.id
    assert len(fetched.items) == 1


@pytest.mark.asyncio
async def test_get_plan_missing(db):
    planner = ContentPlanner(db)
    assert await planner.get_plan("nonexistent") is None


@pytest.mark.asyncio
async def test_list_plans_all(db):
    planner = ContentPlanner(db)
    await planner.create_plan([{"idea_id": "a"}], "2026-04-01", "2026-04-07")
    await planner.create_plan([{"idea_id": "b"}], "2026-04-08", "2026-04-14")
    plans = await planner.list_plans()
    assert len(plans) == 2


@pytest.mark.asyncio
async def test_list_plans_by_status(db):
    planner = ContentPlanner(db)
    p1 = await planner.create_plan([{"idea_id": "a"}], "2026-04-01", "2026-04-07")
    await planner.create_plan([{"idea_id": "b"}], "2026-04-08", "2026-04-14")
    await planner.update_plan_status(p1.id, "approved")

    drafts = await planner.list_plans(status="draft")
    assert len(drafts) == 1
    approved = await planner.list_plans(status="approved")
    assert len(approved) == 1


@pytest.mark.asyncio
async def test_update_plan_status_missing(db):
    planner = ContentPlanner(db)
    assert await planner.update_plan_status("nope", "approved") is False
