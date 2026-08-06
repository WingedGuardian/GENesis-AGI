"""Tests for the shared unique-prefix id resolver (crud/_id_resolve.py).

Mirrors the memory_expand / session_charter resolvers: a short hex prefix
resolves to a unique full id; ambiguous prefixes are never guessed; full-length
and non-hex ids pass through untouched; DB errors fail open.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import _id_resolve

pytestmark = pytest.mark.asyncio


async def _mk(db: aiosqlite.Connection, *ids: str) -> None:
    await db.execute("CREATE TABLE t (id TEXT PRIMARY KEY)")
    for i in ids:
        await db.execute("INSERT INTO t (id) VALUES (?)", (i,))
    await db.commit()


async def _resolve(db, raw):
    return await _id_resolve.resolve_unique_prefix(db, table="t", id_column="id", raw_id=raw)


async def test_unique_prefix_resolves():
    async with aiosqlite.connect(":memory:") as db:
        full = "abcd1234" + "0" * 24
        await _mk(db, full, "9999" + "f" * 28)
        matches, outcome = await _resolve(db, "abcd1234")
    assert outcome == _id_resolve.RESOLVED
    assert matches == [full]


async def test_ambiguous_prefix_reports_candidates():
    async with aiosqlite.connect(":memory:") as db:
        a, b = "dead0001" + "a" * 24, "dead0001" + "b" * 24
        await _mk(db, a, b)
        matches, outcome = await _resolve(db, "dead0001")
    assert outcome == _id_resolve.AMBIGUOUS
    assert set(matches) >= {a, b}  # candidates surfaced for the error message


async def test_no_match_reports_not_found():
    async with aiosqlite.connect(":memory:") as db:
        await _mk(db, "abcd1234" + "0" * 24)
        matches, outcome = await _resolve(db, "beef")
    assert outcome == _id_resolve.NOT_FOUND
    assert matches == []


async def test_full_length_id_passes_through():
    async with aiosqlite.connect(":memory:") as db:
        await _mk(db, "abcd1234" + "0" * 24)
        # A 32-char string is treated as a full id — never LIKE-matched.
        matches, outcome = await _resolve(db, "f" * 32)
    assert outcome == _id_resolve.PASSTHROUGH
    assert matches == ["f" * 32]


async def test_non_hex_id_passes_through():
    async with aiosqlite.connect(":memory:") as db:
        await _mk(db, "abcd1234" + "0" * 24)
        matches, outcome = await _resolve(db, "not-a-hex-id!")
    assert outcome == _id_resolve.PASSTHROUGH
    assert matches == ["not-a-hex-id!"]


async def test_short_prefix_below_min_len_passes_through():
    async with aiosqlite.connect(":memory:") as db:
        await _mk(db, "abcd1234" + "0" * 24)
        # < min_len (4) hex chars is too short to risk a LIKE — pass through.
        matches, outcome = await _resolve(db, "ab")
    assert outcome == _id_resolve.PASSTHROUGH
    assert matches == ["ab"]


async def test_db_error_fails_open():
    async with aiosqlite.connect(":memory:") as db:
        # No table `t` created → the LIKE query raises → fail open to passthrough.
        matches, outcome = await _id_resolve.resolve_unique_prefix(
            db, table="t", id_column="id", raw_id="abcd1234"
        )
    assert outcome == _id_resolve.PASSTHROUGH
    assert matches == ["abcd1234"]


async def test_bad_identifier_rejected():
    """table/id_column are developer literals — a non-identifier must raise, not
    reach string interpolation."""
    async with aiosqlite.connect(":memory:") as db:
        with pytest.raises((ValueError, AssertionError)):
            await _id_resolve.resolve_unique_prefix(
                db, table="t; DROP TABLE t", id_column="id", raw_id="abcd1234"
            )
