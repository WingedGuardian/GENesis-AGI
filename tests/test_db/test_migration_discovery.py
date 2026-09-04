"""Tests for the shared migration file-discovery (db/_migration_discovery.py).

Extracted from the two migration runners; this exercises it directly (the
structural reviewer flagged it had only transitive coverage).
"""

from __future__ import annotations

import re
import sqlite3

from genesis.db._migration_discovery import discover_numbered_modules
from genesis.db.data_migrations.runner import _DATA_MIGRATION_PATTERN
from genesis.db.migrations.runner import _MIGRATION_PATTERN


def _make(dir_, *names):
    for n in names:
        (dir_ / n).write_text("")


def test_matches_pattern_and_orders_by_id(tmp_path):
    _make(tmp_path, "0002_b.py", "0001_a.py", "0010_c.py")
    pat = re.compile(r"^(\d{4})_\w+\.py$")
    out = discover_numbered_modules(tmp_path, pat)
    assert [mid for mid, _, _ in out] == ["0001", "0002", "0010"]  # zero-pad sort
    assert [stem for _, stem, _ in out] == ["0001_a", "0002_b", "0010_c"]


def test_frozen_legacy_ids_always_precede_timestamp_ids(tmp_path):
    """THE ordering invariant the whole mixed-width scheme rests on.

    Every legacy id starts with '0' (the namespace froze at 0091) and every
    timestamp with '2', so a plain lexicographic sort keeps the frozen history
    ahead of all new work — in Python's `sorted()` here AND in SQLite's TEXT
    collation on the ledger. If this ever fails, a fresh install would replay
    history in the wrong order.
    """
    _make(
        tmp_path,
        "20260903200000_new.py",
        "0091_last_legacy.py",
        "20260101000000_earlier.py",
        "0001_first_legacy.py",
    )
    out = discover_numbered_modules(tmp_path, _MIGRATION_PATTERN)
    ids = [mid for mid, _, _ in out]
    assert ids == ["0001", "0091", "20260101000000", "20260903200000"]

    # The SQLite half, EXERCISED rather than asserted in prose (the docstring
    # used to claim it while the body never touched a database): both ledgers
    # key on TEXT PRIMARY KEY, whose default BINARY collation must agree with
    # Python's codepoint order. Rows go in reversed so a table that happened to
    # return insertion order would fail this.
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE m (id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO m VALUES (?)", [(i,) for i in reversed(ids)])
        assert [r[0] for r in conn.execute("SELECT id FROM m ORDER BY id")] == ids
    finally:
        conn.close()


def test_timestamp_ids_are_discovered_in_both_namespaces(tmp_path):
    """The runners' real patterns must accept the new width — a pattern that
    silently skipped it would mean the migration never runs, with no error."""
    _make(tmp_path, "20260903200000_schema.py", "d20260903200000_data.py")
    assert [m for m, _, _ in discover_numbered_modules(tmp_path, _MIGRATION_PATTERN)] == [
        "20260903200000"
    ]
    assert [m for m, _, _ in discover_numbered_modules(tmp_path, _DATA_MIGRATION_PATTERN)] == [
        "d20260903200000"
    ]


def test_off_width_ids_are_refused_by_both_patterns(tmp_path):
    """Anchored alternation ({4}|{14}), never {4,14}: a mistyped 13- or
    15-digit id is REFUSED at discovery rather than quietly founding a third
    id namespace with its own ordering semantics."""
    _make(
        tmp_path,
        "2026090320000_short.py",
        "202609032000000_long.py",
        "d2026090320000_short.py",
    )
    assert discover_numbered_modules(tmp_path, _MIGRATION_PATTERN) == []
    assert discover_numbered_modules(tmp_path, _DATA_MIGRATION_PATTERN) == []


def test_ignores_non_matching_files(tmp_path):
    _make(tmp_path, "0001_ok.py", "README.md", "0001_ok.pyc", "notes.txt", "_helper.py")
    pat = re.compile(r"^(\d{4})_\w+\.py$")
    out = discover_numbered_modules(tmp_path, pat)
    assert [stem for _, stem, _ in out] == ["0001_ok"]


def test_data_migration_pattern_is_distinct(tmp_path):
    # The d-prefixed pattern must NOT pick up schema-style files and vice versa.
    _make(
        tmp_path,
        "0001_schema.py",
        "d0001_data.py",
        "20260903200000_schema_ts.py",
        "d20260903200000_data_ts.py",
    )
    assert [s for _, s, _ in discover_numbered_modules(tmp_path, _MIGRATION_PATTERN)] == [
        "0001_schema",
        "20260903200000_schema_ts",
    ]
    assert [s for _, s, _ in discover_numbered_modules(tmp_path, _DATA_MIGRATION_PATTERN)] == [
        "d0001_data",
        "d20260903200000_data_ts",
    ]


def test_empty_dir_returns_empty(tmp_path):
    assert discover_numbered_modules(tmp_path, re.compile(r"^(\d{4})_\w+\.py$")) == []
