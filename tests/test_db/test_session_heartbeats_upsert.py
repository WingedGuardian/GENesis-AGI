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


# ---------------------------------------------------------------------------
# Roster identity columns (pid, pid_started_at, cwd, git_branch, slot)
# ---------------------------------------------------------------------------


async def _identity_row(db, sid: str = _SID) -> dict:
    cur = await db.execute(
        "SELECT pid, pid_started_at, cwd, git_branch, slot "
        "FROM session_heartbeats WHERE cc_session_id = ?",
        (sid,),
    )
    r = await cur.fetchone()
    assert r is not None, "the heartbeat row was never written"
    return dict(r)


@pytest.mark.parametrize("column,value", [("cwd", "/some/dir"), ("slot", "3")])
async def test_identity_column_preserved_when_omitted(db, column, value):
    """cwd/slot follow the table's COALESCE contract: omitted = unknown."""
    await session_heartbeats.upsert(db, cc_session_id=_SID, **{column: value})
    assert (await _identity_row(db))[column] == value, "setup: value never stored"

    await session_heartbeats.upsert(db, cc_session_id=_SID)

    assert (await _identity_row(db))[column] == value, (
        f"{column} was WIPED by a writer that did not know it"
    )


async def test_pid_pair_writes_and_preserves_atomically(db):
    """pid and pid_started_at move as ONE pair.

    pid_started_at exists solely to reject a recycled pid at observe time, so a
    stored (pid, started_at) from two different writes would attribute one
    process's start time to another — worse than unknown. The contract: a write
    that KNOWS the pid updates both; a write that does not know the pid (None)
    touches neither.
    """
    await session_heartbeats.upsert(
        db, cc_session_id=_SID, pid=100, pid_started_at="2026-01-01T00:00:00+00:00"
    )
    row = await _identity_row(db)
    assert (row["pid"], row["pid_started_at"]) == (100, "2026-01-01T00:00:00+00:00")

    # Resume case: same session id, NEW process — an informed write overwrites.
    await session_heartbeats.upsert(
        db, cc_session_id=_SID, pid=200, pid_started_at="2026-02-02T00:00:00+00:00"
    )
    row = await _identity_row(db)
    assert (row["pid"], row["pid_started_at"]) == (200, "2026-02-02T00:00:00+00:00")

    # Uninformed write (walker failed): the PAIR is preserved, not half of it.
    await session_heartbeats.upsert(db, cc_session_id=_SID)
    row = await _identity_row(db)
    assert (row["pid"], row["pid_started_at"]) == (200, "2026-02-02T00:00:00+00:00")


async def test_git_branch_three_valued_contract(db):
    """"" = known-not-on-a-branch (overwrites); None = unknown (preserves)."""
    await session_heartbeats.upsert(db, cc_session_id=_SID, git_branch="main")
    assert (await _identity_row(db))["git_branch"] == "main"

    # Left the repo / detached: the writer KNOWS there is no branch.
    await session_heartbeats.upsert(db, cc_session_id=_SID, git_branch="")
    assert (await _identity_row(db))["git_branch"] == "", (
        "an empty string is a real value and must overwrite"
    )

    await session_heartbeats.upsert(db, cc_session_id=_SID, git_branch="feat/x")
    await session_heartbeats.upsert(db, cc_session_id=_SID, git_branch=None)
    assert (await _identity_row(db))["git_branch"] == "feat/x", (
        "None means resolution failed — it must not clear a known branch"
    )


async def test_sync_identity_columns_parity(tmp_path):
    """The sync twin carries the same identity-column contract as the async."""
    import sqlite3

    import aiosqlite

    from genesis.db.schema import create_all_tables

    path = tmp_path / "hb2.db"
    conn = await aiosqlite.connect(str(path))
    try:
        await create_all_tables(conn)
        await conn.commit()
    finally:
        await conn.close()

    session_heartbeats.upsert_sync(
        str(path),
        cc_session_id=_SID,
        pid=100,
        pid_started_at="2026-01-01T00:00:00+00:00",
        cwd="/w",
        git_branch="main",
        slot="2",
    )

    def _read() -> tuple | None:
        c = sqlite3.connect(str(path))
        try:
            return c.execute(
                "SELECT pid, pid_started_at, cwd, git_branch, slot "
                "FROM session_heartbeats WHERE cc_session_id = ?",
                (_SID,),
            ).fetchone()
        finally:
            c.close()

    assert _read() == (100, "2026-01-01T00:00:00+00:00", "/w", "main", "2"), (
        "setup failed: the identity write never landed (upsert_sync swallows errors)"
    )

    # Pure liveness touch preserves everything.
    session_heartbeats.upsert_sync(str(path), cc_session_id=_SID)
    assert _read() == (100, "2026-01-01T00:00:00+00:00", "/w", "main", "2")


async def test_roster_reads_degrade_on_premigration_schema(tmp_path):
    """MEASURED live (2026-09-05): on a DB that predates the identity
    migration, selecting the new columns raises and the swallow turned the
    whole roster into [] — every peer invisible, which reads exactly like "no
    concurrent sessions". The mid-deploy window (hooks updated at session
    start, DB migrated at server restart) puts EVERY install here. The read
    must degrade to the legacy column set with identity fields as None.

    The legacy schema is built by replaying the PRE-migration DDL: create the
    current schema, then apply the migration's own down() — so this fixture
    tracks reality instead of a hand-copied CREATE."""
    import importlib

    import aiosqlite

    from genesis.db.schema import create_all_tables

    path = tmp_path / "legacy.db"
    conn = await aiosqlite.connect(str(path))
    try:
        await create_all_tables(conn)
        mig = importlib.import_module(
            "genesis.db.migrations.20260905194140_roster_identity_columns"
        )
        await mig.down(conn)  # strip the identity columns -> legacy shape
        await conn.commit()
        from datetime import UTC, datetime


        # legacy-writer shape: the OLD upsert had no identity kwargs; emulate
        # with a direct minimal insert through the same connection.
        await conn.execute(
            "INSERT INTO session_heartbeats (cc_session_id, updated_at) "
            "VALUES (?, ?)",
            ("legacy-peer", datetime.now(UTC).isoformat()),
        )
        await conn.commit()
    finally:
        await conn.close()

    rows = session_heartbeats.get_roster_sync(str(path))
    assert len(rows) == 1, "pre-migration schema must degrade, not read empty"
    assert rows[0]["cc_session_id"] == "legacy-peer"
    assert rows[0]["pid"] is None and rows[0]["git_branch"] is None


async def test_roster_dedupes_same_pid_keeping_newest(db):
    """A /clear starts a new cc_session_id in the SAME claude process; the
    old conversation's row keeps a live pid for 24h and rendered as a
    phantom live peer (review finding). One row per pid — the newest."""
    from genesis.db.crud.session_heartbeats import get_roster

    await session_heartbeats.upsert(db, cc_session_id="old-convo", pid=4242,
                                    pid_started_at="2026-01-01T00:00:00+00:00")
    await session_heartbeats.upsert(db, cc_session_id="new-convo", pid=4242,
                                    pid_started_at="2026-01-01T00:00:00+00:00")
    await session_heartbeats.upsert(db, cc_session_id="pidless-a")
    await session_heartbeats.upsert(db, cc_session_id="pidless-b")

    rows = await get_roster(db)
    ids = [r["cc_session_id"] for r in rows]
    assert "new-convo" in ids and "old-convo" not in ids, (
        f"same-pid dedupe must keep only the newest conversation: {ids}"
    )
    assert "pidless-a" in ids and "pidless-b" in ids, "pid-less rows exempt"
