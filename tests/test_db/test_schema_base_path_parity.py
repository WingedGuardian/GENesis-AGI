"""Guard against the #1123/#1127 bootstrap-crash class.

Root cause (2026-07-18): ``create_all_tables`` runs ``_migrate_add_columns``
and then builds ``INDEXES`` — and the numbered-migration runner only runs
*afterward* (``runtime/init/db.py``). So on a legacy DB whose table predates a
column, an index in the module-level ``INDEXES`` list that references that
column hits ``no such column`` during the index build and bootstrap crashes,
because the numbered migration that would have added the column has not run
yet. Fresh-DB CI never sees it (the canonical ``CREATE TABLE`` in ``_tables``
already carries the column), so it merges green. #1123 shipped exactly this
for ``ego_directives`` and #1127 patched the instance.

This is the *class* guard. The precise invariant that prevents the crash:

    every column referenced by the module-level ``INDEXES`` list must exist on
    a legacy table at the moment ``create_all_tables`` builds indexes — i.e.
    it is either an original canonical column (a legacy DB has always had it)
    or, if a numbered migration adds it, it is ALSO added by
    ``_migrate_add_columns`` (which runs before the index build).

Equivalently: no ``INDEXES``-referenced column may be *migration-added* without
being mirrored into ``_migrate_add_columns``. "Referenced" covers both the
``ON(...)`` key columns and a partial index's ``WHERE``-predicate columns —
both must exist when the index is built.

Deliberately NOT asserted: that every migration-added column is mirrored into
``_migrate_add_columns``. Only *indexed* columns can trigger the crash, and
mirroring an unindexed column whose migration does a bare (unguarded)
``ADD COLUMN`` would itself collide with the runner and abort boot. So the
guard is scoped to exactly the columns that can crash create_all_tables.

Pure static analysis — no DB, no services, no network. Install-agnostic.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from genesis.db import migrations as _migrations_pkg
from genesis.db.schema import INDEXES, TABLES
from genesis.db.schema import _migrations as _schema_migrations

_ALTER_RE = re.compile(r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)", re.IGNORECASE)
_INDEX_ON_RE = re.compile(r"\bON\s+(\w+)\s*\(([^)]+)\)(.*)$", re.IGNORECASE | re.DOTALL)


def _index_column_pairs() -> set[tuple[str, str]]:
    """(table, column) pairs in the ON(...) list of each module-level INDEXES
    statement. Strict — every token here is genuinely a column name."""
    pairs: set[tuple[str, str]] = set()
    for stmt in INDEXES:
        m = _INDEX_ON_RE.search(stmt)
        if not m:
            continue
        table = m.group(1).lower()
        for col in m.group(2).split(","):
            name = col.strip().split()[0].strip('"').lower()  # drop ASC/DESC/collate
            pairs.add((table, name))
    return pairs


def _index_predicate_pairs() -> set[tuple[str, str]]:
    """(table, identifier) tokens in the WHERE clause of a partial index. A
    partial index's predicate columns must ALSO exist at index-build time, so a
    migration-only WHERE column is the same #1123 crash. Loose — includes SQL
    keywords/values, which is safe for the crash-class check only (a keyword is
    never a migration-added column). NOT used by the fresh-DB check, where a
    keyword token would false-positive."""
    pairs: set[tuple[str, str]] = set()
    for stmt in INDEXES:
        m = _INDEX_ON_RE.search(stmt)
        if not m:
            continue
        table = m.group(1).lower()
        where = re.search(r"\bWHERE\b(.*)$", m.group(3), re.IGNORECASE | re.DOTALL)
        if not where:
            continue
        for tok in re.findall(r"[a-z_]\w*", where.group(1), re.IGNORECASE):
            pairs.add((table, tok.lower()))
    return pairs


def _base_path_addable() -> set[tuple[str, str]]:
    """(table, column) pairs that _migrate_add_columns adds on the base path."""
    src = inspect.getsource(_schema_migrations._migrate_add_columns)
    return {(t.lower(), c.lower()) for t, c in _ALTER_RE.findall(src)}


def _migration_added() -> set[tuple[str, str]]:
    """(table, column) pairs any numbered migration adds via ALTER ... ADD COLUMN."""
    mig_dir = Path(_migrations_pkg.__file__).parent
    pairs: set[tuple[str, str]] = set()
    for path in sorted(mig_dir.glob("[0-9][0-9][0-9][0-9]_*.py")):
        for t, c in _ALTER_RE.findall(path.read_text()):
            pairs.add((t.lower(), c.lower()))
    return pairs


def _canonical_columns() -> dict[str, set[str]]:
    """Column names declared in each canonical CREATE TABLE (lenient word scan)."""
    cols: dict[str, set[str]] = {}
    for name, ddl in TABLES.items():
        found: set[str] = set()
        for line in ddl.splitlines():
            m = re.match(r"\s*([a-z_][a-z0-9_]*)\s+", line, re.IGNORECASE)
            if not m:
                continue
            tok = m.group(1).lower()
            if tok in {"primary", "foreign", "unique", "check", "constraint", "create", "table"}:
                continue
            found.add(tok)
        cols[name.lower()] = found
    return cols


def crash_class_columns(
    indexed: set[tuple[str, str]],
    migration_added: set[tuple[str, str]],
    addable: set[tuple[str, str]],
) -> set[tuple[str, str]]:
    """Indexed columns a legacy DB will be MISSING when create_all_tables builds
    indexes: migration-added but not mirrored into _migrate_add_columns."""
    return {pair for pair in indexed if pair in migration_added and pair not in addable}


def test_no_indexed_column_is_migration_only():
    """The #1123 invariant: every INDEXES-referenced, migration-added column is
    also added by _migrate_add_columns (so it exists before the index build).
    Covers both ON(...) columns and partial-index WHERE-predicate columns."""
    referenced = _index_column_pairs() | _index_predicate_pairs()
    offenders = crash_class_columns(referenced, _migration_added(), _base_path_addable())
    assert not offenders, (
        "INDEXES references column(s) that only a numbered migration adds, so "
        "create_all_tables will crash on a legacy DB before the migration runs "
        "(the #1123/#1127 bootstrap-crash class). Add an idempotent "
        "`ALTER TABLE <t> ADD COLUMN <c> ...` to `_migrate_add_columns` in "
        "src/genesis/db/schema/_migrations.py (mirror the numbered migration's "
        f"exact DDL), before the INDEXES loop. Offending (table, column): "
        f"{sorted(offenders)}"
    )


def test_every_indexed_column_is_creatable_before_index_build():
    """Fresh-DB companion: an INDEXES entry can't reference a column that exists
    NOWHERE the base path can create it. On a fresh DB create_all_tables runs
    CREATE TABLE (canonical) then _migrate_add_columns then the INDEXES loop, so
    an indexed column must be in the canonical CREATE TABLE OR added by
    _migrate_add_columns (e.g. memory_metadata.superseded_by is add-column-only
    and never in the canonical DDL — that is legitimate and safe). A column in
    neither means the index references something that exists nowhere — a fresh
    DB crashes on the index build."""
    canon = _canonical_columns()
    addable = _base_path_addable()
    missing = []
    for table, col in sorted(_index_column_pairs()):
        # unknown table (view/fts/dynamically-created) — out of scope for this guard
        if table not in canon:
            continue
        if col not in canon[table] and (table, col) not in addable:
            missing.append((table, col))
    assert not missing, (
        "INDEXES references column(s) present neither in the canonical CREATE "
        "TABLE (src/genesis/db/schema/_tables.py) nor in _migrate_add_columns — "
        "the column exists nowhere the base path creates it, so a fresh DB "
        f"crashes building the index. Add it to one of them. Missing: {missing}"
    )


def test_checker_detects_synthetic_gap():
    """Prove the checker actually catches the bug (not just that main is clean):
    an indexed, migration-added column absent from _migrate_add_columns is
    flagged; the same column present in _migrate_add_columns is not."""
    indexed = {("widgets", "color")}
    migration_added = {("widgets", "color")}
    assert crash_class_columns(indexed, migration_added, addable=set()) == {("widgets", "color")}
    assert crash_class_columns(indexed, migration_added, addable={("widgets", "color")}) == set()
    # an original (non-migration) indexed column is never flagged
    assert crash_class_columns({("widgets", "id")}, migration_added, addable=set()) == set()
