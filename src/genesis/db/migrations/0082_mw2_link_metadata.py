"""Add MW-2 edge-metadata columns to ``memory_links``.

Five NULLable columns — the stamping location for relationship-classifier
verdicts (the coarse judgment function shipped by the MW-2 lean keystone):

  - ``proposed_type`` — the classifier's verdict label (its vocabulary, e.g.
    ``duplicate``/``succeeded_by`` — NOT constrained to the link_type CHECK).
  - ``confidence`` — classifier confidence 0..1 (NULL = never classified).
  - ``classifier`` — provenance of the judgment: ``'llm:<call_site>'`` /
    ``'entity_scan'`` / NULL (legacy, never judged).
  - ``review_state`` — ``'classified'`` / ``'confirmed'`` / NULL (legacy edge,
    its ``link_type`` stands as written).
  - ``safe_for_boost`` — INTEGER bool; **NULL means boost-eligible** (legacy
    default — the 212k existing edges keep today's behavior; only a future
    explicit ``0`` would gate an edge out of content expansion).

All five are NULLable with NO default and NO backfill. NOTHING reads or writes
them in production yet (# GROUNDWORK(mw-5-merge-gate)): MW-5's merge gate stamps
verdicts here; the deferred MW-2b (stored-graph reclassification + boost gating)
was measured 2026-08-11 as not-currently-justified (probe over the full
~197k-pair similarity population, visibility-filtered: 73.2% benign-distinct;
unsafe slice mostly harmless duplicates — see
``~/.genesis/output/mw2_classifier_probe_20260811_045500.json``).

Deliberately NO CHECK change and NO table rebuild: the link_type allowlist stays
the 12 types (locked by test_migration_0082's CHECK-unchanged test).

Self-contained + idempotent: each ADD is PRAGMA-guarded (applies via the base
path OR the numbered runner; whichever ran first, the guard skips). No commit —
the runner owns the transaction. These columns are ALSO mirrored in
``_migrate_add_columns`` (guarded ``_try_alter``): create_all_tables runs that
function but NOT the numbered runner (schema_both_build_paths).
"""

from __future__ import annotations

import aiosqlite

_MEMORY_LINKS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("proposed_type", "TEXT"),
    ("confidence", "REAL"),
    ("classifier", "TEXT"),
    ("review_state", "TEXT"),
    ("safe_for_boost", "INTEGER"),
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
    for col, decl in _MEMORY_LINKS_COLUMNS:
        await _add_column(db, "memory_links", col, decl)


async def down(db: aiosqlite.Connection) -> None:
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_links'"
    )
    if not await cursor.fetchone():
        return
    cursor = await db.execute("PRAGMA table_info(memory_links)")
    cols = {row[1] for row in await cursor.fetchall()}
    for col, _decl in _MEMORY_LINKS_COLUMNS:
        if col in cols:
            await db.execute(f"ALTER TABLE memory_links DROP COLUMN {col}")
