"""CRUD tests for entity_adjudications (uses the full in-memory ``db`` fixture,
which exercises the canonical ``create_all_tables`` build path)."""

from __future__ import annotations

import pytest

from genesis.db.crud import entity_adjudications as adj
from genesis.security.immunity_shadow import DispatchGateRefused


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
async def test_settled_pair_keys_excludes_stale(db):
    await adj.record_verdict(db, entity_a="a", entity_b="b", verdict="distinct")
    await adj.record_verdict(db, entity_a="c", entity_b="d", verdict="merge")
    await adj.record_verdict(db, entity_a="e", entity_b="f", verdict="proposed_merge")
    await adj.record_verdict(db, entity_a="g", entity_b="h", verdict="stale")
    # stale is excluded so the sweep can rediscover it; the rest are settled.
    assert await adj.settled_pair_keys(db) == {"a|b", "c|d", "e|f"}
    assert await adj.all_pair_keys(db) == {"a|b", "c|d", "e|f", "g|h"}


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
    assert await adj.mark_stale(db, pair_key="a|b") is True
    row = await adj.get_by_pair(db, "a", "b")
    assert row["verdict"] == "stale"
    assert await adj.list_proposed_merges(db) == []


@pytest.mark.asyncio
async def test_mark_stale_noop_on_already_merged(db):
    """P2#4: mark_stale is conditional on verdict='proposed_merge'. Once a concurrent
    winner has flipped the row to 'merge', a losing applier's stale write must be a
    no-op (returns False) — never clobber the applied merge back to stale."""
    await adj.record_verdict(
        db, entity_a="a", entity_b="b", verdict="proposed_merge", loser_id="b", survivor_id="a"
    )
    await adj.approve(db, pair_key="a|b", approved_by="jay")
    # A concurrent winner already applied: proposed_merge → merge.
    assert (
        await adj.claim_approved_for_apply(db, pair_key="a|b", loser_id="b", survivor_id="a")
        is True
    )
    # The loser now tries to mark it stale — must NOT corrupt the applied merge.
    assert await adj.mark_stale(db, pair_key="a|b") is False
    assert (await adj.get_by_pair(db, "a", "b"))["verdict"] == "merge"


@pytest.mark.asyncio
async def test_readjudication_direction_flip_clears_approval(db):
    """P1#1: a re-adjudication that FLIPS survivor/loser (same pair, opposite
    direction) must CLEAR the human approval. The apply path's staleness check compares
    {survivor_id, loser_id} as an order-INDEPENDENT set, so a flipped direction passes
    staleness and would apply the human's old approval the WRONG way — tombstoning the
    entity they chose to keep (Codex P1, #1477)."""
    await adj.record_verdict(
        db,
        entity_a="e1",
        entity_b="e2",
        verdict="proposed_merge",
        loser_id="e2",
        survivor_id="e1",
        norm_a="n1",
        norm_b="n2",
    )
    await adj.approve(db, pair_key=adj.pair_key("e1", "e2"), approved_by="jay")
    assert (await adj.get_by_pair(db, "e1", "e2"))["approved_at"] is not None
    # Re-judge flips the direction (survivor/loser swapped).
    await adj.record_verdict(
        db,
        entity_a="e1",
        entity_b="e2",
        verdict="proposed_merge",
        loser_id="e1",
        survivor_id="e2",
        norm_a="n1",
        norm_b="n2",
        provider="strong-model",
    )
    row = await adj.get_by_pair(db, "e1", "e2")
    assert row["survivor_id"] == "e2" and row["loser_id"] == "e1"  # the re-judge landed
    assert row["approved_at"] is None and row["approved_by"] is None  # approval cleared


@pytest.mark.asyncio
async def test_readjudication_norm_change_clears_approval(db):
    """P1#1: a re-adjudication that changes a norm snapshot the apply path reads
    (identity drift) clears approval — the human approved a merge under norms they saw."""
    await adj.record_verdict(
        db,
        entity_a="e1",
        entity_b="e2",
        verdict="proposed_merge",
        loser_id="e2",
        survivor_id="e1",
        norm_a="n1",
        norm_b="n2",
    )
    await adj.approve(db, pair_key=adj.pair_key("e1", "e2"), approved_by="jay")
    await adj.record_verdict(
        db,
        entity_a="e1",
        entity_b="e2",
        verdict="proposed_merge",
        loser_id="e2",
        survivor_id="e1",
        norm_a="n1-DRIFTED",
        norm_b="n2",
    )
    assert (await adj.get_by_pair(db, "e1", "e2"))["approved_at"] is None


@pytest.mark.asyncio
async def test_readjudication_same_decision_preserves_approval(db):
    """P1#1 guard against over-broadening the field set: a re-judge that reaches the
    SAME decision (only provider/reasoning changed) KEEPS approval — the CRUD-layer
    mirror of test_reapprove_not_clobbered_by_readjudication."""
    await adj.record_verdict(
        db,
        entity_a="e1",
        entity_b="e2",
        verdict="proposed_merge",
        loser_id="e2",
        survivor_id="e1",
        norm_a="n1",
        norm_b="n2",
    )
    await adj.approve(db, pair_key=adj.pair_key("e1", "e2"), approved_by="jay")
    await adj.record_verdict(
        db,
        entity_a="e1",
        entity_b="e2",
        verdict="proposed_merge",
        loser_id="e2",
        survivor_id="e1",
        norm_a="n1",
        norm_b="n2",
        provider="strong-model",
        reasoning="re-confirmed",
    )
    row = await adj.get_by_pair(db, "e1", "e2")
    assert row["approved_at"] is not None and row["approved_by"] == "jay"


@pytest.mark.asyncio
async def test_approve_reject_refuse_dispatched_at_crud_layer(db, monkeypatch):
    """P1#2 CRITICAL: the human-only guard lives in the CRUD functions, not only the
    MCP wrapper — so a Bash-capable dispatched session that imports adj_crud.approve/
    reject directly (its subprocess inherits GENESIS_CC_SESSION=1) is refused too. A
    foreground/supervised session passes (#1477)."""
    await adj.record_verdict(
        db, entity_a="e1", entity_b="e2", verdict="proposed_merge", loser_id="e2", survivor_id="e1"
    )
    pk = adj.pair_key("e1", "e2")
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    monkeypatch.delenv("GENESIS_SESSION_SUPERVISED", raising=False)
    with pytest.raises(DispatchGateRefused):
        await adj.approve(db, pair_key=pk, approved_by="sentinel")
    with pytest.raises(DispatchGateRefused):
        await adj.reject(db, pair_key=pk, reason="x")
    # Nothing moved: still an un-approved proposed_merge.
    row = await adj.get_by_pair(db, "e1", "e2")
    assert row["verdict"] == "proposed_merge" and row["approved_at"] is None
    # Supervised foreground session passes.
    monkeypatch.setenv("GENESIS_SESSION_SUPERVISED", "1")
    assert await adj.approve(db, pair_key=pk, approved_by="jay") is True
