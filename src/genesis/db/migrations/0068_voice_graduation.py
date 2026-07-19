"""Voice graduation W0 — quarantine table + memory_metadata provenance columns.

The edge→core "graduation" boundary lands typed, synthesized events (never raw
transcripts) in a quarantine table; a policy drainer (W2, separate PR) later
routes them into memory stores. This migration ships the substrate DARK:

- ``graduation_events`` — verbatim quarantine landing for
  ``POST /v1/voice/graduate``. ``event_id`` UNIQUE is the transport dedup key
  (edge outbox is at-least-once; INSERT OR IGNORE in CRUD makes delivery
  effectively-once). ``disposition`` starts ``'pending'``; only the W2 drainer
  moves it. Retention: dispositioned rows pruned after 90d
  (``crud.graduation_events.prune_older_than``); pending rows are NEVER pruned.
- ``memory_metadata`` gains 5 NULLable columns (``provenance_class``,
  ``trust_level``, ``attribution``, ``origin_ref``, ``capture_clarity``) —
  GROUNDWORK(voice-graduation-w2), written by the drainer. NULLable keeps the
  ADD COLUMN O(1) metadata-only; non-overheard rows stay NULL by design (the
  trust ladder is overheard-only).

Additive + idempotent; DDL mirrored in ``db/schema/_tables.py`` AND the
``memory_metadata`` columns mirrored in ``_migrate_add_columns``
(schema_both_build_paths — the #1123/#1127 class). No backfill (nothing
overheard exists yet). Individual ``db.execute()`` calls, no commit — the
runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite

_MEMORY_METADATA_COLUMNS = (
    ("provenance_class", "TEXT"),
    ("trust_level", "TEXT"),
    ("attribution", "TEXT"),
    ("origin_ref", "TEXT"),
    ("capture_clarity", "REAL"),
)


async def _has_table(db: aiosqlite.Connection, name: str) -> bool:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return await cursor.fetchone() is not None


async def up(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS graduation_events (
            id                 TEXT PRIMARY KEY,
            event_id           TEXT NOT NULL UNIQUE,
            schema_version     INTEGER NOT NULL,
            type               TEXT NOT NULL
                               CHECK (type IN ('perk_up','memory_candidate','meeting_summary')),
            source             TEXT NOT NULL,
            occurred_at        TEXT NOT NULL,
            received_at        TEXT NOT NULL,
            payload            TEXT NOT NULL,
            provenance         TEXT NOT NULL,
            disposition        TEXT NOT NULL DEFAULT 'pending'
                               CHECK (disposition IN ('pending','landed','rejected','merged')),
            memory_id          TEXT,
            disposition_reason TEXT,
            disposed_at        TEXT
        )
        """
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_graduation_events_disposition "
        "ON graduation_events(disposition, received_at)"
    )

    if await _has_table(db, "memory_metadata"):
        col_cursor = await db.execute("PRAGMA table_info(memory_metadata)")
        cols = {row[1] for row in await col_cursor.fetchall()}
        for name, sql_type in _MEMORY_METADATA_COLUMNS:
            if name not in cols:
                await db.execute(
                    f"ALTER TABLE memory_metadata ADD COLUMN {name} {sql_type}"  # noqa: S608 - fixed identifiers from this module only
                )


async def down(db: aiosqlite.Connection) -> None:
    await db.execute("DROP TABLE IF EXISTS graduation_events")
    # memory_metadata columns stay — additive-only policy (0054 precedent).
