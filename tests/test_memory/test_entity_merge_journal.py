"""Reversibility journal for entity merges (PR-1).

merge_entity physically DELETEs the loser's mention/link rows, so an applied
merge is irreversible without a pre-delete snapshot. These tests lock that the
snapshot is written inside the merge, and that the journal is age-pruned.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest
import pytest_asyncio

from genesis.db.crud import entities as ec
from genesis.db.schema._tables import TABLES


@pytest_asyncio.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    for table in (
        "entities",
        "entity_mentions",
        "entity_links",
        "entity_merge_journal",
    ):
        await conn.execute(TABLES[table])
    await conn.commit()
    yield conn
    await conn.close()


async def _mk(db, name, etype="product"):
    return await ec.create_entity(db, name=name, norm_name=name.lower(), entity_type=etype)


@pytest.mark.asyncio
async def test_merge_journal_captures_loser_before_delete(db):
    loser = await _mk(db, "widget")
    survivor = await _mk(db, "widgets")
    other = await _mk(db, "gadget")
    await ec.upsert_mention(db, memory_id="m1", entity_id=loser, provenance="EXTRACTED")
    await ec.upsert_link(
        db, source_id=loser, target_id=other, link_type="part_of", provenance="EXTRACTED"
    )

    await ec.merge_entity(db, loser_id=loser, survivor_id=survivor)

    cur = await db.execute(
        "SELECT survivor_id, loser_name, loser_norm, loser_type, mentions_json, links_json "
        "FROM entity_merge_journal WHERE loser_id = ?",
        (loser,),
    )
    row = await cur.fetchone()
    assert row is not None, "merge must write a reversibility snapshot"
    assert row["survivor_id"] == survivor
    assert row["loser_name"] == "widget" and row["loser_norm"] == "widget"
    mentions = json.loads(row["mentions_json"])
    links = json.loads(row["links_json"])
    # The loser's mention + link are captured BEFORE merge_entity deletes them.
    assert any(m["memory_id"] == "m1" for m in mentions)
    assert any(link["target_id"] == other for link in links)


@pytest.mark.asyncio
async def test_merge_journal_empty_loser(db):
    """A loser with no mentions/links still gets a journal row (empty lists)."""
    loser = await _mk(db, "alpha")
    survivor = await _mk(db, "alphaa")
    await ec.merge_entity(db, loser_id=loser, survivor_id=survivor)
    cur = await db.execute(
        "SELECT mentions_json, links_json FROM entity_merge_journal WHERE loser_id = ?",
        (loser,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert json.loads(row["mentions_json"]) == []
    assert json.loads(row["links_json"]) == []


@pytest.mark.asyncio
async def test_prune_merge_journal_by_age(db):
    await db.execute(
        "INSERT INTO entity_merge_journal (id, loser_id, survivor_id, merged_at) "
        "VALUES (?, ?, ?, ?)",
        ("old", "L1", "S1", "2020-01-01T00:00:00+00:00"),
    )
    await db.execute(
        "INSERT INTO entity_merge_journal (id, loser_id, survivor_id, merged_at) "
        "VALUES (?, ?, ?, ?)",
        ("recent", "L2", "S2", "2026-08-20T00:00:00+00:00"),
    )
    await db.commit()

    deleted = await ec.prune_merge_journal(db, older_than_days=180, now="2026-08-25T12:00:00+00:00")

    assert deleted == 1
    cur = await db.execute("SELECT id FROM entity_merge_journal")
    remaining = {r[0] for r in await cur.fetchall()}
    assert remaining == {"recent"}


@pytest.mark.asyncio
async def test_prune_merge_journal_noops_when_table_absent():
    """Prune no-ops (returns 0) before migration 0086 lands — table-existence guard."""
    conn = await aiosqlite.connect(":memory:")
    try:
        deleted = await ec.prune_merge_journal(
            conn, older_than_days=180, now="2026-08-25T12:00:00+00:00"
        )
        assert deleted == 0
    finally:
        await conn.close()
