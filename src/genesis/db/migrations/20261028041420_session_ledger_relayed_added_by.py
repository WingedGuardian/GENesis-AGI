"""Widen session_ledger.added_by to admit 'relayed'.

'relayed' means "the owner's own request, written by a dispatched session": a
channel conversation (Telegram, say) relaying something the owner asked for
onto a foreground session's charter. Before this value existed such a row was
written as 'ambient' -- the value `_default_added_by()` stamps on any
dispatched session -- which made the owner's commitment indistinguishable from
background noise, and kept it out of the undisposed-ledger escalation sweep
(foreground-only by default). `added_by` was answering "who wrote the row"
when the sweep needs "whose commitment is it".

SQLite cannot ALTER a CHECK constraint, so this is a table rebuild following
SQLite's generalized ALTER TABLE procedure (lang_altertable.html, "Making Other
Kinds Of Table Schema Changes"). The guards are adapted from the reviewed
update_history rebuild (20261018175924_update_history_crashed_recovered_status)
and copied, not imported: a migration is a frozen snapshot.

ONE deliberate difference from both earlier rebuilds: the new table is created
from the LIVE table's own stored DDL with only the added_by value list
rewritten, rather than from a hardcoded CREATE. Column list, order, defaults,
constraints and any fork-added column therefore carry over exactly, so there is
no "column the template does not know" to refuse or reconstruct.

Every refusal raises BEFORE anything is changed, and the runner rolls the whole
migration back:
- a stored DDL this rewrite does not recognise (no CREATE TABLE session_ledger
  header, not exactly one added_by CHECK, or a comment inside its value list);
- with foreign_keys=ON, a table declaring a FOREIGN KEY to session_ledger: the
  DROP TABLE would run an implicit DELETE that fails on, or cascades into, its
  rows;
- an object already holding the scratch name session_ledger_new.

Rows are copied with their rowid. Indexes on the table, and EVERY view and
trigger in the schema (ALTER ... RENAME reparses the whole schema, so a view or
another table's trigger naming session_ledger would abort it), are dropped
first where needed and recreated after the rename.

Idempotent: keyed on the CHECK's parsed value set -- the thing this migration
changes -- never on a proxy (the 0007/0012 lesson in the precedent's docstring).
No db.commit(): the runner owns the transaction.
"""

from __future__ import annotations

import re

import aiosqlite

_TABLE = "session_ledger"
_SCRATCH_TABLE = "session_ledger_new"
_NEW_VALUE = "relayed"

# The added_by CHECK's value list. Same shape the precedent's drift tests parse.
_CHECK_RE = re.compile(r"(CHECK\s*\(\s*added_by\s+IN\s*\()([^)]*)(\)\s*\))", re.I | re.S)

# The CREATE TABLE header. SQLite stores the statement without any IF NOT
# EXISTS clause; after a RENAME it stores the name QUOTED
# (`CREATE TABLE "session_ledger" (`), which is the shape a migrated install
# carries. The other quoting styles are accepted for completeness.
_HEADER_RE = re.compile(
    r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:\"session_ledger\"|`session_ledger`|\[session_ledger\]|session_ledger)\s*\(",
    re.I,
)


def _values(check_body: str) -> list[str]:
    return [v.strip().strip("'\"") for v in check_body.split(",") if v.strip()]


async def _live_ddl(db: aiosqlite.Connection) -> str | None:
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='session_ledger'"
    )
    row = await cursor.fetchone()
    return (row[0] or "") if row else None


def _the_check(ddl: str) -> re.Match[str]:
    checks = list(_CHECK_RE.finditer(ddl))
    if (
        len(checks) != 1
        or not _HEADER_RE.match(ddl)
        or "--" in checks[0].group(2)
        or "/*" in checks[0].group(2)
    ):
        raise RuntimeError(
            "session_ledger rebuild: the stored table DDL is not a shape this "
            "migration can rewrite (expected one CREATE TABLE session_ledger "
            f"header and exactly one comment-free added_by CHECK, found "
            f"{len(checks)} CHECK(s)). Nothing was changed."
        )
    return checks[0]


async def _refuse_unsafe_rebuild(db: aiosqlite.Connection) -> None:
    # The documented procedure disables foreign keys BEFORE the transaction,
    # which a migration cannot do (the runner owns it, and PRAGMA foreign_keys
    # is a no-op inside one). With them on, DROP TABLE cascades into, or fails
    # on, any child row, so refuse up front naming the referencing table. The
    # canonical schema has none; any such table is one an install added.
    cursor = await db.execute("PRAGMA foreign_keys")
    if (await cursor.fetchone())[0]:
        cursor = await db.execute(
            "SELECT DISTINCT m.name FROM sqlite_master AS m, "
            "pragma_foreign_key_list(m.name) AS f "
            "WHERE m.type = 'table' AND lower(f.\"table\") = 'session_ledger'"
        )
        referencing = [r[0] for r in await cursor.fetchall()]
        if referencing:
            raise RuntimeError(
                f"session_ledger rebuild: table(s) {referencing} declare a FOREIGN "
                "KEY to session_ledger; with foreign_keys=ON the rebuild's DROP "
                "TABLE would delete or block on their rows. Apply this migration "
                "with foreign_keys=OFF. Nothing was changed."
            )

    # Names are case-insensitive, and a TEMP object of that name would shadow
    # the unqualified references below, so both schemas are checked. The runner
    # rolls a failed attempt of ours back, so nothing of ours can hold it.
    cursor = await db.execute(
        "SELECT type, name FROM sqlite_master WHERE lower(name) = ? "
        "UNION ALL SELECT type, name FROM sqlite_temp_master WHERE lower(name) = ?",
        (_SCRATCH_TABLE, _SCRATCH_TABLE),
    )
    squatter = await cursor.fetchone()
    if squatter:
        raise RuntimeError(
            f"session_ledger rebuild: a {squatter[0]} named {squatter[1]!r} already "
            f"exists and this migration needs the name {_SCRATCH_TABLE!r}. It is "
            "not the migration's to drop: rename or remove it, then re-run. "
            "Nothing was changed."
        )


async def up(db: aiosqlite.Connection) -> None:
    ddl = await _live_ddl(db)
    if ddl is None:
        # Table absent: a fresh install creates it from _tables.py, which
        # already carries the widened constraint.
        return
    check = _the_check(ddl)
    if _NEW_VALUE in _values(check.group(2)):
        return  # already widened (fresh install, or a prior run)

    await _refuse_unsafe_rebuild(db)

    widened = (
        ddl[: check.start()]
        + check.group(1)
        + check.group(2).rstrip()
        + f",'{_NEW_VALUE}'"
        + check.group(3)
        + ddl[check.end() :]
    )
    new_ddl = _HEADER_RE.sub(f"CREATE TABLE {_SCRATCH_TABLE} (", widened, count=1)

    # Insertable columns: table_info omits generated columns, which an INSERT
    # cannot name. Same DDL on both sides, so the lists are identical.
    cursor = await db.execute(f"PRAGMA table_info({_TABLE})")
    collist = ", ".join('"' + r[1].replace('"', '""') + '"' for r in await cursor.fetchall())

    # Autoindexes (sql IS NULL) come back with the table's own constraints.
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND tbl_name='session_ledger' AND sql IS NOT NULL ORDER BY rowid"
    )
    live_indexes = [r[0] for r in await cursor.fetchall()]
    cursor = await db.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='view' AND sql IS NOT NULL ORDER BY rowid"
    )
    live_views = list(await cursor.fetchall())
    cursor = await db.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL "
        "ORDER BY rowid"
    )
    live_triggers = list(await cursor.fetchall())

    await db.execute(new_ddl)
    # Identifiers come from PRAGMA output; no value is interpolated.
    await db.execute(
        f"INSERT INTO {_SCRATCH_TABLE} (rowid, {collist}) "  # noqa: S608
        f"SELECT rowid, {collist} FROM {_TABLE}"
    )
    for name, _sql in reversed(live_triggers):
        await db.execute('DROP TRIGGER IF EXISTS "' + name.replace('"', '""') + '"')
    for name, _sql in reversed(live_views):
        await db.execute('DROP VIEW "' + name.replace('"', '""') + '"')
    await db.execute(f"DROP TABLE {_TABLE}")
    await db.execute(f"ALTER TABLE {_SCRATCH_TABLE} RENAME TO {_TABLE}")
    for stmt in live_indexes:
        await db.execute(stmt)
    for _name, stmt in live_views:
        await db.execute(stmt)
    for _name, stmt in live_triggers:
        await db.execute(stmt)

    # A rewrite that misfired but still parsed must not pass as applied.
    after = await _live_ddl(db) or ""
    if _NEW_VALUE not in _values(_the_check(after).group(2)):
        raise RuntimeError(
            "session_ledger rebuild: the rebuilt table does not admit "
            f"{_NEW_VALUE!r}; rolling back."
        )
