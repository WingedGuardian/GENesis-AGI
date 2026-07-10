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
_ENTITY_INDEX_PREFIXES = ("idx_entities_", "idx_entity_")


async def up(db: aiosqlite.Connection) -> None:
    from genesis.db.schema._tables import INDEXES, TABLES

    for name in _ENTITY_TABLES:
        await db.execute(TABLES[name])
    for ddl in INDEXES:
        if any(prefix in ddl for prefix in _ENTITY_INDEX_PREFIXES):
            await db.execute(ddl)


async def down(db: aiosqlite.Connection) -> None:
    for name in reversed(_ENTITY_TABLES):
        await db.execute(f"DROP TABLE IF EXISTS {name}")  # noqa: S608
