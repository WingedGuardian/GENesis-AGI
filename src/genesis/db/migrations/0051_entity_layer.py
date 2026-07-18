"""Entity layer substrate — entities, entity_mentions, entity_links.

WS-H Pillar 2 (Graphiti blueprint on the SQLite+Qdrant substrate):
typed entity nodes, memory↔entity mentions with provenance tags
(EXTRACTED/INFERRED/AMBIGUOUS), and bi-temporal entity↔entity relations
with an open link_type vocabulary (LLM-first; deliberately NOT the
memory_links CHECK registry). Canonical DDL lives in
``db/schema/_tables.py``; this migration brings existing DBs to the
same shape. Additive + idempotent.
"""

from __future__ import annotations

import aiosqlite

_ENTITY_TABLES = ("entities", "entity_mentions", "entity_links")
# Pinned historical snapshot — the exact indexes this migration created at its
# point in history. Do NOT prefix-scan the live ``INDEXES`` list: a later
# ``idx_entity_*`` index on a table THIS migration does not create (e.g.
# ``idx_entity_adjud_verdict`` on ``entity_adjudications``, migration 0065) would
# be swept in and fail with "no such table". Matches the 0033 explicit-DDL pattern.
_ENTITY_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_entities_norm ON entities(norm_name)",
    "CREATE INDEX IF NOT EXISTS idx_entity_mentions_entity ON entity_mentions(entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_entity_links_target ON entity_links(target_id)",
)


async def up(db: aiosqlite.Connection) -> None:
    from genesis.db.schema._tables import TABLES

    for name in _ENTITY_TABLES:
        await db.execute(TABLES[name])
    for ddl in _ENTITY_INDEXES:
        await db.execute(ddl)


async def down(db: aiosqlite.Connection) -> None:
    for name in reversed(_ENTITY_TABLES):
        await db.execute(f"DROP TABLE IF EXISTS {name}")  # noqa: S608
