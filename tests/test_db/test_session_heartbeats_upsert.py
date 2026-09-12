"""The session_heartbeats upsert must PRESERVE fields a partial writer omits.

The table has several independent writers that each know a different subset of
the row: the UserPromptSubmit hook knows the prompt and the tool digest, a
tool-use refresh knows only that the session is alive, and the model comes from
a cache another hook populates at SessionStart. A writer that omits a column is
saying "I don't know this", never "clear it".

The conflict clause originally said ``model = excluded.model`` (and the same for
both summaries), so passing None WIPED a stored value. Only ``topic`` was
COALESCE-preserved. That is a live data-loss path rather than a style point: the
model cache is bounded at 24 entries with insertion-order eviction, so a
long-lived session whose entry is evicted resolves no model on its next write
and would destroy the value already stored.
"""

import pytest

from genesis.db.crud import session_heartbeats

_SID = "test-session-0001"


async def _row(db, sid: str = _SID) -> dict:
    cur = await db.execute(
        "SELECT model, topic, user_summary, genesis_summary, source_tag "
        "FROM session_heartbeats WHERE cc_session_id = ?",
        (sid,),
    )
    r = await cur.fetchone()
    assert r is not None, "the heartbeat row was never written"
    return dict(r)


@pytest.mark.parametrize(
    "column",
    ["model", "topic", "user_summary", "genesis_summary"],
)
async def test_omitting_a_column_preserves_it(db, column):
    """A later writer that does not know a column must not clear it."""
    await session_heartbeats.upsert(db, cc_session_id=_SID, **{column: "original"})
    assert (await _row(db))[column] == "original", "setup failed: value never stored"

    # A partial writer: knows the session is alive, knows nothing else.
    await session_heartbeats.upsert(db, cc_session_id=_SID)

    assert (await _row(db))[column] == "original", (
        f"{column} was WIPED by a writer that simply did not know it -- "
        "omitting a column means 'unknown', never 'clear it'"
    )


async def test_an_explicit_value_still_overwrites(db):
    """Preserve-on-None must not become preserve-always."""
    await session_heartbeats.upsert(db, cc_session_id=_SID, model="old", topic="t1")
    await session_heartbeats.upsert(db, cc_session_id=_SID, model="new", topic="t2")
    row = await _row(db)
    assert row["model"] == "new"
    assert row["topic"] == "t2"


async def test_source_tag_still_overwrites(db):
    """source_tag is deliberately NOT preserved -- it has a NOT NULL default.

    Pinned so a future 'make everything COALESCE' sweep has to justify itself
    against a column whose default is meaningful.
    """
    await session_heartbeats.upsert(db, cc_session_id=_SID, source_tag="reflection")
    await session_heartbeats.upsert(db, cc_session_id=_SID)
    assert (await _row(db))["source_tag"] == "foreground"


async def test_sync_upsert_preserves_identically(tmp_path):
    """The sync path is what the hooks actually call -- it must not diverge.

    Two implementations of one conflict clause is exactly how a fix lands on one
    writer and not the other, so the contract is pinned on both. This needs a
    FILE database rather than the shared in-memory ``db`` fixture, because
    ``upsert_sync`` takes a path (hooks cannot share the runtime's connection).

    Note ``upsert_sync`` swallows every exception -- "best-effort, never block the
    hook" -- so a write that fails for ANY reason is indistinguishable from one
    that wrote a None. That is exactly why this asserts the stored ROW and seeds
    a real value first: without the seed assertion, a silently-failed write and a
    correctly-preserved value look the same.
    """
    import sqlite3

    import aiosqlite

    from genesis.db.schema import create_all_tables

    path = tmp_path / "hb.db"
    conn = await aiosqlite.connect(str(path))
    try:
        await create_all_tables(conn)
        await conn.commit()
    finally:
        await conn.close()

    session_heartbeats.upsert_sync(str(path), cc_session_id=_SID, model="m1", topic="t1")

    def _read() -> tuple | None:
        c = sqlite3.connect(str(path))
        try:
            return c.execute(
                "SELECT model, topic FROM session_heartbeats WHERE cc_session_id = ?",
                (_SID,),
            ).fetchone()
        finally:
            c.close()

    assert _read() == ("m1", "t1"), (
        "setup failed: the first sync write never landed, so anything below "
        "would prove nothing (upsert_sync swallows its own errors)"
    )

    session_heartbeats.upsert_sync(str(path), cc_session_id=_SID)

    assert _read() == ("m1", "t1"), (
        f"sync upsert wiped a field it was never told about: {_read()}"
    )
