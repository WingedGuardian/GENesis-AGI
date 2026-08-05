"""Tests for the data-migration framework (WS-C): ledger CRUD + runner.

The runner is driven against FAKE migration modules (monkeypatched discovery +
import) so the state machine is exercised without real Qdrant/entity I/O — the
seed d0001's own logic is tested in test_memory/test_origin_class_backfill.py.
"""

from __future__ import annotations

import types

import aiosqlite
import pytest

from genesis.db.crud import data_migrations as crud
from genesis.db.data_migrations import runner as runner_mod
from genesis.db.data_migrations.runner import DataMigrationRunner
from genesis.db.schema import create_all_tables


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture(autouse=True)
def _no_boot_settle_delay(monkeypatch):
    """Disable the data-migration boot-settle delay by default so tests that call
    run_data_migrations() don't wait the real 120s. The delay's own tests override
    this and patch asyncio.sleep."""
    monkeypatch.setenv("GENESIS_DATA_MIGRATION_BOOT_DELAY_S", "0")


async def test_migration_0060_idempotent_after_create_all_tables():
    """The REAL boot path: init_db runs create_all_tables (which creates
    data_migrations from _tables.py) BEFORE the migration runner. Migration
    0060 must be idempotent against that — a bare CREATE would abort boot."""
    from genesis.db.migrations.runner import MigrationRunner

    conn = await aiosqlite.connect(":memory:")
    try:
        await create_all_tables(conn)  # table now exists (fresh-install path)
        results = await MigrationRunner(conn).run_pending()  # 0060 must not fail
        failed = [r for r in results if not r.success]
        assert failed == [], [(r.name, r.error) for r in failed]
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='data_migrations'"
        )
        assert await cur.fetchone() is not None
    finally:
        await conn.close()


# ── ledger CRUD ──────────────────────────────────────────────────────


async def test_ensure_row_status_depends_on_operator_flag(db):
    await crud.ensure_row(db, id="d0001", name="d0001_x", requires_operator=False)
    await crud.ensure_row(db, id="d0002", name="d0002_y", requires_operator=True)
    assert await crud.get_status(db, "d0001") == "pending"
    assert await crud.get_status(db, "d0002") == "operator_pending"


async def test_ensure_row_is_idempotent_and_never_downgrades(db):
    await crud.ensure_row(db, id="d0001", name="d0001_x", requires_operator=False)
    assert await crud.claim(db, "d0001")
    await crud.mark_completed(db, "d0001", summary="{}")
    # A second ensure_row must NOT reset a completed row back to pending.
    await crud.ensure_row(db, id="d0001", name="d0001_x", requires_operator=False)
    assert await crud.get_status(db, "d0001") == "completed"


async def test_claim_is_exclusive(db):
    await crud.ensure_row(db, id="d0001", name="d0001_x", requires_operator=False)
    assert await crud.claim(db, "d0001") is True  # first wins
    assert await crud.claim(db, "d0001") is False  # already running, loser
    assert await crud.get_status(db, "d0001") == "running"


async def test_claim_retries_failed_but_not_completed_or_operator(db):
    await crud.ensure_row(db, id="d0001", name="a", requires_operator=False)
    await crud.claim(db, "d0001")
    await crud.mark_failed(db, "d0001", error="boom")
    assert await crud.claim(db, "d0001") is True  # failed is retryable

    await crud.ensure_row(db, id="d0002", name="b", requires_operator=True)
    assert await crud.claim(db, "d0002") is False  # operator_pending never auto-claims

    await crud.ensure_row(db, id="d0003", name="c", requires_operator=False)
    await crud.claim(db, "d0003")
    await crud.mark_completed(db, "d0003", summary="done")
    assert await crud.claim(db, "d0003") is False  # completed never re-claims


async def test_reset_running_to_pending(db):
    await crud.ensure_row(db, id="d0001", name="a", requires_operator=False)
    await crud.claim(db, "d0001")  # -> running
    assert await crud.reset_running_to_pending(db) == 1
    assert await crud.get_status(db, "d0001") == "pending"
    # A completed row is NOT reset.
    await crud.claim(db, "d0001")
    await crud.mark_completed(db, "d0001", summary="x")
    assert await crud.reset_running_to_pending(db) == 0


async def test_mark_failed_records_error_and_get_all(db):
    await crud.ensure_row(db, id="d0001", name="a", requires_operator=False)
    await crud.claim(db, "d0001")
    await crud.mark_failed(db, "d0001", error="kaboom")
    rows = await crud.get_all(db)
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["error"] == "kaboom"
    assert rows[0]["attempts"] == 1


# ── runner state machine (fake migrations) ───────────────────────────


def _fake_module(migrate, verify, requires_operator=False):
    return types.SimpleNamespace(
        migrate=migrate, verify=verify, requires_operator=requires_operator
    )


def _patch_migrations(monkeypatch, mods: dict):
    """Wire discovery + import to a dict of {stem: fake module}."""
    from pathlib import Path

    available = [(stem[:5], stem, Path(f"/fake/{stem}.py")) for stem in mods]
    monkeypatch.setattr(DataMigrationRunner, "_discover", lambda self: available)
    monkeypatch.setattr(
        runner_mod.importlib, "import_module", lambda name: mods[name.rsplit(".", 1)[-1]]
    )


async def test_runner_runs_pending_and_marks_completed(db, monkeypatch):
    calls = {"migrate": 0, "verify": 0}

    def migrate():
        calls["migrate"] += 1
        return {"updated": 3}

    def verify():
        calls["verify"] += 1
        return True

    _patch_migrations(monkeypatch, {"d0001_x": _fake_module(migrate, verify)})
    outcomes = await DataMigrationRunner(db).run_pending()
    assert outcomes == [
        {"id": "d0001", "name": "d0001_x", "success": True, "summary": {"updated": 3}}
    ]
    assert calls == {"migrate": 1, "verify": 1}
    assert await crud.get_status(db, "d0001") == "completed"

    # Second run: already completed -> not re-run (idempotent skip).
    outcomes2 = await DataMigrationRunner(db).run_pending()
    assert outcomes2 == []
    assert calls == {"migrate": 1, "verify": 1}


async def test_runner_verify_failure_marks_failed(db, monkeypatch):
    _patch_migrations(
        monkeypatch,
        {"d0001_x": _fake_module(lambda: {}, lambda: False)},
    )
    outcomes = await DataMigrationRunner(db).run_pending()
    assert outcomes[0]["success"] is False
    assert await crud.get_status(db, "d0001") == "failed"


async def test_runner_exception_marks_failed_and_continues(db, monkeypatch):
    def boom():
        raise RuntimeError("qdrant down")

    ran_second = {"v": False}

    def ok_migrate():
        ran_second["v"] = True
        return {}

    _patch_migrations(
        monkeypatch,
        {
            "d0001_a": _fake_module(boom, lambda: True),
            "d0002_b": _fake_module(ok_migrate, lambda: True),
        },
    )
    outcomes = await DataMigrationRunner(db).run_pending()
    assert {o["id"]: o["success"] for o in outcomes} == {"d0001": False, "d0002": True}
    assert await crud.get_status(db, "d0001") == "failed"  # recorded, not raised
    assert ran_second["v"] is True  # batch continued past the failure


async def test_runner_skips_operator_gated(db, monkeypatch):
    ran = {"v": False}

    def migrate():
        ran["v"] = True
        return {}

    _patch_migrations(
        monkeypatch,
        {"d0002_op": _fake_module(migrate, lambda: True, requires_operator=True)},
    )
    outcomes = await DataMigrationRunner(db).run_pending()
    assert outcomes == []
    assert ran["v"] is False
    assert await crud.get_status(db, "d0002") == "operator_pending"


async def test_runner_rejects_duplicate_prefix(db, monkeypatch):
    # Two files sharing d0001 would silently drop the second (INSERT OR IGNORE
    # + completed-skip) — the runner must raise loudly instead.
    from pathlib import Path

    available = [
        ("d0001", "d0001_a", Path("/fake/d0001_a.py")),
        ("d0001", "d0001_b", Path("/fake/d0001_b.py")),
    ]
    monkeypatch.setattr(DataMigrationRunner, "_discover", lambda self: available)
    with pytest.raises(RuntimeError, match="Duplicate data-migration prefix 'd0001'"):
        await DataMigrationRunner(db).run_pending()
    # run_data_migrations swallows it (never aborts boot) but logs.
    assert await runner_mod.run_data_migrations(db) == []


def test_no_duplicate_migration_prefixes_in_tree():
    """Static guard: the REAL migration directories carry no duplicate prefixes.

    Two concurrently-merged PRs each took `d0009` (2026-08-01: #1274 + #1276) —
    the runner's runtime guard then made run_data_migrations a boot-time no-op
    on EVERY install (error swallowed, all data migrations skipped) until one
    was renamed. The runtime guard fires at deploy time on every install; this
    test fires at CI time on the offending PR's merge ref, where the collision
    is cheap to fix.
    """
    import re
    from collections import Counter

    from genesis.db._migration_discovery import discover_numbered_modules

    surfaces = [
        (
            runner_mod._DATA_MIGRATIONS_DIR,
            runner_mod._DATA_MIGRATION_PATTERN,
            "data migration",
        ),
        (
            runner_mod._DATA_MIGRATIONS_DIR.parent / "migrations",
            re.compile(r"^(\d{4})_\w+\.py$"),
            "schema migration",
        ),
    ]
    for directory, pattern, label in surfaces:
        ids = [mid for mid, _, _ in discover_numbered_modules(directory, pattern)]
        dupes = {mid: n for mid, n in Counter(ids).items() if n > 1}
        assert not dupes, (
            f"duplicate {label} prefix(es) {dupes} in {directory} — "
            "rename the newer file to the next free prefix"
        )


async def test_runner_dependency_unavailable_warns_not_errors(db, monkeypatch, caplog):
    # A cold-boot transient (embedder/Qdrant not warm) raises the sentinel: the
    # runner defers it (marked failed -> replays next boot) and logs at WARNING
    # with NO traceback — not the scary ERROR+traceback reserved for real bugs.
    import logging

    from genesis.db.data_migrations._util import MigrationDependencyUnavailable

    def cold():
        raise MigrationDependencyUnavailable("embedder unavailable after 3 attempts")

    _patch_migrations(monkeypatch, {"d0001_x": _fake_module(cold, lambda: True)})
    with caplog.at_level(logging.DEBUG, logger="genesis.db.data_migrations.runner"):
        outcomes = await DataMigrationRunner(db).run_pending()

    assert outcomes[0]["success"] is False
    assert outcomes[0].get("deferred") is True
    assert await crud.get_status(db, "d0001") == "failed"  # retryable -> replays next boot
    recs = [r for r in caplog.records if r.name == "genesis.db.data_migrations.runner"]
    assert any(r.levelno == logging.WARNING and "deferred" in r.getMessage() for r in recs)
    assert not any(r.levelno >= logging.ERROR for r in recs)  # no ERROR
    assert not any(r.exc_info for r in recs)  # no traceback attached


async def test_runner_genuine_exception_still_logs_error_with_traceback(db, monkeypatch, caplog):
    # A NON-sentinel exception (a real migration bug) must still hit ERROR+traceback —
    # the WARN demotion is scoped to the dependency-unavailable sentinel only.
    import logging

    def boom():
        raise RuntimeError("a real migration bug")

    _patch_migrations(monkeypatch, {"d0001_x": _fake_module(boom, lambda: True)})
    with caplog.at_level(logging.WARNING, logger="genesis.db.data_migrations.runner"):
        await DataMigrationRunner(db).run_pending()

    recs = [r for r in caplog.records if r.name == "genesis.db.data_migrations.runner"]
    assert any(r.levelno == logging.ERROR and r.exc_info for r in recs)
    assert await crud.get_status(db, "d0001") == "failed"


async def test_run_data_migrations_waits_boot_settle_delay(db, monkeypatch):
    # The entry point sleeps the configured boot-settle delay before running.
    slept = {"secs": None}

    async def fake_sleep(secs):
        slept["secs"] = secs

    monkeypatch.setattr(runner_mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setenv("GENESIS_DATA_MIGRATION_BOOT_DELAY_S", "7")  # overrides autouse 0
    monkeypatch.setattr(DataMigrationRunner, "_discover", lambda self: [])
    await runner_mod.run_data_migrations(db)
    assert slept["secs"] == 7.0


def test_boot_delay_rejects_non_finite_and_garbage(monkeypatch):
    # inf would make asyncio.sleep(inf) hang migrations forever; nan / garbage are
    # also nonsense — all fall back to the default rather than a hang or crash.
    for bad in ("inf", "-inf", "nan", "not-a-number", ""):
        monkeypatch.setenv("GENESIS_DATA_MIGRATION_BOOT_DELAY_S", bad)
        assert runner_mod._boot_settle_delay_s() == runner_mod._DEFAULT_BOOT_SETTLE_DELAY_S
    # A negative finite value clamps to 0 (disabled), not the default.
    monkeypatch.setenv("GENESIS_DATA_MIGRATION_BOOT_DELAY_S", "-5")
    assert runner_mod._boot_settle_delay_s() == 0.0


async def test_run_data_migrations_no_delay_when_zero(db, monkeypatch):
    slept = {"called": False}

    async def fake_sleep(secs):
        slept["called"] = True

    monkeypatch.setattr(runner_mod.asyncio, "sleep", fake_sleep)
    # autouse fixture already set the env to "0"
    monkeypatch.setattr(DataMigrationRunner, "_discover", lambda self: [])
    await runner_mod.run_data_migrations(db)
    assert slept["called"] is False


async def test_runner_redispatches_orphaned_running(db, monkeypatch):
    # A row left 'running' by a crashed prior boot must re-run.
    await crud.ensure_row(db, id="d0001", name="d0001_x", requires_operator=False)
    await crud.claim(db, "d0001")  # -> running (orphaned)
    ran = {"v": 0}
    _patch_migrations(
        monkeypatch,
        {"d0001_x": _fake_module(lambda: ran.__setitem__("v", ran["v"] + 1) or {}, lambda: True)},
    )
    await DataMigrationRunner(db).run_pending()
    assert ran["v"] == 1
    assert await crud.get_status(db, "d0001") == "completed"
