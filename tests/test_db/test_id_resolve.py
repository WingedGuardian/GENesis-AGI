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


async def test_tagged_and_padded_full_ids_normalize_on_passthrough():
    """A full-length id handed in with an ``id:`` tag, surrounding whitespace, or
    uppercase must pass through as the NORMALIZED id — so the caller's exact lookup
    still hits the row. Regression: passthrough previously echoed the raw string,
    so ``id:<32hex>`` reached exact-match lookups un-normalized and missed."""
    async with aiosqlite.connect(":memory:") as db:
        await _mk(db, "abcd1234" + "0" * 24)
        for raw in ("id:" + "f" * 32, "  " + "f" * 32 + "  ", "F" * 32):
            matches, outcome = await _resolve(db, raw)
            assert outcome == _id_resolve.PASSTHROUGH, raw
            assert matches == ["f" * 32], raw


async def test_db_error_fail_open_normalizes():
    """The DB-error fail-open branch must ALSO return the normalized id (parity with
    the prefix-shape passthrough and the memory resolver) — a short hex prefix with
    an ``id:`` tag reaches the LIKE query, which raises with no table, then fails
    open to the normalized ``mid``, not the raw."""
    async with aiosqlite.connect(":memory:") as db:
        # No table `t` → the LIKE query raises → fail open. ``id:ABCD`` is a valid
        # short prefix shape, so it reaches the query rather than the length gate.
        matches, outcome = await _id_resolve.resolve_unique_prefix(
            db, table="t", id_column="id", raw_id="id:ABCD"
        )
    assert outcome == _id_resolve.PASSTHROUGH
    assert matches == ["abcd"]


async def test_bad_identifier_rejected():
    """table/id_column are developer literals — a non-identifier must raise, not
    reach string interpolation."""
    async with aiosqlite.connect(":memory:") as db:
        with pytest.raises((ValueError, AssertionError)):
            await _id_resolve.resolve_unique_prefix(
                db, table="t; DROP TABLE t", id_column="id", raw_id="abcd1234"
            )
