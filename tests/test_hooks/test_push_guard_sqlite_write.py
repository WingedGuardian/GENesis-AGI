"""Tests for git_push_guard's direct-sqlite-write detection (_is_sqlite_write).

The DB-write guard used to fire on `"sqlite3" in cmd AND \\b(INSERT|UPDATE|
DELETE|DROP|ALTER|REPLACE)\\b` — over-broad in two ways reproduced live:
  1. a DML keyword appearing as a SQL *function* (`replace(...)`) in a read-only
     SELECT, and
  2. a non-sqlite command (a `grep` of this hook's own source) that merely
     MENTIONS "sqlite3" alongside the keywords.

The fix keeps the broad whole-command match (so heredocs / wrappers can't hide a
write) and only narrows the DML to STATEMENT position (INSERT INTO/OR, REPLACE
INTO, UPDATE...SET, DELETE FROM, DROP/ALTER <object>). That removes both false
positives without weakening write coverage — the bypass tests below assert real
writes still block even when read-only-looking tokens appear in the SQL data.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_HOOKS_DIR = _WORKTREE / "scripts" / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

_spec = importlib.util.spec_from_file_location("git_push_guard", _HOOKS_DIR / "git_push_guard.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _is_write(cmd: str) -> bool:
    return _mod._is_sqlite_write(cmd)


class TestRealWritesStillBlocked:
    def test_insert_into(self):
        assert _is_write('sqlite3 db.sqlite "INSERT INTO t VALUES (1)"')

    def test_insert_or_replace(self):
        assert _is_write('sqlite3 db.sqlite "INSERT OR REPLACE INTO t VALUES (1)"')

    def test_update_set(self):
        assert _is_write('sqlite3 db.sqlite "UPDATE t SET x = 1 WHERE id = 2"')

    def test_update_with_conflict_clause(self):
        assert _is_write('sqlite3 db.sqlite "UPDATE OR REPLACE t SET x = 1"')

    def test_delete_from(self):
        assert _is_write('sqlite3 db.sqlite "DELETE FROM t WHERE id = 1"')

    def test_replace_into_statement(self):
        assert _is_write('sqlite3 db.sqlite "REPLACE INTO t VALUES (1)"')

    def test_drop_table(self):
        assert _is_write('sqlite3 db.sqlite "DROP TABLE t"')

    def test_alter_table(self):
        assert _is_write('sqlite3 db.sqlite "ALTER TABLE t ADD COLUMN x TEXT"')

    def test_drop_index(self):
        assert _is_write('sqlite3 db.sqlite "DROP INDEX idx_t"')

    def test_drop_trigger(self):
        assert _is_write('sqlite3 db.sqlite "DROP TRIGGER trg_t"')

    def test_python_sqlite_write(self):
        assert _is_write(
            "python3 -c \"import sqlite3; sqlite3.connect('d').execute('INSERT INTO t VALUES (1)')\""
        )

    def test_write_chained_after_readonly_read(self):
        # A read-only read chained with a real write must still block.
        assert _is_write("sqlite3 db.sqlite 'SELECT 1' && sqlite3 db.sqlite \"DELETE FROM t\"")

    def test_update_with_quoted_identifier(self):
        # Quoted-identifier UPDATE (the gap between UPDATE and SET spans a quote)
        # must still be caught.
        assert _is_write('sqlite3 db.sqlite \'UPDATE "users" SET "name" = 1\'')

    def test_heredoc_python_write_not_hidden(self):
        # A multi-line python heredoc fragments into segments, but whole-command
        # detection still sees sqlite3 + the write.
        cmd = (
            "python3 <<'EOF'\nimport sqlite3\n"
            "sqlite3.connect('x.db').execute(\"INSERT INTO t VALUES (1)\")\nEOF"
        )
        assert _is_write(cmd)

    def test_write_with_readonly_token_in_sql_data(self):
        # A read-only-looking token appearing inside the SQL data must NOT exempt
        # a real write (the former false-exemption bypass).
        assert _is_write('sqlite3 genesis.db "UPDATE settings SET query_only_flag = 0"')
        assert _is_write("sqlite3 genesis.db \"DELETE FROM t WHERE reason = 'mode=ro'\"")


class TestFalsePositivesNotBlocked:
    def test_readonly_select_with_replace_function(self):
        # The exact live false positive: a SELECT using the replace() scalar
        # function against a table whose keyword-ish name is irrelevant.
        cmd = (
            "sqlite3 -readonly /home/x/genesis.db "
            "\"SELECT id, replace(substr(content,1,140), char(10), ' ') "
            "FROM ego_proposals WHERE content LIKE '%user_model%'\""
        )
        assert not _is_write(cmd)

    def test_select_with_replace_function_no_readonly_flag(self):
        # replace() is a scalar function, not the REPLACE INTO statement.
        assert not _is_write("sqlite3 db.sqlite \"SELECT replace(x, 'a', 'b') FROM t\"")

    def test_grep_mentioning_sqlite3_and_bare_keywords(self):
        # The other live false positive: grepping this hook's own source with bare
        # (non-statement) keywords in the pattern.
        cmd = (
            "grep -n 'sqlite3 are not allowed|INSERT|UPDATE|DELETE' scripts/hooks/git_push_guard.py"
        )
        assert not _is_write(cmd)

    def test_immutable_uri_read(self):
        assert not _is_write("sqlite3 'file:d?immutable=1' \"SELECT * FROM t\"")

    def test_plain_select(self):
        assert not _is_write('sqlite3 db.sqlite "SELECT count(*) FROM t"')

    def test_non_sqlite_command(self):
        # No 'sqlite3' mention → never a sqlite write, whatever the DML text.
        assert not _is_write("psql -c 'INSERT INTO t VALUES (1)'")

    def test_cat_of_sql_file(self):
        assert not _is_write("cat migrations/001_INSERT_INTO_seed.sql")
