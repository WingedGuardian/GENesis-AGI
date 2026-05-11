"""Rename confusable call-site IDs in DB-stored rows (Tier 3 rename pass).

The 2026-05-10 routing-config rename disambiguated three call sites whose
descriptors collided in the original numbering scheme:

    17_fresh_eyes_review  -> 17_executor_review
    23_fresh_eyes_review  -> 23_outreach_review
    email_triage          -> outreach_email_triage

YAML, code, dashboard templates, meta, and docs are updated by the same PR.
This migration brings the DB rows along so the dashboard's "last run" column
and any pending deferred-work entries don't reference dead IDs.

Tables touched:

* ``call_site_last_run`` -- one row per call site recording its most recent
  execution. Without this migration, the dashboard would show "no last_run"
  for the three renamed sites until they fire again.
* ``deferred_work_queue`` -- rows with the renamed ``call_site_id`` would
  otherwise stay attributed to a dead ID. The migration runs at server
  startup before workers pick up new jobs, so there's no race against
  in-flight execution.

NOT touched:

* ``cost_events.metadata`` -- the historical audit log. The JSON-embedded
  ``$.call_site`` values reflect what actually happened at the time. Rewriting
  them would falsify history. Future cost events will use the new IDs; old
  ones stay as written.

Idempotent: ``UPDATE ... WHERE call_site_id = '<old>'`` is a no-op once the
rows have been renamed.
"""

from __future__ import annotations

import aiosqlite

# (old_id, new_id) pairs for the rename.
_RENAMES: list[tuple[str, str]] = [
    ("17_fresh_eyes_review", "17_executor_review"),
    ("23_fresh_eyes_review", "23_outreach_review"),
    ("email_triage", "outreach_email_triage"),
]


async def up(db: aiosqlite.Connection) -> None:
    for table in ("call_site_last_run", "deferred_work_queue"):
        # PRAGMA query confirms the table exists before touching it -- avoids
        # ERR on installs that haven't run earlier migrations yet.
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        if not await cursor.fetchone():
            continue
        for old_id, new_id in _RENAMES:
            # `call_site_last_run` uses call_site_id as PRIMARY KEY. If a row
            # already exists for the new ID (e.g., partial rename retry, or a
            # call_site_id collision from manual DB editing), a naive UPDATE
            # would fail with a PK conflict. Delete the old row in that case
            # -- the new-ID row is the authoritative one going forward.
            await db.execute(
                f"DELETE FROM {table} WHERE call_site_id = ?"
                f"  AND EXISTS (SELECT 1 FROM {table} WHERE call_site_id = ?)",
                (old_id, new_id),
            )
            await db.execute(
                f"UPDATE {table} SET call_site_id = ? WHERE call_site_id = ?",
                (new_id, old_id),
            )


async def down(db: aiosqlite.Connection) -> None:
    for table in ("call_site_last_run", "deferred_work_queue"):
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        if not await cursor.fetchone():
            continue
        for old_id, new_id in _RENAMES:
            await db.execute(
                f"UPDATE {table} SET call_site_id = ? WHERE call_site_id = ?",
                (old_id, new_id),
            )
