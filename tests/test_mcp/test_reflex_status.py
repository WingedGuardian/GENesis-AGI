"""Tests for the reflex_status MCP tool (read-only nerve surface)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import aiosqlite
import pytest

from genesis.db.crud import reflex_signals as crud
from genesis.db.schema import create_all_tables
from genesis.reflex.config import ReflexConfig

T0 = "2026-07-21T00:00:00+00:00"
T1 = "2026-07-21T01:00:00+00:00"


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


async def _seed(db, fingerprint, *, class_key="KeyErrorxmemory", now=T0):
    return await crud.upsert_occurrence(
        db,
        fingerprint=fingerprint,
        class_key=class_key,
        task_name="mem-sync",
        subsystem="memory",
        error_type="KeyError",
        error_message="KeyError: 'x'",
        traceback_tail="memory/sync.py:_apply",
        now=now,
    )


async def _run(db):
    svc = MagicMock()
    svc._db = db
    with patch("genesis.mcp.health_mcp._service", svc):
        from genesis.mcp.health.reflex_status import _impl_reflex_status

        return await _impl_reflex_status()


@pytest.mark.asyncio
async def test_unavailable_without_db():
    svc = MagicMock()
    svc._db = None
    with patch("genesis.mcp.health_mcp._service", svc):
        from genesis.mcp.health.reflex_status import _impl_reflex_status

        result = await _impl_reflex_status()
    assert result["status"] == "unavailable"


@pytest.mark.asyncio
async def test_reports_counts_classes_and_recents(db):
    await _seed(db, "fp00000000000001", now=T0)
    await _seed(db, "fp00000000000002", class_key="ValueErrorxrouting", now=T1)
    row = await crud.get_by_fingerprint(db, "fp00000000000001")
    await crud.set_status(db, signal_id=row["id"], expected_from="new", to="diagnosing", now=T1)

    with patch(
        "genesis.reflex.config.load_reflex_config",
        return_value=ReflexConfig(ingest_enabled=True),
    ):
        result = await _run(db)

    assert result["status"] == "ok"
    assert result["ingest_enabled"] is True
    assert result["total_signals"] == 2
    assert result["counts_by_status"] == {"new": 1, "diagnosing": 1}
    assert {c["class_key"] for c in result["top_classes"]} == {
        "KeyErrorxmemory",
        "ValueErrorxrouting",
    }
    # most-recently-seen first
    assert [r["fingerprint"] for r in result["recent_signals"]] == [
        "fp00000000000002",
        "fp00000000000001",
    ]
    assert result["recent_signals"][0]["occurrence_count"] == 1


@pytest.mark.asyncio
async def test_config_failure_degrades_enabled_to_none(db):
    await _seed(db, "fp00000000000001")
    with patch(
        "genesis.reflex.config.load_reflex_config",
        side_effect=RuntimeError("bad yaml"),
    ):
        result = await _run(db)
    assert result["status"] == "ok"
    assert result["ingest_enabled"] is None
    assert result["total_signals"] == 1
