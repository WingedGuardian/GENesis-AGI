"""reference_secrets: where a credential reference's value is held (#2426).

WHY THIS TABLE EXISTS. A credential reference's body is indexed by every
recall surface (knowledge_units + knowledge_fts, memory_fts, the vector payload
and its embedding). The value now lives here instead, and the body carries a
placeholder line. ``memory/reference_ops.resolve_value`` is the only reader.

WHY NOT AN EXISTING STORE (the New-Store Gate). A knowledge_units column would
ride into every recall row, because recall reads that table with SELECT *.
``credential_access_log`` is an append-only audit trail that outlives the units
it names. Consistency: one row per credential unit, keyed by its id, written
before the unit's body by ``crud/knowledge.upsert`` and retired with the unit
by ``reference_secrets_retire``. Retention: none beyond that. Backup: rides
genesis.db.

THE TRIGGERS. ``reference_secrets_retire`` deletes the value when its unit is
deleted by any path. The two ``knowledge_units_credential_body_*`` triggers
refuse a credential row whose body lacks the placeholder line, so a process
still running code from before this change cannot index a value while it waits
for its restart. UPDATE fires only on body, project_type and domain, so rows
written before this change (value still inline) keep updating their counters.

Self-contained: the standalone runner applies this without
``create_all_tables``. Fresh installs get the identical DDL from
``schema/_tables.py`` and ``schema/_migrations.py``;
``tests/test_db/test_reference_secrets_migration.py`` pins the paths in parity.
Additive and idempotent.
"""

from __future__ import annotations

import aiosqlite

_PLACEHOLDER = "[held in the reference secret store; read it with reference_lookup]"
_MARKER_LEN = 9  # the "\n\nValue: " marker
_HELD_LEN = _MARKER_LEN + len(_PLACEHOLDER)

TABLES = {
    "reference_secrets": """
        CREATE TABLE IF NOT EXISTS reference_secrets (
            unit_id    TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """,
}

_RETIRE = """
        CREATE TRIGGER IF NOT EXISTS reference_secrets_retire
        AFTER DELETE ON knowledge_units
        BEGIN
            DELETE FROM reference_secrets WHERE unit_id = OLD.id;
        END
    """

_WS = "' ' || char(9) || char(10) || char(13)"
_MARKER = "char(10) || char(10) || 'Value: '"
_HELD = f"{_MARKER} || '{_PLACEHOLDER}'"
_BODY = "coalesce(NEW.body, '')"
_REFUSED = (
    f"WHEN lower(trim(NEW.project_type, {_WS})) = 'reference' "
    f"AND lower(trim(NEW.domain, {_WS})) = 'reference.credentials' AND NOT ("
    f"instr({_BODY}, {_HELD}) > 0 "
    f"AND length({_BODY}) - length(replace({_BODY}, {_MARKER}, '')) = {_MARKER_LEN} "
    f"AND substr({_BODY}, instr({_BODY}, {_HELD}) + {_HELD_LEN}, 1) IN ('', char(10))"
    ")"
)
_MESSAGE = (
    "refusing a credential reference whose body carries its value: write it "
    "through ingest_knowledge_unit (#2426)"
)
BODY_TRIGGERS = tuple(
    f"CREATE TRIGGER IF NOT EXISTS knowledge_units_credential_body_{name} "
    f"BEFORE {event} ON knowledge_units {_REFUSED} "
    f"BEGIN SELECT RAISE(ABORT, '{_MESSAGE}'); END"
    for name, event in (
        ("insert", "INSERT"),
        ("update", "UPDATE OF body, project_type, domain"),
    )
)


async def up(db: aiosqlite.Connection) -> None:
    for ddl in TABLES.values():
        await db.execute(ddl)
    rows = await db.execute_fetchall(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_units'"
    )
    if not rows:
        # No knowledge_units yet: the schema init that creates it also creates
        # these triggers, from the same DDL.
        return
    await db.execute(_RETIRE)
    for ddl in BODY_TRIGGERS:
        await db.execute(ddl)
