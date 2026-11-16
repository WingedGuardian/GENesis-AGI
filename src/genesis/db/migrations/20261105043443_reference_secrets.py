"""reference_secrets: where a credential reference's value is held (#2426).

WHY THIS TABLE EXISTS. A credential reference's body is indexed by every
recall surface (knowledge_units + knowledge_fts, memory_fts, the vector payload
and its embedding). The value now lives here instead, with the free-text
description beside it (``detail``), and the body carries a placeholder line for
each. ``memory/reference_ops.resolve_value`` is the only reader.

WHY NOT AN EXISTING STORE (the New-Store Gate). A knowledge_units value column
would ride into every recall row, because recall reads that table with SELECT *;
the unit carries only ``secret_ref``, a random id naming its row here.
``credential_access_log`` is an append-only audit trail that outlives the units
it names. Consistency: rows are insert-only, written by ``crud/knowledge.upsert``
under a fresh random id just before ONE statement writes the unit naming it.
Retention: a row dies when no unit names it (the triggers below), and
``crud/knowledge.sweep_orphan_secrets`` removes the ones an interrupted upsert
left unnamed. Backup: rides genesis.db.

THE TRIGGERS. The ``knowledge_units_secret_ref_*`` triggers refuse a credential
row whose secret_ref names no stored secret, and delete the secret a unit stops
naming (repointed or deleted), unless another unit still names it. The two
``knowledge_units_credential_body_*`` triggers refuse a credential row whose
body lacks the placeholder line, so a process still running code from before
this change cannot index a value while it waits for its restart. Both UPDATE
guards fire only on the columns that decide them, so rows written before this
change (value still inline, secret_ref NULL) keep updating their counters.

Self-contained: the standalone runner applies this without
``create_all_tables``. Fresh installs get the identical DDL from
``schema/_tables.py`` and ``schema/_migrations.py`` (which builds it with
``crud/knowledge.secret_ref_trigger_ddl`` and ``credential_body_trigger_ddl``);
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
            id         TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            detail     TEXT,
            created_at TEXT NOT NULL
        )
    """,
}

INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_knowledge_units_secret_ref ON knowledge_units(secret_ref) "
    "WHERE secret_ref IS NOT NULL"
)

_WS = "' ' || char(9) || char(10) || char(13)"
_CREDENTIAL_ROW = (
    f"lower(trim(NEW.project_type, {_WS})) = 'reference' "
    f"AND lower(trim(NEW.domain, {_WS})) = 'reference.credentials'"
)
_MARKER = "char(10) || char(10) || 'Value: '"
_HELD = f"{_MARKER} || '{_PLACEHOLDER}'"
_BODY = "coalesce(NEW.body, '')"
_REFUSED = (
    f"WHEN {_CREDENTIAL_ROW} AND NOT ("
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

_REF_MESSAGE = "refusing a credential reference that names no stored secret (#2426)"
_GUARD = (
    f"WHEN {_CREDENTIAL_ROW} AND (NEW.secret_ref IS NULL OR NOT EXISTS "
    "(SELECT 1 FROM reference_secrets s WHERE s.id = NEW.secret_ref)) "
    f"BEGIN SELECT RAISE(ABORT, '{_REF_MESSAGE}'); END"
)
_RETIRE = (
    "BEGIN DELETE FROM reference_secrets WHERE id = OLD.secret_ref AND NOT EXISTS "
    "(SELECT 1 FROM knowledge_units k WHERE k.secret_ref = OLD.secret_ref); END"
)
SECRET_REF_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS knowledge_units_secret_ref_insert "
    f"BEFORE INSERT ON knowledge_units {_GUARD}",
    "CREATE TRIGGER IF NOT EXISTS knowledge_units_secret_ref_update "
    f"BEFORE UPDATE OF secret_ref, body, project_type, domain ON knowledge_units {_GUARD}",
    "CREATE TRIGGER IF NOT EXISTS knowledge_units_secret_ref_repoint "
    "AFTER UPDATE OF secret_ref ON knowledge_units "
    f"WHEN OLD.secret_ref IS NOT NULL AND OLD.secret_ref IS NOT NEW.secret_ref {_RETIRE}",
    "CREATE TRIGGER IF NOT EXISTS knowledge_units_secret_ref_delete "
    f"AFTER DELETE ON knowledge_units WHEN OLD.secret_ref IS NOT NULL {_RETIRE}",
)


async def up(db: aiosqlite.Connection) -> None:
    for ddl in TABLES.values():
        await db.execute(ddl)
    secret_columns = {
        r[1] for r in await db.execute_fetchall("PRAGMA table_info(reference_secrets)")
    }
    if "detail" not in secret_columns:
        # A table this migration created before the description was held out.
        await db.execute("ALTER TABLE reference_secrets ADD COLUMN detail TEXT")
    rows = await db.execute_fetchall(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_units'"
    )
    if not rows:
        # No knowledge_units yet: the schema init that creates it also creates
        # its secret_ref column and these triggers, from the same DDL.
        return
    columns = {r[1] for r in await db.execute_fetchall("PRAGMA table_info(knowledge_units)")}
    if "secret_ref" not in columns:
        await db.execute("ALTER TABLE knowledge_units ADD COLUMN secret_ref TEXT")
    await db.execute(INDEX)
    for ddl in (*SECRET_REF_TRIGGERS, *BODY_TRIGGERS):
        await db.execute(ddl)
