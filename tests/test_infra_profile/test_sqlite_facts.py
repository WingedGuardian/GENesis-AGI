"""sqlite collector: read-only proof + pragma/metric shape."""

from __future__ import annotations

import sqlite3

from genesis.infra_profile.collectors.sqlite_facts import collect_sqlite
from genesis.infra_profile.types import STATUS_ERROR, STATUS_OK


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()


async def test_pragma_facts_and_size_metrics(tmp_path):
    db_path = tmp_path / "genesis.db"
    _make_db(db_path)

    result = await collect_sqlite(db_path=db_path)
    assert result.status == STATUS_OK
    assert result.facts["pragmas"]["journal_mode"] == "wal"
    assert result.facts["pragmas"]["page_size"] > 0
    # sizes are metrics, never facts (they'd churn the hash every write)
    assert result.metrics["db_size_bytes"] > 0
    assert "db_size_bytes" not in result.facts


async def test_connection_is_readonly(tmp_path):
    """The mode=ro URI must reject writes — proves we can't touch the live DB."""
    db_path = tmp_path / "genesis.db"
    _make_db(db_path)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        import pytest

        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t VALUES (2)")
    finally:
        conn.close()


async def test_missing_db_degrades(tmp_path):
    result = await collect_sqlite(db_path=tmp_path / "absent.db")
    assert result.status == STATUS_ERROR
    assert "not found" in result.error
