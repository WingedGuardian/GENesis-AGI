"""Backfill ``observations.origin_class`` for pre-provenance rows (WS-3).

``origin_class`` was added by migration 0057 but never backfilled, so every row
written before the write-boundary chokepoint (feat: observation write-boundary
origin provenance) carries ``origin_class IS NULL``. The read side is being
switched to treat NULL as external (fail-closed), so without this backfill the
laundering-critical surfaces (essential_knowledge L1, the reflection pipeline)
would drop ALL history. This stamps a definite origin where one can be derived,
and DELIBERATELY leaves genuinely-unknown sources NULL (fail-closed → excluded,
cosmetic, never a leak).

Two derivation paths, mirroring :func:`derive_observation_origin` minus the live
session-env (which is meaningless at migration time):

1. ``source = 'session:<cc_session_id>'`` → inherit the session's own stored
   ``cc_sessions.origin_class`` (JOIN). A session with NULL origin (foreground /
   pre-substrate) stays NULL → fail-closed.
2. every other source → :func:`genesis.memory.provenance._origin_from_source`
   (the authoritative, env-free source→origin classifier: recon/email_recon →
   external; the ``intake:*`` split via surplus._pipeline_for_source; the curated
   first-party allowlist; everything else → None).

Idempotent: every UPDATE is guarded ``WHERE origin_class IS NULL``. No commit —
the runner owns the transaction.
"""

from __future__ import annotations

import aiosqlite


async def _has_table(db: aiosqlite.Connection, name: str) -> bool:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return await cursor.fetchone() is not None


async def up(db: aiosqlite.Connection) -> None:
    if not await _has_table(db, "observations"):
        return

    # 1. Session-attributed rows inherit their session's origin_class.
    #    'session:' is 8 chars → the id starts at position 9 (SQLite substr is
    #    1-indexed). Only set where the joined origin is itself non-NULL.
    if await _has_table(db, "cc_sessions"):
        await db.execute(
            """
            UPDATE observations
               SET origin_class = (
                   SELECT s.origin_class FROM cc_sessions s
                    WHERE s.id = substr(observations.source, 9)
               )
             WHERE origin_class IS NULL
               AND source LIKE 'session:%'
               AND (
                   SELECT s.origin_class FROM cc_sessions s
                    WHERE s.id = substr(observations.source, 9)
               ) IS NOT NULL
            """
        )

    # 2. All other sources: classify via the shared env-free classifier. Only
    #    apply a DEFINITE (non-None) origin; unknown sources stay NULL.
    from genesis.memory.provenance import _origin_from_source

    cursor = await db.execute(
        "SELECT DISTINCT source FROM observations "
        "WHERE origin_class IS NULL AND source IS NOT NULL "
        "AND source NOT LIKE 'session:%'"
    )
    sources = [row[0] for row in await cursor.fetchall()]
    for src in sources:
        origin = _origin_from_source(src)
        if origin is None:
            continue  # fail-closed: leave unknown-source rows NULL
        await db.execute(
            "UPDATE observations SET origin_class = ? WHERE origin_class IS NULL AND source = ?",
            (origin, src),
        )


async def down(db: aiosqlite.Connection) -> None:
    # A backfill is not cleanly reversible — a stamped origin is
    # indistinguishable from one the write path set. No-op.
    return
