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
