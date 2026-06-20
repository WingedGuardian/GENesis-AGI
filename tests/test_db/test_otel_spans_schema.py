"""Schema tests for the otel_spans tracing table (migration 0028).

Verifies the migration path and the fresh-install path (db/schema/_tables.py)
produce the SAME otel_spans schema — the two must never drift — plus the
load-bearing constraints (NOT NULLs, the status CHECK, the partial root index).
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.migrations.runner import MigrationRunner
from genesis.db.schema import create_all_tables

_EXPECTED_COLUMNS = {
    "span_id", "trace_id", "parent_span_id", "name", "kind", "status",
    "status_message", "start_unix_us", "end_unix_us", "duration_us",
    "session_id", "process", "call_site", "provider", "model_id",
    "input_tokens", "output_tokens", "cost_usd", "cost_known",
    "attributes_json", "created_at",
}

_EXPECTED_INDEXES = {
    "idx_otel_spans_trace", "idx_otel_spans_parent", "idx_otel_spans_start",
    "idx_otel_spans_session", "idx_otel_spans_roots",
}


@pytest.fixture()
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        yield conn


async def _columns(conn: aiosqlite.Connection) -> dict[str, tuple[int, int]]:
    """name -> (notnull, pk) from PRAGMA table_info(otel_spans)."""
    cur = await conn.execute("PRAGMA table_info(otel_spans)")
    rows = await cur.fetchall()
    # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
    return {r[1]: (r[3], r[5]) for r in rows}


async def _index_names(conn: aiosqlite.Connection) -> set[str]:
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='otel_spans' AND name LIKE 'idx_otel_spans_%'"
    )
    return {r[0] for r in await cur.fetchall()}


class TestMigrationPath:

    @pytest.mark.asyncio
    async def test_migration_creates_table_and_indexes(self, db) -> None:
        results = await MigrationRunner(db).run_pending()
        assert all(r.success for r in results)

        cols = await _columns(db)
        assert set(cols) == _EXPECTED_COLUMNS
        assert await _index_names(db) == _EXPECTED_INDEXES

    @pytest.mark.asyncio
    async def test_not_null_and_pk_constraints(self, db) -> None:
        await MigrationRunner(db).run_pending()
        cols = await _columns(db)
        # span_id is the PK
        assert cols["span_id"][1] == 1
        # required (NOT NULL) columns
        for name in ("trace_id", "name", "kind", "status", "start_unix_us"):
            assert cols[name][0] == 1, f"{name} should be NOT NULL"
        # nullable columns (parent_span_id => root; end/duration => point span)
        for name in ("parent_span_id", "end_unix_us", "duration_us", "cost_usd"):
            assert cols[name][0] == 0, f"{name} should be nullable"

    @pytest.mark.asyncio
    async def test_status_check_rejects_bad_value(self, db) -> None:
        await MigrationRunner(db).run_pending()
        # 'ok' and 'error' are allowed
        await db.execute(
            "INSERT INTO otel_spans (span_id, trace_id, name, kind, status, "
            "start_unix_us) VALUES ('s1', 't1', 'n', 'llm', 'ok', 1)"
        )
        await db.execute(
            "INSERT INTO otel_spans (span_id, trace_id, name, kind, status, "
            "start_unix_us) VALUES ('s2', 't1', 'n', 'llm', 'error', 2)"
        )
        # 'bogus' violates the CHECK
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO otel_spans (span_id, trace_id, name, kind, status, "
                "start_unix_us) VALUES ('s3', 't1', 'n', 'llm', 'bogus', 3)"
            )

    @pytest.mark.asyncio
    async def test_root_index_is_partial(self, db) -> None:
        await MigrationRunner(db).run_pending()
        cur = await db.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_otel_spans_roots'"
        )
        row = await cur.fetchone()
        assert row is not None and "parent_span_id IS NULL" in row[0]


class TestFreshInstallParity:

    @pytest.mark.asyncio
    async def test_fresh_install_matches_migration(self, db, tmp_path) -> None:
        # Fresh-install path
        await create_all_tables(db)
        fresh_cols = await _columns(db)
        fresh_idx = await _index_names(db)

        assert set(fresh_cols) == _EXPECTED_COLUMNS
        assert fresh_idx == _EXPECTED_INDEXES

        # Migration path (separate DB)
        mig_path = tmp_path / "mig.db"
        async with aiosqlite.connect(str(mig_path)) as mig:
            await mig.execute("PRAGMA journal_mode=WAL")
            await MigrationRunner(mig).run_pending()
            mig_cols = await _columns(mig)
            mig_idx = await _index_names(mig)

        # The two install paths must produce identical column + index sets.
        assert fresh_cols == mig_cols
        assert fresh_idx == mig_idx
