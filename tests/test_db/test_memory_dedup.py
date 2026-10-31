"""Tests for memory dedup: find_exact_duplicate CRUD function."""

import sqlite3

import pytest

from genesis.db.crud import memory

# FTS5 may not be available in in-memory SQLite.
_fts5_available = True
try:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE _fts5_test USING fts5(x)")
    conn.close()
except Exception:
    _fts5_available = False

pytestmark = pytest.mark.skipif(not _fts5_available, reason="FTS5 not available")


async def test_find_exact_duplicate_no_match(db):
    """Returns None when content doesn't exist."""
    result = await memory.find_exact_duplicate(db, content="nonexistent content")
    assert result is None


async def test_find_exact_duplicate_finds_match(db):
    """Returns memory_id when exact content exists."""
    await memory.create(db, memory_id="dup-m1", content="hello world duplicate test")
    result = await memory.find_exact_duplicate(
        db, content="hello world duplicate test",
    )
    assert result == "dup-m1"


async def test_find_exact_duplicate_cross_collection(db):
    """Finds duplicate regardless of FTS collection column value.

    This is critical: the FTS collection column is unreliable (uniformly
    episodic_memory), so dedup must be collection-agnostic.
    """
    await memory.create(
        db, memory_id="kb-m1", content="knowledge content",
        collection="knowledge_base",
    )
    # Search without specifying collection — should still find it
    result = await memory.find_exact_duplicate(db, content="knowledge content")
    assert result == "kb-m1"


async def test_find_exact_duplicate_empty_content(db):
    """Returns None for empty content."""
    result = await memory.find_exact_duplicate(db, content="")
    assert result is None


async def test_find_exact_duplicate_similar_but_different(db):
    """Does not match content that shares prefix but differs."""
    await memory.create(
        db, memory_id="sim-m1",
        content="this is the original content with specific ending AAA",
    )
    # Same prefix, different ending
    result = await memory.find_exact_duplicate(
        db, content="this is the original content with specific ending BBB",
    )
    assert result is None


async def test_find_exact_duplicate_same_length_different_content(db):
    """Does not match content with same length but different text."""
    content_a = "aaaa bbbb cccc"
    content_b = "xxxx yyyy zzzz"
    assert len(content_a) == len(content_b)

    await memory.create(db, memory_id="len-m1", content=content_a)
    result = await memory.find_exact_duplicate(db, content=content_b)
    assert result is None


async def test_find_exact_duplicate_returns_first_match(db):
    """When multiple duplicates exist, returns the first one found."""
    await memory.create(db, memory_id="first-m1", content="duplicate content here")
    # Create a second entry with same content but different ID
    # (simulating the bug this fixes)
    await memory.create(db, memory_id="second-m2", content="duplicate content here")

    result = await memory.find_exact_duplicate(
        db, content="duplicate content here",
    )
    # Should return one of them (which one depends on rowid order)
    assert result in ("first-m1", "second-m2")
# ── Visibility: a duplicate SUPPRESSES a write, so it must be recallable ──
#
# `find_exact_duplicate`'s result makes the caller skip creating the memory and
# return the id found here. Matching a row the user can never retrieve turns a
# successful-looking store into a silent loss — the content is not stored, and
# the id handed back names something invisible. These pin the three exclusions
# that ordinary recall already applies.


async def _meta(db, memory_id, **cols):
    """Insert the metadata row `find_exact_duplicate` now joins against."""
    keys = ["memory_id", "created_at", *cols]
    vals = [memory_id, "2026-09-06T00:00:00+00:00", *cols.values()]
    placeholders = ",".join("?" * len(keys))
    await db.execute(
        f"INSERT INTO memory_metadata ({','.join(keys)}) VALUES ({placeholders})",
        vals,
    )
    await db.commit()


async def test_a_subsystem_row_is_not_a_duplicate(db):
    """Automated ego/triage/reflection writes are excluded from user recall.

    Deduping against one means a user store is skipped in favour of a row they
    cannot see.
    """
    await memory.create(db, memory_id="sub-1", content="the gate blocks on P1")
    await _meta(db, "sub-1", source_subsystem="ego")

    assert await memory.find_exact_duplicate(db, content="the gate blocks on P1") is None


async def test_a_deprecated_row_is_not_a_duplicate(db):
    """A superseded row is filtered by every read path."""
    await memory.create(db, memory_id="dep-1", content="CC pins node 22")
    await _meta(db, "dep-1", deprecated=1)

    assert await memory.find_exact_duplicate(db, content="CC pins node 22") is None


async def test_an_expired_row_is_not_a_duplicate(db):
    """Past `invalid_at` is hidden by the bitemporal filter."""
    await memory.create(db, memory_id="exp-1", content="the pin is 2.1.100")
    await _meta(db, "exp-1", invalid_at="2020-01-01T00:00:00+00:00")

    assert await memory.find_exact_duplicate(db, content="the pin is 2.1.100") is None


async def test_a_future_invalid_at_row_IS_still_a_duplicate(db):
    """Control for the expiry case: validity that has not run out is visible,
    so it must still suppress. Without this, "excludes expired rows" could be
    satisfied by excluding every row that sets the column at all.
    """
    await memory.create(db, memory_id="fut-1", content="the pin is 2.1.246")
    await _meta(db, "fut-1", invalid_at="2099-01-01T00:00:00+00:00")

    assert await memory.find_exact_duplicate(db, content="the pin is 2.1.246") == "fut-1"


async def test_a_row_with_NO_metadata_is_still_a_duplicate(db):
    """Control for the join: the LEFT JOIN must not drop legacy rows.

    An inner join here would make every metadata-less row undedupable, which
    turns a visibility fix into a duplicate generator.
    """
    await memory.create(db, memory_id="bare-1", content="a legacy row with no meta")

    assert await memory.find_exact_duplicate(db, content="a legacy row with no meta") == "bare-1"


# ── Scope: the pool a write dedups against is the pool its readers see ──────
#
# `only_subsystem` recall returns ONLY rows from the named subsystem — user
# content and other subsystems are both excluded. So an automated write whose
# dedup pool were the user-visible rows would mint a copy on every identical
# retry, and one whose pool were all rows could suppress onto a row its own
# readers can never see. The scope is the write's `source_subsystem`.


async def test_a_subsystem_write_dedups_within_its_own_scope(db):
    """A second identical write from the same subsystem finds its prior row."""
    await memory.create(db, memory_id="ref-1", content="the gate blocks on P1")
    await _meta(db, "ref-1", source_subsystem="reflection")

    assert await memory.find_exact_duplicate(
        db, content="the gate blocks on P1", source_subsystem="reflection",
    ) == "ref-1"


async def test_a_subsystem_write_ignores_other_subsystems_rows(db):
    """A different subsystem's row is outside this write's recall scope."""
    await memory.create(db, memory_id="ego-1", content="the gate blocks on P1")
    await _meta(db, "ego-1", source_subsystem="ego")

    assert await memory.find_exact_duplicate(
        db, content="the gate blocks on P1", source_subsystem="reflection",
    ) is None


async def test_a_subsystem_write_ignores_user_rows(db):
    """A user-visible row cannot suppress a subsystem write.

    `only_subsystem` recall excludes NULL-subsystem rows, so deduping the
    write onto one would return an id the subsystem's own readers never see.
    """
    await memory.create(db, memory_id="usr-1", content="the gate blocks on P1")
    await _meta(db, "usr-1")

    assert await memory.find_exact_duplicate(
        db, content="the gate blocks on P1", source_subsystem="reflection",
    ) is None


# ── Delete intent: a row queued for deletion cannot be the "duplicate" ─────
#
# A deferred delete keeps the FTS row and metadata until the reconcile lane
# retries it. If dedup names that row, the caller suppresses the new store and
# returns an id that is about to stop existing — the retry then removes the
# only copy. The exclusion keys on the same identity `has_open_tombstone`
# checks: (topic=memory_id, category='memory_delete', signal_type NULL),
# status pending/processing.


async def _tombstone(db, memory_id, status="pending"):
    """Insert a deferred-delete tombstone row for *memory_id*."""
    import json

    await db.execute(
        "INSERT INTO deferred_work_queue "
        "(id, work_type, priority, payload_json, deferred_at, "
        " deferred_reason, staleness_policy, status, created_at) "
        "VALUES (?, 'memory_deferred_delete', 0, ?, "
        " '2026-09-06T00:00:00+00:00', 'qdrant down', 'drain', ?, "
        " '2026-09-06T00:00:00+00:00')",
        (
            f"ts-{memory_id}-{status}",
            json.dumps(
                {
                    "topic": memory_id,
                    "category": "memory_delete",
                    "signal_type": None,
                    "memory_id": memory_id,
                }
            ),
            status,
        ),
    )
    await db.commit()


async def test_a_row_with_an_open_tombstone_is_not_a_duplicate(db):
    """Pending delete intent: the row is doomed, not a duplicate."""
    await memory.create(db, memory_id="doomed-1", content="the gate blocks on P1")
    await _tombstone(db, "doomed-1", status="pending")

    assert (
        await memory.find_exact_duplicate(db, content="the gate blocks on P1")
        is None
    )


async def test_a_processing_tombstone_is_also_open(db):
    """'processing' is still open — the drain worker owns it but has not
    finished, so the row is still doomed."""
    await memory.create(db, memory_id="doomed-2", content="the gate blocks on P1")
    await _tombstone(db, "doomed-2", status="processing")

    assert (
        await memory.find_exact_duplicate(db, content="the gate blocks on P1")
        is None
    )


async def test_a_completed_tombstone_does_not_hide_the_row(db):
    """Control: a closed intent means the delete already resolved — if the
    row is still indexed it is live, so it must still suppress."""
    await memory.create(db, memory_id="lives-1", content="the gate blocks on P1")
    await _tombstone(db, "lives-1", status="completed")

    assert (
        await memory.find_exact_duplicate(db, content="the gate blocks on P1")
        == "lives-1"
    )


async def test_another_memory_tombstone_does_not_hide_the_row(db):
    """Control for the identity join: only THIS row's tombstone excludes it."""
    await memory.create(db, memory_id="other-1", content="unrelated row")
    await memory.create(db, memory_id="lives-2", content="the gate blocks on P1")
    await _tombstone(db, "other-1")

    assert (
        await memory.find_exact_duplicate(db, content="the gate blocks on P1")
        == "lives-2"
    )
