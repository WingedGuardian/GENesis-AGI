"""Add 'crashed_recovered' to the update_history status CHECK constraint.

bootstrap.sh records the crash recovery of an interrupted update with
``status = 'crashed_recovered'``, but after 0024 the CHECK allowed only
('success', 'failed', 'rolled_back', 'conflicts_pending'). The INSERT raised
IntegrityError, so a recovered crash was never recorded.

SQLite cannot ALTER a CHECK constraint, so this is a full table rebuild,
following SQLite's generalized ALTER TABLE procedure (lang_altertable.html,
"Making Other Kinds Of Table Schema Changes"): create ``update_history_new``
with the widened CHECK, copy every row (``rowid`` included), drop the original,
rename into place, and recreate the indexes, views and triggers the DROP removed
or would have broken. The canonical schema has no index, view, trigger or
foreign key touching update_history; every such object handled here is one an
install added.

The guard is adapted from the reviewed inbox_items rebuild
(20260926234429_inbox_items_superseded_status) and copied, not imported: a
migration is a frozen snapshot and must not depend on another migration module.
Every refusal raises BEFORE anything is changed:

- an object already holding the scratch name ``update_history_new`` (it is the
  operator's; 0024 dropped it, but the runner rolls a failed attempt of ours
  back, so nothing of ours can be holding it);
- a live column the rebuild target does not declare (its data would be lost);
- a constraint on the live table the fixed template does not produce (UNIQUE,
  an extra CHECK, DEFAULT, COLLATE, NOT NULL, a different PRIMARY KEY or
  declared type, a FOREIGN KEY, ON CONFLICT, AUTOINCREMENT, generated columns,
  STRICT / WITHOUT ROWID);
- with ``foreign_keys=ON``, a table declaring a FOREIGN KEY to update_history:
  DROP TABLE would run an implicit DELETE that fails or cascades into its rows.

Idempotent: skipped when the live table already carries this migration's
status CHECK (compared as parsed expressions, not as a substring).
No ``db.commit()`` — the runner owns the transaction.
"""

from __future__ import annotations

import sqlite3

import aiosqlite

_TABLE = "update_history"
_SCRATCH_TABLE = "update_history_new"

# Canonical update_history columns (0001, CHECK widened by 0024); only the
# status CHECK differs between up() and down().
_COLUMNS_TEMPLATE = """
    id TEXT PRIMARY KEY,
    old_tag TEXT NOT NULL,
    new_tag TEXT NOT NULL,
    old_commit TEXT NOT NULL,
    new_commit TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ({statuses})),
    rollback_tag TEXT,
    failure_reason TEXT,
    degraded_subsystems TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL
"""

_WIDE_STATUSES = "'success', 'failed', 'rolled_back', 'conflicts_pending', 'crashed_recovered'"
_NARROW_STATUSES = "'success', 'failed', 'rolled_back', 'conflicts_pending'"
# The pre-0024 constraint: a live table still carrying it is the canonical
# table at an older step, not an install-added CHECK.
_ORIGINAL_STATUSES = "'success', 'failed', 'rolled_back'"


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


def _canonical_shape(statuses: str) -> dict:
    """What the rebuild target declares, measured from a private in-memory copy."""
    sql = f"CREATE TABLE {_TABLE} ({_COLUMNS_TEMPLATE.format(statuses=statuses)})"
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(sql)
        xinfo = list(conn.execute(f"PRAGMA table_xinfo({_TABLE})"))
    finally:
        conn.close()
    cols = {r[1]: (str(r[2]).upper(), r[3], r[4]) for r in xinfo}
    pk = [r[1] for r in sorted(xinfo, key=lambda r: r[5]) if r[5]]
    checks, _other = _ddl_features(sql)
    return {"cols": cols, "pk": pk, "checks": checks}


async def _refuse_constraints_the_rebuild_would_drop(db: aiosqlite.Connection, ddl: str) -> None:
    """Raise if the live table carries a constraint the fixed template lacks.

    The rebuild recreates update_history from _COLUMNS_TEMPLATE, so anything an
    install added to the table definition itself would be silently lost.
    Reconstructing arbitrary DDL is out of scope; refusing is not. Coverage:

    - PRIMARY KEY (column or table)   -> table_xinfo pk columns must equal the target's
    - UNIQUE (column or table)        -> index_list origin 'u' (its autoindex has NULL
                                         sql, so the index copy would miss it)
    - FOREIGN KEY / REFERENCES        -> foreign_key_list must be empty (target has none)
    - CHECK (column or table)         -> every CHECK in the live DDL must be a status
                                         CHECK some canonical step declares
    - NOT NULL, DEFAULT, declared type -> table_xinfo, per shared column
    - COLLATE, ON CONFLICT, AUTOINCREMENT, STRICT / WITHOUT ROWID -> DDL scan
    - GENERATED columns               -> table_xinfo hidden flag
    Explicit CREATE [UNIQUE] INDEX objects are not constraints of the table
    definition: they are copied verbatim by _rebuild.
    """
    found: list[str] = []
    try:
        live_checks, live_other = _ddl_features(ddl)
    except ValueError as exc:
        raise RuntimeError(
            f"update_history rebuild: cannot read the live table definition ({exc}); "
            "refusing to rebuild a table whose constraints it cannot verify. "
            "Nothing was changed."
        ) from exc
    found += live_other

    allowed_checks = {
        c
        for sv in (_ORIGINAL_STATUSES, _NARROW_STATUSES, _WIDE_STATUSES)
        for c in _canonical_shape(sv)["checks"]
    }
    for chk in live_checks:
        if chk not in allowed_checks:
            found.append("CHECK " + " ".join(chk))

    target = _canonical_shape(_WIDE_STATUSES)
    cursor = await db.execute(f"PRAGMA table_xinfo({_TABLE})")
    xinfo = list(await cursor.fetchall())
    live_pk = [r[1] for r in sorted(xinfo, key=lambda r: r[5]) if r[5]]
    if live_pk and live_pk != target["pk"]:
        found.append(f"PRIMARY KEY ({', '.join(live_pk)})")
    for r in xinfo:
        name, ctype, notnull, dflt = r[1], str(r[2]).upper(), r[3], r[4]
        if r[6]:
            found.append(f"column {name} GENERATED (hidden={r[6]})")
        want = target["cols"].get(name)
        if want is None:
            continue  # unknown columns are refused separately by _rebuild
        if ctype != want[0]:
            found.append(f"column {name} declared type {ctype} (target {want[0]})")
        if notnull and not want[1]:
            found.append(f"column {name} NOT NULL")
        if dflt is not None and dflt != want[2]:
            found.append(f"column {name} DEFAULT {dflt}")

    cursor = await db.execute(f"PRAGMA index_list({_TABLE})")
    for row in await cursor.fetchall():
        if row[3] == "u":
            cur2 = await db.execute(f'PRAGMA index_info("{row[1].replace(chr(34), chr(34) * 2)}")')
            cols = [c[2] for c in await cur2.fetchall()]
            found.append(f"UNIQUE ({', '.join(cols)})")
    cursor = await db.execute(f"PRAGMA foreign_key_list({_TABLE})")
    for row in await cursor.fetchall():
        if row[1] == 0:  # seq 0 = first column of each constraint
            found.append(f"FOREIGN KEY ({row[3]}) REFERENCES {row[2]}")

    if found:
        raise RuntimeError(
            "update_history rebuild: the live table carries constraint(s) the rebuild "
            f"template does not produce and would silently drop: {found}. This "
            "migration will not reconstruct install-local DDL. Remove them (or "
            "recreate update_history with the canonical DDL) and re-run. "
            "Nothing was changed."
        )


async def _live_ddl(db: aiosqlite.Connection) -> str | None:
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='update_history'"
    )
    row = await cursor.fetchone()
    return (row[0] or "") if row else None


async def _rebuild(db: aiosqlite.Connection, *, statuses: str, remap_crashed: bool = False) -> None:
    """Rebuild update_history with the given status CHECK, preserving rows,
    rowids, indexes, views and triggers. Every refusal comes before any change."""
    # Foreign keys: the documented procedure disables them BEFORE the
    # transaction, which a migration cannot do (the runner owns the transaction,
    # and PRAGMA foreign_keys is a no-op inside one). With them enabled, DROP
    # TABLE runs an implicit DELETE that fails on, or cascades into, any child
    # row, so refuse up front naming the referencing table.
    cursor = await db.execute("PRAGMA foreign_keys")
    if (await cursor.fetchone())[0]:
        cursor = await db.execute(
            "SELECT DISTINCT m.name FROM sqlite_master AS m, "
            "pragma_foreign_key_list(m.name) AS f "
            "WHERE m.type = 'table' AND lower(f.\"table\") = 'update_history'"
        )
        referencing = [r[0] for r in await cursor.fetchall()]
        if referencing:
            raise RuntimeError(
                f"update_history rebuild: table(s) {referencing} declare a FOREIGN KEY "
                "to update_history; with foreign_keys=ON the rebuild's DROP TABLE would "
                "delete or block on their rows. Apply this migration with "
                "foreign_keys=OFF. Nothing was changed."
            )

    # Names are case-insensitive, and a TEMP object of that name would shadow
    # the unqualified references below, so both schemas are checked.
    cursor = await db.execute(
        "SELECT type, name FROM sqlite_master WHERE lower(name) = ? "
        "UNION ALL SELECT type, name FROM sqlite_temp_master WHERE lower(name) = ?",
        (_SCRATCH_TABLE, _SCRATCH_TABLE),
    )
    squatter = await cursor.fetchone()
    if squatter:
        raise RuntimeError(
            f"update_history rebuild: a {squatter[0]} named {squatter[1]!r} already "
            f"exists and this migration needs the name {_SCRATCH_TABLE!r} for its "
            "scratch table. It is not the migration's to drop: rename or remove it, "
            "then re-run. Nothing was changed."
        )

    # table_xinfo, not table_info: table_info omits generated columns.
    cursor = await db.execute(f"PRAGMA table_xinfo({_TABLE})")
    live_cols = [r[1] for r in await cursor.fetchall()]
    target_cols = _canonical_shape(statuses)["cols"]
    unknown = [c for c in live_cols if c not in target_cols]
    if unknown:
        raise RuntimeError(
            f"update_history rebuild: live column(s) {unknown} are not in the rebuild "
            "target; refusing to drop their data. Nothing was changed."
        )

    await _refuse_constraints_the_rebuild_would_drop(db, await _live_ddl(db) or "")

    # Capture every explicit index on the table (autoindexes have NULL sql and
    # come back with the PRIMARY KEY), and EVERY view and trigger in the schema:
    # ALTER TABLE ... RENAME reparses the whole schema, and a view or trigger
    # naming update_history (directly, through another view, or from a trigger
    # on some other table) fails that reparse while the table is absent. Neither
    # holds data, so all are dropped first and recreated after the rename in
    # creation order (views before triggers, since a trigger may name a view).
    cursor = await db.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='view' AND sql IS NOT NULL ORDER BY rowid"
    )
    live_views = list(await cursor.fetchall())
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND tbl_name='update_history' AND sql IS NOT NULL ORDER BY rowid"
    )
    live_indexes = [r[0] for r in await cursor.fetchall()]
    cursor = await db.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL "
        "ORDER BY rowid"
    )
    live_triggers = list(await cursor.fetchall())

    await db.execute(
        f"CREATE TABLE {_SCRATCH_TABLE} ({_COLUMNS_TEMPLATE.format(statuses=statuses)})"
    )
    copied = [c for c in live_cols if c in target_cols]
    collist = ", ".join(copied)
    # down() narrows the CHECK: crash rows are carried over as 'failed' in the
    # copy itself, so no row is touched before every refusal above has passed.
    selectlist = ", ".join(
        "CASE status WHEN 'crashed_recovered' THEN 'failed' ELSE status END"
        if c == "status" and remap_crashed
        else c
        for c in copied
    )
    # Identifiers come from PRAGMA output checked against in-repo literals.
    await db.execute(
        f"INSERT INTO {_SCRATCH_TABLE} (rowid, {collist}) "  # noqa: S608
        f"SELECT rowid, {selectlist} FROM {_TABLE}"
    )

    for name, _sql in reversed(live_triggers):
        quoted = name.replace('"', '""')
        await db.execute(f'DROP TRIGGER IF EXISTS "{quoted}"')
    for name, _sql in reversed(live_views):
        quoted = name.replace('"', '""')
        await db.execute(f'DROP VIEW "{quoted}"')
    await db.execute(f"DROP TABLE {_TABLE}")
    await db.execute(f"ALTER TABLE {_SCRATCH_TABLE} RENAME TO {_TABLE}")
    for stmt in live_indexes:
        await db.execute(stmt)
    for _name, stmt in live_views:
        await db.execute(stmt)
    for _name, stmt in live_triggers:
        await db.execute(stmt)


def _has_wide_status_check(ddl: str) -> bool:
    """Does the live table carry THIS migration's status CHECK?

    Compared as parsed CHECK expressions, never as a substring: the literal
    'crashed_recovered' elsewhere in the DDL (a DEFAULT, a comment, another
    column's CHECK) must not mark a still-narrow status constraint as migrated.
    Any other shape is rebuilt, and the rebuild refuses what it does not know.
    """
    live_checks = set(_ddl_features(ddl)[0])
    return set(_canonical_shape(_WIDE_STATUSES)["checks"]) <= live_checks


async def up(db: aiosqlite.Connection) -> None:
    ddl = await _live_ddl(db)
    if ddl is None:
        return  # Table absent — defensive guard for non-sequential edge cases
    if _has_wide_status_check(ddl):
        return  # Already has the expanded constraint
    await _rebuild(db, statuses=_WIDE_STATUSES)


async def down(db: aiosqlite.Connection) -> None:
    """Revert to the 0024 constraint, keeping recorded crashes as 'failed'.

    0024's down() dropped the rows its status allowed; a recorded crash is worth
    more than that, and its failure_reason ("Crashed in phase: ...") still says
    what happened, so the row is kept under the closest older status.
    """
    ddl = await _live_ddl(db)
    if ddl is None or not _has_wide_status_check(ddl):
        return  # Absent or already reverted
    await _rebuild(db, statuses=_NARROW_STATUSES, remap_crashed=True)
