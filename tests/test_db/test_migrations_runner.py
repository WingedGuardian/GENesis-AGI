"""Tests for genesis.db.migrations.runner — atomicity and idempotence."""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.db.migrations.runner import MigrationRunner


@pytest.fixture()
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        yield conn


class TestBasicLifecycle:

    @pytest.mark.asyncio
    async def test_apply_first_migration(self, db) -> None:
        runner = MigrationRunner(db)

        # Initially nothing applied
        applied = await runner.get_applied()
        assert applied == set()

        # At least one migration available
        available = await runner.get_available()
        assert len(available) >= 1

        # Apply pending
        results = await runner.run_pending()
        assert len(results) >= 1
        assert all(r.success for r in results)

        # Tracked
        applied_after = await runner.get_applied()
        assert "0001" in applied_after

    @pytest.mark.asyncio
    async def test_idempotent_re_run(self, db) -> None:
        runner = MigrationRunner(db)
        await runner.run_pending()

        # Re-run should be no-op
        results = await runner.run_pending()
        assert results == []

    @pytest.mark.asyncio
    async def test_dry_run_does_not_apply(self, db) -> None:
        runner = MigrationRunner(db)

        results = await runner.run_pending(dry_run=True)
        assert len(results) >= 1

        # Nothing actually applied
        applied = await runner.get_applied()
        assert applied == set()


class TestAtomicity:
    """Migration body + tracking row must be a single atomic transaction.

    Specifically tests the DDL-in-rollback case which previously failed
    because Python sqlite3's implicit transaction handling auto-commits
    before CREATE TABLE.
    """

    @pytest.mark.asyncio
    async def test_failure_rolls_back_ddl_and_tracking(self, db) -> None:
        runner = MigrationRunner(db)

        mod = importlib.import_module("genesis.db.migrations.0001_add_update_history")
        original_up = mod.up

        async def failing_up(conn):
            await original_up(conn)  # creates update_history
            raise RuntimeError("simulated failure after DDL")

        mod.up = failing_up

        try:
            results = await runner.run_pending()
        finally:
            mod.up = original_up

        # Migration reported failure
        assert len(results) == 1
        assert not results[0].success
        assert "simulated failure" in results[0].error

        # Tracking row absent
        applied = await runner.get_applied()
        assert "0001" not in applied

        # CRITICAL: DDL must have been rolled back too
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='update_history'"
        )
        row = await cursor.fetchone()
        assert row is None, (
            "update_history table should NOT exist after rollback — "
            "if it does, DDL atomicity is broken (Python sqlite3 implicit "
            "commit before DDL leaked through)"
        )

    @pytest.mark.asyncio
    async def test_recovery_after_rollback(self, db) -> None:
        """After a failed migration is fixed, re-running applies it cleanly."""
        runner = MigrationRunner(db)

        mod = importlib.import_module("genesis.db.migrations.0001_add_update_history")
        original_up = mod.up

        async def failing_up(conn):
            await original_up(conn)
            raise RuntimeError("transient")

        mod.up = failing_up

        try:
            results1 = await runner.run_pending()
            assert not results1[0].success
        finally:
            mod.up = original_up

        # Now re-run with the original (working) migration
        results2 = await runner.run_pending()
        assert len(results2) >= 1 and all(r.success for r in results2)

        applied = await runner.get_applied()
        assert "0001" in applied

        # Table now exists
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE name='update_history'"
        )
        assert (await cursor.fetchone()) is not None


class TestConnectionProxy:
    """The connection passed to up() must block commit/rollback mechanically.

    Regression guard: without the proxy, a stray db.commit() inside a
    migration re-introduces the DDL-rollback bug.
    """

    @pytest.mark.asyncio
    async def test_migration_commit_is_blocked(self, db) -> None:
        runner = MigrationRunner(db)

        mod = importlib.import_module("genesis.db.migrations.0001_add_update_history")
        original_up = mod.up

        async def commit_in_migration(conn):
            await original_up(conn)
            await conn.commit()  # Must raise via the proxy

        mod.up = commit_in_migration

        try:
            results = await runner.run_pending()
        finally:
            mod.up = original_up

        assert len(results) == 1
        assert not results[0].success
        assert "must not call db.commit" in results[0].error.lower()

        # And the DDL must still have been rolled back
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='update_history'"
        )
        assert await cursor.fetchone() is None

    @pytest.mark.asyncio
    async def test_migration_rollback_is_blocked(self, db) -> None:
        runner = MigrationRunner(db)

        mod = importlib.import_module("genesis.db.migrations.0001_add_update_history")
        original_up = mod.up

        async def rollback_in_migration(conn):
            await original_up(conn)
            await conn.rollback()

        mod.up = rollback_in_migration

        try:
            results = await runner.run_pending()
        finally:
            mod.up = original_up

        assert not results[0].success
        assert "must not call db.rollback" in results[0].error.lower()

    @pytest.mark.asyncio
    async def test_migration_async_with_is_blocked(self, db) -> None:
        runner = MigrationRunner(db)

        mod = importlib.import_module("genesis.db.migrations.0001_add_update_history")
        original_up = mod.up

        async def async_with_in_migration(conn):
            async with conn:
                await original_up(conn)

        mod.up = async_with_in_migration

        try:
            results = await runner.run_pending()
        finally:
            mod.up = original_up

        assert not results[0].success
        assert "async with" in results[0].error.lower()

    @pytest.mark.asyncio
    async def test_migration_execute_passes_through(self, db) -> None:
        """Proxy must NOT break normal execute/executemany/fetchall usage."""
        runner = MigrationRunner(db)
        results = await runner.run_pending()
        assert all(r.success for r in results)

        # And verify the actual 0001 migration (which uses execute) applied.
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE name='update_history'"
        )
        assert await cursor.fetchone() is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sql", [
        "COMMIT",
        "commit",
        "COMMIT;",
        "  COMMIT  ",
        "BEGIN",
        "BEGIN IMMEDIATE",
        "begin transaction",
        "ROLLBACK",
        "rollback;",
        "END",
        "SAVEPOINT sp1",
        "RELEASE sp1",
    ])
    async def test_migration_sql_level_transaction_control_blocked(
        self, db, sql,
    ) -> None:
        """Regression for review M1: block raw-SQL escape hatches.

        Without this filter a migration author could bypass the proxy
        with ``await db.execute("COMMIT")``, reopening the DDL-rollback
        bug that the proxy exists to prevent.
        """
        runner = MigrationRunner(db)

        mod = importlib.import_module("genesis.db.migrations.0001_add_update_history")
        original_up = mod.up

        async def raw_tx_in_migration(conn):
            await original_up(conn)
            await conn.execute(sql)

        mod.up = raw_tx_in_migration

        try:
            results = await runner.run_pending()
        finally:
            mod.up = original_up

        assert len(results) == 1
        assert not results[0].success
        assert "must not" in results[0].error.lower()

        # DDL was rolled back
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE name='update_history'"
        )
        assert await cursor.fetchone() is None

    @pytest.mark.asyncio
    async def test_migration_cursor_is_blocked(self, db) -> None:
        """db.cursor() would be a back-door around the execute filter."""
        runner = MigrationRunner(db)

        mod = importlib.import_module("genesis.db.migrations.0001_add_update_history")
        original_up = mod.up

        async def cursor_in_migration(conn):
            await original_up(conn)
            await conn.cursor()

        mod.up = cursor_in_migration

        try:
            results = await runner.run_pending()
        finally:
            mod.up = original_up

        assert not results[0].success
        assert "cursor" in results[0].error.lower()

    @pytest.mark.asyncio
    async def test_migration_executescript_is_blocked(self, db) -> None:
        """executescript implicitly commits before running — always block."""
        runner = MigrationRunner(db)

        mod = importlib.import_module("genesis.db.migrations.0001_add_update_history")
        original_up = mod.up

        async def script_in_migration(conn):
            await original_up(conn)
            await conn.executescript("CREATE TABLE extra (x INTEGER);")

        mod.up = script_in_migration

        try:
            results = await runner.run_pending()
        finally:
            mod.up = original_up

        assert not results[0].success
        assert "executescript" in results[0].error.lower()

    @pytest.mark.asyncio
    async def test_migration_executemany_blocks_tx_tokens(self, db) -> None:
        """executemany must also filter transaction control verbs."""
        runner = MigrationRunner(db)

        mod = importlib.import_module("genesis.db.migrations.0001_add_update_history")
        original_up = mod.up

        async def em_tx_in_migration(conn):
            await original_up(conn)
            await conn.executemany("COMMIT", [])

        mod.up = em_tx_in_migration

        try:
            results = await runner.run_pending()
        finally:
            mod.up = original_up

        assert not results[0].success
        assert "commit" in results[0].error.lower()

    @pytest.mark.asyncio
    async def test_migration_normal_dml_still_works(self, db) -> None:
        """SELECT, CREATE, INSERT, UPDATE, DELETE must all still pass."""
        runner = MigrationRunner(db)

        mod = importlib.import_module("genesis.db.migrations.0001_add_update_history")
        original_up = mod.up

        async def mixed_dml_migration(conn):
            await original_up(conn)
            await conn.execute("CREATE TABLE t (id INTEGER)")
            await conn.execute("INSERT INTO t VALUES (?)", (1,))
            cursor = await conn.execute("SELECT id FROM t")
            row = await cursor.fetchone()
            assert row[0] == 1
            await conn.execute("UPDATE t SET id = ? WHERE id = ?", (2, 1))
            await conn.execute("DELETE FROM t WHERE id = ?", (2,))

        mod.up = mixed_dml_migration

        try:
            results = await runner.run_pending()
        finally:
            mod.up = original_up

        assert results[0].success, f"normal DML was blocked: {results[0].error}"


class TestDuplicatePrefixDetection:
    """Pre-flight check must catch duplicate migration prefixes before running."""

    @pytest.mark.asyncio
    async def test_duplicate_prefix_raises_before_any_migration_runs(
        self, db, tmp_path, monkeypatch,
    ) -> None:
        # Create two migration files with the same prefix in a temp dir
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        for name in ("0001_alpha.py", "0001_beta.py"):
            (migrations_dir / name).write_text(
                "async def up(db): pass\n"
            )
        monkeypatch.setattr(
            "genesis.db.migrations.runner._MIGRATIONS_DIR", migrations_dir,
        )

        runner = MigrationRunner(db)
        with pytest.raises(RuntimeError, match=r"Duplicate migration prefix '0001'"):
            await runner.run_pending()

        # No migrations applied — check was pre-flight
        applied = await runner.get_applied()
        assert applied == set()

    @pytest.mark.asyncio
    async def test_no_false_positive_on_unique_prefixes(
        self, db, tmp_path, monkeypatch,
    ) -> None:
        """Unique prefixes pass the pre-flight check (verified via get_pending)."""
        migrations_dir = tmp_path / "migrations"
        migrations_dir.mkdir()
        (migrations_dir / "0001_alpha.py").write_text(
            "async def up(db): pass\n"
        )
        (migrations_dir / "0002_beta.py").write_text(
            "async def up(db): pass\n"
        )
        monkeypatch.setattr(
            "genesis.db.migrations.runner._MIGRATIONS_DIR", migrations_dir,
        )

        runner = MigrationRunner(db)
        # Pre-flight passes — no RuntimeError raised.  We don't need to
        # run the actual migrations (importlib can't resolve temp files);
        # just verify the duplicate check doesn't false-positive.
        available = await runner.get_available()
        assert len(available) == 2
        # Verify the check itself passes by calling the same logic
        seen: dict[str, str] = {}
        for mid, name, _ in available:
            assert mid not in seen, f"Unexpected duplicate: {mid}"
            seen[mid] = name


class TestStatus:

    @pytest.mark.asyncio
    async def test_status_reports_pending_then_applied(self, db) -> None:
        runner = MigrationRunner(db)

        status_before = await runner.status()
        assert status_before["applied_count"] == 0
        assert status_before["pending_count"] >= 1

        await runner.run_pending()

        status_after = await runner.status()
        assert status_after["pending_count"] == 0
        assert status_after["applied_count"] >= 1
