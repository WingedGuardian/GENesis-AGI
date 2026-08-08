"""Add MW-1 Tier-0 extraction judgment columns to ``memory_metadata``.

Three SEPARATE judgment axes, captured WRITE-ONLY at extraction time to cure the
"defaulting disease" (every memory landing as ``memory_class=fact`` with no
trust/durability/protection signal):

  - ``speech_act`` (+ ``speech_act_confidence``) — what KIND of utterance;
    drives existence-PROTECTION eligibility. Consumer: MW-5 (dream_shield).
  - ``assertion_provenance`` — WHO asserted it (user/external/self_inference);
    drives recall ranking WEIGHT. Consumer: MW-4. (Distinct from ``origin_class``,
    a pipeline-trust label that is ``first_party`` for all transcript extraction.)
  - ``durability`` + ``expires_at`` — permanent vs transient context; drives
    temporary-context TTL lifecycle. Consumer: MW-4.

All five columns are NULLable with NO default and NO backfill: expiry is strictly
OPT-IN (a memory expires only on an EXPLICIT ``durability='temporary'`` + a valid
elapsed ``expires_at``), so NULL/unclassified rows never expire — a wrong
"temporary" must never silently delete memories. NOTHING reads these columns yet;
MW-1 only writes them (# GROUNDWORK(mw-4-*) / (mw-5-*)).

Self-contained + idempotent: each ADD is PRAGMA-guarded (applies via the base
path OR the numbered runner; whichever ran first, the guard skips). No commit —
the runner owns the transaction. These columns are ALSO mirrored in
``_migrate_add_columns`` (guarded ``_try_alter``): create_all_tables runs that
function but NOT the numbered runner, so a create_all_tables→``MemoryStore.store``
INSERT on an existing DB (e.g. scripts/migrate_faiss_to_qdrant.py) needs the
columns present there too. Being unindexed only means the INDEXES-parity guard
cannot catch the omission — NOT that the mirror is unnecessary. schema_both_build_paths.

(Numbered 0079: 0078 is the highest present; the runner applies by per-id tracking
and only duplicate prefixes are fatal.)
"""

from __future__ import annotations

import aiosqlite

_MEMORY_METADATA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("speech_act", "TEXT"),
    ("speech_act_confidence", "REAL"),
    ("assertion_provenance", "TEXT"),
    ("durability", "TEXT"),
    ("expires_at", "TEXT"),
)


async def _add_column(db: aiosqlite.Connection, table: str, col: str, decl: str) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    if not await cursor.fetchone():
        return  # fresh DB — CREATE TABLE already carries the column
    cursor = await db.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in await cursor.fetchall()}
    if col not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


async def up(db: aiosqlite.Connection) -> None:
    for col, decl in _MEMORY_METADATA_COLUMNS:
        await _add_column(db, "memory_metadata", col, decl)


async def down(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_metadata'"
    )
    if not await cursor.fetchone():
        return
    cursor = await db.execute("PRAGMA table_info(memory_metadata)")
    cols = {row[1] for row in await cursor.fetchall()}
    for col, _decl in _MEMORY_METADATA_COLUMNS:
        if col in cols:
            await db.execute(f"ALTER TABLE memory_metadata DROP COLUMN {col}")
