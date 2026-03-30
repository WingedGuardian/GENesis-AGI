"""Tests for IdeaBank."""

from __future__ import annotations

import pytest

from genesis.modules.content_pipeline.idea_bank import IdeaBank


@pytest.mark.asyncio
async def test_capture_creates_idea(db):
    bank = IdeaBank(db)
    idea = await bank.capture("manual", "Test idea content", tags=["ai", "blog"])
    assert idea.id
    assert idea.source == "manual"
    assert idea.content == "Test idea content"
    assert idea.tags == ["ai", "blog"]
    assert idea.status == "new"
    assert idea.score == 0.0


@pytest.mark.asyncio
async def test_capture_with_platform_target(db):
    bank = IdeaBank(db)
    idea = await bank.capture("recon", "Recon finding", platform_target="linkedin")
    assert idea.platform_target == "linkedin"


@pytest.mark.asyncio
async def test_rank_returns_new_sorted_by_score(db):
    bank = IdeaBank(db)
    i1 = await bank.capture("manual", "Low score")
    i2 = await bank.capture("manual", "High score")
    i3 = await bank.capture("manual", "Mid score")
    await bank.update_score(i1.id, 1.0)
    await bank.update_score(i2.id, 10.0)
    await bank.update_score(i3.id, 5.0)

    ranked = await bank.rank(limit=10)
    assert len(ranked) == 3
    assert ranked[0].id == i2.id
    assert ranked[1].id == i3.id
    assert ranked[2].id == i1.id


@pytest.mark.asyncio
async def test_rank_excludes_non_new(db):
    bank = IdeaBank(db)
    i1 = await bank.capture("manual", "New idea")
    i2 = await bank.capture("manual", "Planned idea")
    await bank.update_status(i2.id, "planned")

    ranked = await bank.rank()
    assert len(ranked) == 1
    assert ranked[0].id == i1.id


@pytest.mark.asyncio
async def test_list_by_status(db):
    bank = IdeaBank(db)
    await bank.capture("manual", "Idea 1")
    i2 = await bank.capture("manual", "Idea 2")
    await bank.update_status(i2.id, "drafted")

    new_ideas = await bank.list_by_status("new")
    assert len(new_ideas) == 1
    drafted = await bank.list_by_status("drafted")
    assert len(drafted) == 1
    assert drafted[0].id == i2.id


@pytest.mark.asyncio
async def test_update_status_returns_false_for_missing(db):
    bank = IdeaBank(db)
    result = await bank.update_status("nonexistent", "archived")
    assert result is False


@pytest.mark.asyncio
async def test_update_score_returns_false_for_missing(db):
    bank = IdeaBank(db)
    result = await bank.update_score("nonexistent", 5.0)
    assert result is False
