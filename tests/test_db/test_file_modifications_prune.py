"""Retention test for the audit-trail file_modifications table.

The table is written per file edit by scripts/file_modification_audit_hook.py, so it grows
unbounded. Its ``prune_older_than`` existed but had no caller until the learning-scheduler job
added in this change — cover it before wiring it into a scheduled irreversible delete. Age is
set via the ``timestamp`` column relative to now -> wall-clock-independent.
"""

from datetime import UTC, datetime, timedelta

from genesis.db.crud import file_modifications


async def test_prune_older_than_deletes_only_old(db):
    now = datetime.now(UTC)
    await file_modifications.record(
        db, session_id="s1", file_path="/old", action="edit",
        timestamp=(now - timedelta(days=120)).isoformat(),
    )
    await file_modifications.record(
        db, session_id="s1", file_path="/new", action="edit",
        timestamp=(now - timedelta(days=10)).isoformat(),
    )
    removed = await file_modifications.prune_older_than(db, days=90)
    assert removed == 1
    assert await file_modifications.query_by_file(db, "/new")  # kept
    assert await file_modifications.query_by_file(db, "/old") == []  # pruned


async def test_prune_older_than_empty_is_zero(db):
    assert await file_modifications.prune_older_than(db, days=90) == 0
