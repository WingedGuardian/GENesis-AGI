"""CRUD tests for entity_adjudications (uses the full in-memory ``db`` fixture,
which exercises the canonical ``create_all_tables`` build path)."""

from __future__ import annotations

import pytest

from genesis.db.crud import entity_adjudications as adj


def test_pair_key_order_independent():
    assert adj.pair_key("e2", "e1") == adj.pair_key("e1", "e2") == "e1|e2"


@pytest.mark.asyncio
async def test_record_and_get_by_pair_order_independent(db):
    await adj.record_verdict(
        db,
        entity_a="e1",
        entity_b="e2",
        verdict="distinct",
        reasoning="different things",
        provider="mechanical",
    )
    # Lookup with the pair reversed must find the same row.
    row = await adj.get_by_pair(db, "e2", "e1")
    assert row is not None
    assert row["verdict"] == "distinct"
    assert row["provider"] == "mechanical"
    assert row["pair_key"] == "e1|e2"


@pytest.mark.asyncio
async def test_get_by_pair_missing_returns_none(db):
    assert await adj.get_by_pair(db, "nope", "nada") is None


@pytest.mark.asyncio
async def test_record_verdict_upsert_overwrites_same_pair(db):
    await adj.record_verdict(db, entity_a="e1", entity_b="e2", verdict="proposed_merge")
    # Re-adjudicate the reversed pair with a new verdict — must overwrite, not dup.
    await adj.record_verdict(
        db, entity_a="e2", entity_b="e1", verdict="distinct", reasoning="second look"
    )
    keys = await adj.all_pair_keys(db)
    assert keys == {"e1|e2"}  # deduped to one row
    row = await adj.get_by_pair(db, "e1", "e2")
    assert row["verdict"] == "distinct"
    assert row["reasoning"] == "second look"


@pytest.mark.asyncio
async def test_all_pair_keys(db):
    await adj.record_verdict(db, entity_a="a", entity_b="b", verdict="distinct")
    await adj.record_verdict(db, entity_a="c", entity_b="d", verdict="merge")
    assert await adj.all_pair_keys(db) == {"a|b", "c|d"}


@pytest.mark.asyncio
async def test_list_proposed_merges_only_proposed(db):
    await adj.record_verdict(
        db, entity_a="a", entity_b="b", verdict="proposed_merge", loser_id="b", survivor_id="a"
    )
    await adj.record_verdict(db, entity_a="c", entity_b="d", verdict="distinct")
    await adj.record_verdict(db, entity_a="e", entity_b="f", verdict="merge")
    proposed = await adj.list_proposed_merges(db)
    assert [r["pair_key"] for r in proposed] == ["a|b"]
    assert proposed[0]["survivor_id"] == "a"


@pytest.mark.asyncio
async def test_mark_applied_promotes_proposed_to_merge(db):
    await adj.record_verdict(db, entity_a="a", entity_b="b", verdict="proposed_merge")
    await adj.mark_applied(db, pair_key="a|b", loser_id="b", survivor_id="a")
    row = await adj.get_by_pair(db, "a", "b")
    assert row["verdict"] == "merge"
    assert row["loser_id"] == "b"
    assert row["survivor_id"] == "a"
    assert row["applied_at"] is not None
    # No longer in the propose backlog.
    assert await adj.list_proposed_merges(db) == []


@pytest.mark.asyncio
async def test_mark_stale(db):
    await adj.record_verdict(db, entity_a="a", entity_b="b", verdict="proposed_merge")
    await adj.mark_stale(db, pair_key="a|b")
    row = await adj.get_by_pair(db, "a", "b")
    assert row["verdict"] == "stale"
    assert await adj.list_proposed_merges(db) == []
