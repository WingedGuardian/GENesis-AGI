"""CRUD tests for pending_outreach — thread_id / validated_recipient round-trip.

The subprocess fallback path (``outreach_send`` with ``pipeline=None``) enqueues
here. It must persist the resolved thread + recipient so the genesis-server
drain can rebuild a properly-routed request instead of defaulting a recipient-
less email to the agent's own address.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import pending_outreach


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await pending_outreach.ensure_table(conn)
        yield conn


@pytest.mark.asyncio
async def test_ensure_table_includes_new_columns(db):
    cur = await db.execute("PRAGMA table_info(pending_outreach)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "thread_id" in cols
    assert "validated_recipient" in cols


@pytest.mark.asyncio
async def test_enqueue_persists_thread_and_recipient(db):
    await pending_outreach.enqueue(
        db,
        message="follow up",
        category="notification",
        channel="email",
        thread_id="thread-123",
        validated_recipient="real@prospect.com",
    )
    rows = await pending_outreach.drain(db, now="2999-01-01T00:00:00+00:00")
    assert len(rows) == 1
    assert rows[0]["thread_id"] == "thread-123"
    assert rows[0]["validated_recipient"] == "real@prospect.com"


@pytest.mark.asyncio
async def test_enqueue_defaults_to_none(db):
    await pending_outreach.enqueue(
        db,
        message="m",
        category="notification",
        channel="telegram",
    )
    rows = await pending_outreach.drain(db, now="2999-01-01T00:00:00+00:00")
    assert len(rows) == 1
    assert rows[0]["thread_id"] is None
    assert rows[0]["validated_recipient"] is None


@pytest.mark.asyncio
async def test_drain_exposes_rowid(db):
    """drain must surface rowid so NULL-id rows can be cleared by rowid."""
    await pending_outreach.enqueue(
        db,
        message="m",
        category="notification",
        channel="telegram",
    )
    rows = await pending_outreach.drain(db, now="2999-01-01T00:00:00+00:00")
    assert len(rows) == 1
    assert isinstance(rows[0]["rowid"], int)


@pytest.mark.asyncio
async def test_mark_delivered_by_rowid_clears_null_id_row(db):
    """A NULL-id row can't be marked by id (WHERE id=NULL matches nothing);
    mark_delivered_by_rowid targets it by its always-present rowid."""
    await db.execute(
        "INSERT INTO pending_outreach (message, category, channel, urgency, "
        "created_at, delivered) VALUES ('m', 'notification', 'telegram', "
        "'low', '2020-01-01T00:00:00+00:00', 0)",
    )
    await db.commit()
    rows = await pending_outreach.drain(db, now="2999-01-01T00:00:00+00:00")
    assert len(rows) == 1 and rows[0]["id"] is None
    rowid = rows[0]["rowid"]

    # id-keyed mark is a no-op on a NULL id; rowid-keyed clears it.
    assert (
        await pending_outreach.mark_delivered(db, None, delivered_at="2026-01-01T00:00:00+00:00")
        is False
    )
    assert (
        await pending_outreach.mark_delivered_by_rowid(
            db, rowid, delivered_at="2026-01-01T00:00:00+00:00"
        )
        is True
    )
    assert await pending_outreach.drain(db, now="2999-01-01T00:00:00+00:00") == []


# ── cancel ────────────────────────────────────────────────────────────────────
# A queued message could previously only be enqueued or marked DELIVERED. With no
# cancel, changing a reminder's time meant either sending a duplicate or writing
# delivered=1 for something never sent — a false record in a store that gets read
# back. Cancelling records a distinct state instead.


@pytest.mark.asyncio
async def test_cancel_removes_a_queued_message_from_the_drain(db):
    """The load-bearing assertion: a cancelled row must not be DELIVERED.

    `drain` was the only pending-predicate over this table, so it is the one gate
    that has to learn about the new state — miss it and cancel silently does
    nothing while reporting success.
    """
    pid = await pending_outreach.enqueue(
        db,
        message="reminder at the wrong time",
        category="notification",
    )
    assert await pending_outreach.drain(db, now="2999-01-01T00:00:00+00:00")

    assert await pending_outreach.cancel(db, pid) == (True, "cancelled")
    assert await pending_outreach.drain(db, now="2999-01-01T00:00:00+00:00") == []


@pytest.mark.asyncio
async def test_cancel_is_distinguishable_from_delivered(db):
    """Cancelled must NOT masquerade as delivered — that was the whole point."""
    pid = await pending_outreach.enqueue(
        db,
        message="m",
        category="notification",
    )
    await pending_outreach.cancel(db, pid)
    cur = await db.execute(
        "SELECT delivered, delivered_at, cancelled_at FROM pending_outreach WHERE id = ?",
        (pid,),
    )
    row = await cur.fetchone()
    assert row["delivered"] == 0, "a cancelled message must not read as delivered"
    assert row["delivered_at"] is None
    assert row["cancelled_at"] is not None, "cancellation must be recorded, not inferred"


@pytest.mark.asyncio
async def test_cancel_refuses_a_dequeued_message_without_claiming_delivery(db):
    """Cancelling a message that has left the queue must fail — and the reason must
    NOT say "delivered", because the flag does not mean that.

    ``delivered = 1`` is written by the drain for three outcomes, only one of which
    is a send: DELIVERED/ENGAGED (sent), HELD (handed to the autonomy gate, still
    awaiting the owner's approval, and it WILL send afterwards), and the 24h
    age-out whose own log line reads "never delivered". An earlier version of this
    reason said "already delivered (cannot be un-sent)" — false in the HELD case,
    which is precisely the window where someone wants to retract.
    """
    pid = await pending_outreach.enqueue(db, message="m", category="notification")
    await pending_outreach.mark_delivered(db, pid, delivered_at="2026-01-01T00:00:00+00:00")
    assert await pending_outreach.cancel(db, pid) == (False, "already_dequeued"), (
        "delivered=1 also means HELD / aged-out, so the reason must not assert delivery"
    )


@pytest.mark.asyncio
async def test_cancel_unknown_id_returns_false(db):
    """An unknown id is not a silent success — the caller must be able to tell."""
    assert await pending_outreach.cancel(db, "no-such-id") == (False, "unknown_id")


@pytest.mark.asyncio
async def test_cancel_is_idempotent_but_reports_honestly(db):
    """A second cancel changes nothing and says so, rather than claiming a fresh
    cancellation (which would let a caller double-count)."""
    pid = await pending_outreach.enqueue(db, message="m", category="notification")
    assert await pending_outreach.cancel(db, pid) == (True, "cancelled")
    assert await pending_outreach.cancel(db, pid) == (False, "already_cancelled")


@pytest.mark.asyncio
async def test_ensure_table_includes_cancelled_at(db):
    """The CREATE path carries the column — ensure_table is the standalone
    fallback used on a subprocess-only boot, before the numbered migration runs."""
    cur = await db.execute("PRAGMA table_info(pending_outreach)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "cancelled_at" in cols


@pytest.mark.asyncio
async def test_ensure_table_adds_cancelled_at_to_a_legacy_table(tmp_path):
    """The ALTER path — the one every EXISTING install takes, and the one the
    CREATE-path test above cannot reach.

    Mutation-verified: deleting the ALTER left the CREATE-path test green, because
    a fresh table is built with the column already in it. So the branch that runs
    on a real upgrade had no coverage at all. Build the pre-column shape
    explicitly, or this test silently becomes a second copy of the one above.
    """
    async with aiosqlite.connect(str(tmp_path / "legacy.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("""
            CREATE TABLE pending_outreach (
                id                  TEXT PRIMARY KEY,
                message             TEXT NOT NULL,
                category            TEXT NOT NULL,
                channel             TEXT NOT NULL DEFAULT 'telegram',
                urgency             TEXT NOT NULL DEFAULT 'low',
                deliver_after       TEXT,
                created_at          TEXT NOT NULL,
                delivered           INTEGER NOT NULL DEFAULT 0,
                delivered_at        TEXT,
                thread_id           TEXT,
                validated_recipient TEXT
            )
        """)
        # Deliberately post-0038 (thread_id present) but pre-labeled_surplus:
        # ensure_table's legacy block heals exactly labeled_surplus and
        # cancelled_at, so those two are what the fixture must be missing.
        await conn.commit()

        pre = {
            row[1]
            for row in await (await conn.execute("PRAGMA table_info(pending_outreach)")).fetchall()
        }
        assert "cancelled_at" not in pre, "fixture must start WITHOUT the column"

        await pending_outreach.ensure_table(conn)

        post = {
            row[1]
            for row in await (await conn.execute("PRAGMA table_info(pending_outreach)")).fetchall()
        }
        assert "cancelled_at" in post
        assert "labeled_surplus" in post, "the pre-existing legacy ALTER must still fire"

        # Idempotent: a second call must not raise "duplicate column name".
        await pending_outreach.ensure_table(conn)

        # And the new column is usable on the migrated table, not merely present.
        pid = await pending_outreach.enqueue(conn, message="m", category="notification")
        assert await pending_outreach.cancel(conn, pid) == (True, "cancelled")
