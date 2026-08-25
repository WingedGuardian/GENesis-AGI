"""Entity-adjudication MCP review surface (PR-2): list / approve / reject / apply.

Wires the human-in-the-loop half of the entity-merge gate. Proves the full path
through the MCP tools: a proposed merge is applied ONLY after approve, reject
records 'distinct', and list enriches both entities.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import aiosqlite
import pytest

from genesis.db.crud import entities as entities_crud
from genesis.db.crud import entity_adjudications as adj_crud
from genesis.mcp.memory_mcp import mcp


async def _tools():
    return await mcp.get_tools()


async def _seed_proposal(db, name_a, norm_a, name_b, norm_b):
    a = await entities_crud.create_entity(db, name=name_a, norm_name=norm_a, entity_type="concept")
    b = await entities_crud.create_entity(db, name=name_b, norm_name=norm_b, entity_type="concept")
    # b will be the loser (a seeded with a mention so a wins survivor selection)
    await entities_crud.upsert_mention(db, memory_id="m1", entity_id=a, provenance="EXTRACTED")
    await adj_crud.record_verdict(
        db,
        entity_a=a,
        entity_b=b,
        verdict="proposed_merge",
        loser_id=b,
        survivor_id=a,
        norm_a=norm_a,
        norm_b=norm_b,
        provider="small-free",
        reasoning="same concept, formatting variant",
    )
    return a, b, adj_crud.pair_key(a, b)


async def _with_db(fn):
    import genesis.mcp.memory_mcp as mod

    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        from genesis.db.schema import create_all_tables

        await create_all_tables(db)
        await db.commit()
        old = (mod._store, mod._db, mod._retriever)
        try:
            mod._store, mod._db, mod._retriever = MagicMock(), db, MagicMock()
            return await fn(db)
        finally:
            mod._store, mod._db, mod._retriever = old


@pytest.mark.asyncio
async def test_list_enriches_proposals():
    async def body(db):
        a, b, _ = await _seed_proposal(db, "Codex P2", "codex p2", "Codex-P2", "codex-p2")
        tools = await _tools()
        rows = await tools["entity_adjudication_list"].fn(status="proposed", limit=10)
        assert len(rows) == 1
        r = rows[0]
        assert r["norm_a"] == "codex p2" and r["norm_b"] == "codex-p2"
        assert r["reasoning"] and r["provider"] == "small-free"
        assert r["approved_at"] is None
        assert r["entity_a"]["name"] in ("Codex P2", "Codex-P2")
        assert r["entity_b"]["type"] == "concept"

    await _with_db(body)


@pytest.mark.asyncio
async def test_approve_then_apply_merges():
    async def body(db):
        a, b, pk = await _seed_proposal(db, "widget", "widget", "widgets", "widgets")
        tools = await _tools()
        # apply before approval → nothing merges (gate)
        pre = await tools["entity_adjudication_apply"].fn(budget=5)
        assert pre["merged"] == 0
        # approve, then apply
        appr = await tools["entity_adjudication_approve"].fn(pair_key=pk, approved_by="jay")
        assert appr["approved"] is True
        post = await tools["entity_adjudication_apply"].fn(budget=5)
        assert post["merged"] == 1
        # loser tombstoned
        cur = await db.execute("SELECT status, merged_into FROM entities WHERE entity_id=?", (b,))
        row = await cur.fetchone()
        assert row["status"] == "merged" and row["merged_into"] == a
        # journal snapshot written
        jr = await (
            await db.execute("SELECT loser_id FROM entity_merge_journal WHERE loser_id=?", (b,))
        ).fetchone()
        assert jr is not None

    await _with_db(body)


@pytest.mark.asyncio
async def test_list_status_branches():
    """status filter: 'proposed' vs 'approved' vs 'all' vs an invalid value."""

    async def body(db):
        a, b, pk = await _seed_proposal(db, "alpha", "alpha", "alpha-x", "alpha-x")
        tools = await _tools()
        # unapproved → in 'proposed' and 'all', NOT in 'approved'
        assert len(await tools["entity_adjudication_list"].fn(status="proposed")) == 1
        assert await tools["entity_adjudication_list"].fn(status="approved") == []
        assert len(await tools["entity_adjudication_list"].fn(status="all")) == 1
        # approve → moves to 'approved', leaves 'proposed'
        await tools["entity_adjudication_approve"].fn(pair_key=pk, approved_by="jay")
        assert await tools["entity_adjudication_list"].fn(status="proposed") == []
        assert len(await tools["entity_adjudication_list"].fn(status="approved")) == 1
        assert len(await tools["entity_adjudication_list"].fn(status="all")) == 1
        # an unknown status is rejected loudly (fail-closed), not silently empty
        with pytest.raises(ValueError):
            await tools["entity_adjudication_list"].fn(status="bogus")

    await _with_db(body)


@pytest.mark.asyncio
async def test_reject_records_distinct_not_applied():
    async def body(db):
        a, b, pk = await _seed_proposal(db, "nas", "nas", "nas tier-2", "nas tier-2")
        tools = await _tools()
        rej = await tools["entity_adjudication_reject"].fn(
            pair_key=pk, reason="concept vs instance"
        )
        assert rej["rejected"] is True
        row = await adj_crud.get_by_pair(db, a, b)
        assert row["verdict"] == "distinct" and row["approved_at"] is None
        # not in the proposed list anymore, and apply merges nothing
        rows = await tools["entity_adjudication_list"].fn(status="proposed")
        assert rows == []
        counts = await tools["entity_adjudication_apply"].fn(budget=5)
        assert counts["merged"] == 0

    await _with_db(body)
