"""Tests for the shared migration file-discovery (db/_migration_discovery.py).

Extracted from the two migration runners; this exercises it directly (the
structural reviewer flagged it had only transitive coverage).
"""

from __future__ import annotations

import re

from genesis.db._migration_discovery import discover_numbered_modules


def _make(dir_, *names):
    for n in names:
        (dir_ / n).write_text("")


def test_matches_pattern_and_orders_by_id(tmp_path):
    _make(tmp_path, "0002_b.py", "0001_a.py", "0010_c.py")
    pat = re.compile(r"^(\d{4})_\w+\.py$")
    out = discover_numbered_modules(tmp_path, pat)
    assert [mid for mid, _, _ in out] == ["0001", "0002", "0010"]  # zero-pad sort
    assert [stem for _, stem, _ in out] == ["0001_a", "0002_b", "0010_c"]


def test_ignores_non_matching_files(tmp_path):
    _make(tmp_path, "0001_ok.py", "README.md", "0001_ok.pyc", "notes.txt", "_helper.py")
    pat = re.compile(r"^(\d{4})_\w+\.py$")
    out = discover_numbered_modules(tmp_path, pat)
    assert [stem for _, stem, _ in out] == ["0001_ok"]


def test_data_migration_pattern_is_distinct(tmp_path):
    # The d-prefixed pattern must NOT pick up schema-style files and vice versa.
    _make(tmp_path, "0001_schema.py", "d0001_data.py")
    schema_pat = re.compile(r"^(\d{4})_\w+\.py$")
    data_pat = re.compile(r"^(d\d{4})_\w+\.py$")
    assert [s for _, s, _ in discover_numbered_modules(tmp_path, schema_pat)] == ["0001_schema"]
    assert [s for _, s, _ in discover_numbered_modules(tmp_path, data_pat)] == ["d0001_data"]


def test_empty_dir_returns_empty(tmp_path):
    assert discover_numbered_modules(tmp_path, re.compile(r"^(\d{4})_\w+\.py$")) == []
