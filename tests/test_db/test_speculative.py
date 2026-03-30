"""Tests for speculative_claims CRUD."""

import sqlite3

import pytest

from genesis.db.crud import speculative

_COMMON = dict(
    claim="LLMs benefit from chain-of-thought",
    hypothesis_expiry="2026-06-01",
    created_at="2026-01-01T00:00:00",
)


async def test_create_and_get(db):
    rid = await speculative.create(db, id="sc1", **_COMMON)
    assert rid == "sc1"
    row = await speculative.get_by_id(db, "sc1")
    assert row is not None
    assert row["speculative"] == 1


async def test_get_nonexistent(db):
    assert await speculative.get_by_id(db, "nope") is None


async def test_list_active(db):
    await speculative.create(db, id="sc2", **_COMMON)
    rows = await speculative.list_active(db)
    assert any(r["id"] == "sc2" for r in rows)


async def test_add_evidence_increments(db):
    await speculative.create(db, id="sc3", **_COMMON)
    assert await speculative.add_evidence(db, "sc3", memory_id="mem1") is True
    row = await speculative.get_by_id(db, "sc3")
    assert row["evidence_count"] == 1
    assert row["speculative"] == 1  # still speculative with 1 evidence


async def test_add_evidence_confirms_at_three(db):
    await speculative.create(db, id="sc4", **_COMMON)
    for i in range(3):
        await speculative.add_evidence(db, "sc4", memory_id=f"mem{i}")
    row = await speculative.get_by_id(db, "sc4")
    assert row["evidence_count"] == 3
    assert row["speculative"] == 0  # confirmed


async def test_add_evidence_nonexistent(db):
    assert await speculative.add_evidence(db, "nope", memory_id="x") is False


async def test_archive(db):
    await speculative.create(db, id="sc5", **_COMMON)
    assert await speculative.archive(db, "sc5", archived_at="2026-02-01") is True
    row = await speculative.get_by_id(db, "sc5")
    assert row["archived_at"] == "2026-02-01"


async def test_archived_not_in_active(db):
    await speculative.create(db, id="sc6", **_COMMON)
    await speculative.archive(db, "sc6", archived_at="2026-02-01")
    rows = await speculative.list_active(db)
    assert all(r["id"] != "sc6" for r in rows)


async def test_archive_nonexistent(db):
    assert await speculative.archive(db, "nope", archived_at="x") is False


async def test_delete(db):
    await speculative.create(db, id="sc7", **_COMMON)
    assert await speculative.delete(db, "sc7") is True


async def test_delete_nonexistent(db):
    assert await speculative.delete(db, "nope") is False


async def test_duplicate_id_raises(db):
    await speculative.create(db, id="scdup", **_COMMON)
    with pytest.raises(sqlite3.IntegrityError):
        await speculative.create(db, id="scdup", **_COMMON)
