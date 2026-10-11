"""Add ``inbox_items.status = 'superseded'`` and reclassify superseded rows.

A parked inbox row whose file snapshot was replaced before it dispatched is not
a failure: a newer drop carries a superset of its content. Such rows were
written as ``status='failed'`` with an ``approval_invalidated:`` marker, so they
dominated every "failed" count and hid real failures among them.

SQLite cannot ALTER a CHECK constraint, so widening the ``status`` CHECK needs a
full table rebuild: create ``inbox_items_new`` with the widened CHECK, copy every
row (``rowid`` included — ``get_all_known`` breaks ``created_at`` ties on rowid,
so renumbering would change which row supplies a file's known hash), drop the
original, rename into place, and recreate every index the live table carried
plus the canonical set.

Backfill: rows written by the three supersession writers, current and retired —
``approval_invalidated:superseded by newer modification`` (current),
``approval_invalidated:superseded by new inbox scan`` (the pre-idempotent-approval
writer, same meaning: a newer scan replaced a parked row; no current writer) and
``approval_invalidated:content changed`` (a changed file invalidating a parked
row) — move from ``failed`` to ``superseded``. ``error_message`` is kept for
audit. Other ``approval_invalidated:`` reasons (approval terminal, source file
deleted/vanished, content removed before retry) are left as ``failed``.

Self-contained (no genesis imports) and idempotent: the rebuild is skipped when
the live DDL already allows ``'superseded'`` (fresh DBs get it from the canonical
CREATE in ``db/schema/_tables.py``); the backfill is a guarded UPDATE that
matches nothing on a second run. A live column this rebuild target does not
declare RAISES rather than being dropped — the runner rolls the whole migration
back. The rebuild also refuses, before changing anything, when an object named
``inbox_items_new`` already exists (it is the operator's, never the migration's
to drop) or when the live table carries a constraint the fixed rebuild template
does not produce (see ``_refuse_constraints_the_rebuild_would_drop``).
No ``db.commit()`` — the runner owns the transaction.
"""

from __future__ import annotations

import sqlite3

import aiosqlite

# Canonical inbox_items columns (must match _tables.py's CREATE); only the
# status CHECK differs between up() and down().
_COLUMNS_TEMPLATE = """
    id             TEXT PRIMARY KEY,
    file_path      TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ({status_values})
    ),
    batch_id       TEXT,
    response_path  TEXT,
    created_at     TEXT NOT NULL,
    processed_at   TEXT,
    error_message  TEXT,
    retry_count    INTEGER NOT NULL DEFAULT 0,
    evaluated_content TEXT,
    drop_id        TEXT,
    batch_items    TEXT
"""

_WIDE_STATUSES = "'pending', 'processing', 'completed', 'failed', 'superseded'"
_NARROW_STATUSES = "'pending', 'processing', 'completed', 'failed'"

_CANONICAL_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_inbox_items_status ON inbox_items(status)",
    "CREATE INDEX IF NOT EXISTS idx_inbox_items_file_path ON inbox_items(file_path)",
    "CREATE INDEX IF NOT EXISTS idx_inbox_items_batch_id ON inbox_items(batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_inbox_items_drop ON inbox_items(drop_id)",
)

# The exact error_message values that mean "replaced by a newer snapshot", not
# "failed" — every value any writer (current or retired) has produced. Matched
# with equality, never LIKE: '_' in the prefix is a LIKE wildcard.
_SUPERSEDED_REASONS = (
    "approval_invalidated:superseded by newer modification",
    "approval_invalidated:superseded by new inbox scan",
    "approval_invalidated:content changed",
)


_SCRATCH_TABLE = "inbox_items_new"


def _sql_tokens(sql: str) -> list[str]:
    """Split CREATE TABLE text into tokens, dropping comments and whitespace.

    Keywords and identifiers are lowercased; a quoted identifier ("x", `x`,
    [x]) becomes its bare lowercased name; a string literal is kept verbatim
    (quotes included), so text inside one never reads as a keyword. Anything the
    scanner cannot close (an unterminated quote or comment) raises: the caller
    treats that as "cannot verify" and refuses the rebuild.
    """
    tokens: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c.isspace():
            i += 1
        elif sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end == -1 else end + 1
        elif sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2  # SQLite accepts an unclosed trailing /*
        elif c == "'":
            j = i + 1
            while True:
                j = sql.find("'", j)
                if j == -1:
                    raise ValueError("unterminated string literal")
                if sql.startswith("''", j):
                    j += 2
                    continue
                break
            tokens.append(sql[i : j + 1])
            i = j + 1
        elif c in '"`[':
            close = "]" if c == "[" else c
            j = i + 1
            parts = []
            while True:
                k = sql.find(close, j)
                if k == -1:
                    raise ValueError("unterminated quoted identifier")
                parts.append(sql[j:k])
                if close != "]" and sql.startswith(close * 2, k):
                    parts.append(close)
                    j = k + 2
                    continue
                break
            tokens.append("".join(parts).lower())
            i = k + 1
        elif c.isalnum() or c == "_" or c == "$" or ord(c) > 127:
            j = i
            while j < n and (sql[j].isalnum() or sql[j] in "_$" or ord(sql[j]) > 127):
                j += 1
            tokens.append(sql[i:j].lower())
            i = j
        else:
            tokens.append(c)
            i += 1
    return tokens


def _ddl_features(sql: str) -> tuple[list[tuple[str, ...]], list[str]]:
    """Return (CHECK expressions, other clause keywords) found in a CREATE TABLE.

    CHECK expressions are token tuples of the parenthesised body. The other list
    names every COLLATE / ON CONFLICT / AUTOINCREMENT clause, plus any table
    option after the column list (STRICT, WITHOUT ROWID) — the constraint kinds
    no PRAGMA reports.
    """
    toks = _sql_tokens(sql)
    checks: list[tuple[str, ...]] = []
    other: list[str] = []
    depth = 0
    body_closed_at = None
    for idx, tok in enumerate(toks):
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
            if depth == 0 and body_closed_at is None:
                body_closed_at = idx
        elif tok == "check" and idx + 1 < len(toks) and toks[idx + 1] == "(":
            level, j = 0, idx + 1
            while j < len(toks):
                level += toks[j] == "("
                level -= toks[j] == ")"
                if level == 0:
                    break
                j += 1
            checks.append(tuple(toks[idx + 1 : j + 1]))
        elif tok == "collate" and idx + 1 < len(toks):
            other.append(f"COLLATE {toks[idx + 1]}")
        elif tok == "conflict":
            other.append("ON CONFLICT")
        elif tok == "autoincrement":
            other.append("AUTOINCREMENT")
    if body_closed_at is None:
        raise ValueError("no closed column list")
    trailing = [t for t in toks[body_closed_at + 1 :] if t not in (",", ";")]
    if trailing:
        other.append("table option " + " ".join(trailing).upper())
    return checks, other


def _canonical_shape(status_values: str) -> dict:
    """What the rebuild target declares, measured from a private in-memory copy."""
    sql = f"CREATE TABLE inbox_items ({_COLUMNS_TEMPLATE.format(status_values=status_values)})"
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(sql)
        cols = {
            r[1]: (str(r[2]).upper(), r[3], r[4])
            for r in conn.execute("PRAGMA table_xinfo(inbox_items)")
        }
        pk = [
            r[1]
            for r in sorted(conn.execute("PRAGMA table_xinfo(inbox_items)"), key=lambda r: r[5])
            if r[5]
        ]
    finally:
        conn.close()
    checks, _other = _ddl_features(sql)
    return {"cols": cols, "pk": pk, "checks": checks}


async def _refuse_constraints_the_rebuild_would_drop(db: aiosqlite.Connection, ddl: str) -> None:
    """Raise if the live table carries a constraint the fixed template lacks.

    The rebuild recreates inbox_items from _COLUMNS_TEMPLATE, so anything an
    install added to the table definition itself would be silently lost.
    Reconstructing arbitrary DDL is out of scope; refusing is not. Coverage of
    SQLite's constraint kinds:

    - PRIMARY KEY (column or table)   -> table_xinfo pk columns must equal the target's
    - UNIQUE (column or table)        -> index_list origin 'u' (its autoindex has NULL
                                         sql, so the index copy would miss it)
    - FOREIGN KEY / REFERENCES        -> foreign_key_list must be empty (target has none)
    - CHECK (column or table)         -> every CHECK in the live DDL must be one the
                                         narrow or wide target declares (the status CHECK)
    - NOT NULL, DEFAULT, declared type -> table_xinfo, per shared column
    - COLLATE, ON CONFLICT, AUTOINCREMENT, STRICT / WITHOUT ROWID -> DDL scan (no
                                         PRAGMA reports them; the target has none)
    - GENERATED columns               -> table_xinfo hidden flag (a canonical name) and
                                         the column drift check in _rebuild (a new name)
    PK ASC/DESC is not compared: it orders the autoindex only, with no effect on
    which rows the key admits.
    Explicit CREATE [UNIQUE] INDEX objects are not constraints of the table
    definition: they are copied verbatim by _rebuild.
    """
    found: list[str] = []
    try:
        live_checks, live_other = _ddl_features(ddl)
    except ValueError as exc:
        raise RuntimeError(
            f"inbox_items rebuild: cannot read the live table definition ({exc}); "
            "refusing to rebuild a table whose constraints it cannot verify."
        ) from exc
    found += live_other

    allowed_checks = {
        c for sv in (_NARROW_STATUSES, _WIDE_STATUSES) for c in _canonical_shape(sv)["checks"]
    }
    for chk in live_checks:
        if chk not in allowed_checks:
            found.append("CHECK " + " ".join(chk))

    target = _canonical_shape(_WIDE_STATUSES)
    cursor = await db.execute("PRAGMA table_xinfo(inbox_items)")
    xinfo = list(await cursor.fetchall())
    live_pk = [r[1] for r in sorted(xinfo, key=lambda r: r[5]) if r[5]]
    if live_pk and live_pk != target["pk"]:
        found.append(f"PRIMARY KEY ({', '.join(live_pk)})")
    for r in xinfo:
        name, ctype, notnull, dflt = r[1], str(r[2]).upper(), r[3], r[4]
        if r[6]:
            # A generated column reports a plain declared type; only the hidden
            # flag (2 virtual, 3 stored) shows the expression the rebuild would
            # lose. Unknown names are also caught by the drift check, but one
            # shadowing a canonical name is caught only here.
            found.append(f"column {name} GENERATED (hidden={r[6]})")
        want = target["cols"].get(name)
        if want is None:
            continue  # unknown columns are reported by the drift check
        if ctype != want[0]:
            found.append(f"column {name} declared type {ctype} (target {want[0]})")
        if notnull and not want[1]:
            found.append(f"column {name} NOT NULL")
        if dflt is not None and dflt != want[2]:
            found.append(f"column {name} DEFAULT {dflt}")

    cursor = await db.execute("PRAGMA index_list(inbox_items)")
    for row in await cursor.fetchall():
        if row[3] == "u":
            cur2 = await db.execute(f'PRAGMA index_info("{row[1].replace(chr(34), chr(34) * 2)}")')
            cols = [c[2] for c in await cur2.fetchall()]
            found.append(f"UNIQUE ({', '.join(cols)})")
    cursor = await db.execute("PRAGMA foreign_key_list(inbox_items)")
    for row in await cursor.fetchall():
        if row[1] == 0:  # seq 0 = first column of each constraint
            found.append(f"FOREIGN KEY ({row[3]}) REFERENCES {row[2]}")

    if found:
        raise RuntimeError(
            "inbox_items rebuild: the live table carries constraint(s) the rebuild "
            f"template does not produce and would silently drop: {found}. This "
            "migration will not reconstruct install-local DDL. Remove them (or "
            "recreate inbox_items with the canonical DDL) and re-run; nothing was changed."
        )


async def _live_ddl(db: aiosqlite.Connection) -> str | None:
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='inbox_items'"
    )
    row = await cursor.fetchone()
    return (row[0] or "") if row else None


async def _rebuild(db: aiosqlite.Connection, *, status_values: str) -> None:
    """Rebuild inbox_items with the given status CHECK, preserving rows/rowids/
    indexes/triggers/views.

    Follows SQLite's documented generalized ALTER TABLE procedure
    (lang_altertable.html, "Making Other Kinds Of Table Schema Changes"):
    create the new table under a temporary name, copy, drop, rename into place,
    then recreate the indexes, triggers and views the DROP removed or broke.
    The canonical schema has no view, trigger or foreign key touching
    inbox_items; every such object handled here is one an install added.
    """
    # Foreign keys: the procedure disables them BEFORE the transaction, which a
    # migration cannot do (the runner owns the transaction, and PRAGMA
    # foreign_keys is a no-op inside one). With them enabled, DROP TABLE runs an
    # implicit DELETE that fails on any child row, so refuse up front with a
    # message naming the referencing table rather than failing mid-rebuild.
    cursor = await db.execute("PRAGMA foreign_keys")
    if (await cursor.fetchone())[0]:
        cursor = await db.execute(
            "SELECT DISTINCT m.name FROM sqlite_master AS m, "
            "pragma_foreign_key_list(m.name) AS f "
            "WHERE m.type = 'table' AND lower(f.\"table\") = 'inbox_items'"
        )
        referencing = [r[0] for r in await cursor.fetchall()]
        if referencing:
            raise RuntimeError(
                f"inbox_items rebuild: table(s) {referencing} declare a FOREIGN KEY "
                "to inbox_items; with foreign_keys=ON the rebuild's DROP TABLE would "
                "fail. Apply this migration with foreign_keys=OFF."
            )

    # The scratch name is created here, so anything already holding it belongs
    # to the operator (a retained manual-recovery copy, say). Dropping it would
    # destroy their data and the migration would commit the loss: refuse
    # instead. Names are case-insensitive, and a TEMP object of that name would
    # shadow the unqualified references below, so both schemas are checked.
    cursor = await db.execute(
        "SELECT type, name FROM sqlite_master WHERE lower(name) = ? "
        "UNION ALL SELECT type, name FROM sqlite_temp_master WHERE lower(name) = ?",
        (_SCRATCH_TABLE, _SCRATCH_TABLE),
    )
    squatter = await cursor.fetchone()
    if squatter:
        raise RuntimeError(
            f"inbox_items rebuild: a {squatter[0]} named {squatter[1]!r} already exists "
            f"and this migration needs the name {_SCRATCH_TABLE!r} for its scratch table. "
            "It is not the migration's to drop: rename or remove it, then re-run. "
            "Nothing was changed."
        )

    await _refuse_constraints_the_rebuild_would_drop(db, await _live_ddl(db) or "")

    # Capture every explicit index on the live table so none is lost to the
    # DROP (autoindexes have NULL sql and are recreated by the PRIMARY KEY), and
    # EVERY view and trigger in the schema: ALTER TABLE ... RENAME reparses the
    # whole schema, and a view or trigger naming inbox_items — directly, through
    # another view, or from a trigger attached to some other table — fails that
    # reparse while the table is absent. Dropping a view also drops its INSTEAD
    # OF triggers. Neither holds data, so all of them are dropped first and
    # recreated after the rename in creation order (views before triggers, since
    # a trigger may name a view).
    cursor = await db.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='view' AND sql IS NOT NULL ORDER BY rowid"
    )
    live_views = list(await cursor.fetchall())
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND tbl_name='inbox_items' AND sql IS NOT NULL"
    )
    live_indexes = [r[0] for r in await cursor.fetchall()]
    cursor = await db.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL "
        "ORDER BY rowid"
    )
    live_triggers = list(await cursor.fetchall())

    await db.execute(
        f"CREATE TABLE inbox_items_new ({_COLUMNS_TEMPLATE.format(status_values=status_values)})"
    )

    # table_xinfo, not table_info: table_info omits generated columns, which
    # would let an install-added generated column pass the drift check and be
    # silently dropped. A generated column under a NEW name is reported as drift
    # below; one reusing a canonical name was already refused by the hidden-flag
    # check in _refuse_constraints_the_rebuild_would_drop.
    cursor = await db.execute("PRAGMA table_xinfo(inbox_items)")
    src_cols = [r[1] for r in await cursor.fetchall()]
    cursor = await db.execute("PRAGMA table_xinfo(inbox_items_new)")
    dst_cols = {r[1] for r in await cursor.fetchall()}
    dropped = [c for c in src_cols if c not in dst_cols]
    if dropped:
        raise RuntimeError(
            f"inbox_items rebuild drift: live column(s) {dropped} are not in the "
            "rebuild target; refusing to drop their data. Add them to "
            "_COLUMNS_TEMPLATE to match the canonical _tables.py DDL."
        )
    collist = ", ".join(c for c in src_cols if c in dst_cols)
    # Identifiers come from PRAGMA / in-repo literals, never user input.
    await db.execute(
        f"INSERT INTO inbox_items_new (rowid, {collist}) "  # noqa: S608
        f"SELECT rowid, {collist} FROM inbox_items"
    )

    for name, _sql in reversed(live_triggers):
        quoted = name.replace('"', '""')
        await db.execute(f'DROP TRIGGER IF EXISTS "{quoted}"')
    for name, _sql in reversed(live_views):
        quoted = name.replace('"', '""')
        await db.execute(f'DROP VIEW "{quoted}"')
    await db.execute("DROP TABLE inbox_items")
    await db.execute("ALTER TABLE inbox_items_new RENAME TO inbox_items")
    for stmt in live_indexes:
        await db.execute(stmt)
    for stmt in _CANONICAL_INDEXES:
        await db.execute(stmt)
    for _name, stmt in live_views:
        await db.execute(stmt)
    for _name, stmt in live_triggers:
        await db.execute(stmt)


async def up(db: aiosqlite.Connection) -> None:
    ddl = await _live_ddl(db)
    if ddl is None:
        return  # bare DB (runner unit tests) — nothing to alter
    if "'superseded'" not in ddl:
        await _rebuild(db, status_values=_WIDE_STATUSES)
    await db.execute(
        "UPDATE inbox_items SET status = 'superseded' "
        "WHERE status = 'failed' AND error_message IN (?, ?, ?)",
        _SUPERSEDED_REASONS,
    )


async def down(db: aiosqlite.Connection) -> None:
    ddl = await _live_ddl(db)
    if ddl is None or "'superseded'" not in ddl:
        return
    # Preserve data: superseded rows return to the pre-migration encoding.
    await db.execute("UPDATE inbox_items SET status = 'failed' WHERE status = 'superseded'")
    await _rebuild(db, status_values=_NARROW_STATUSES)
