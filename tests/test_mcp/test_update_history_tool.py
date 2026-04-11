"""Tests for genesis.mcp.health.update_history._impl_update_history_recent."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from genesis.mcp.health.update_history import _impl_update_history_recent


async def _init_update_history(db_path: Path) -> None:
    """Create the update_history table (matches 0001 migration)."""
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            """
            CREATE TABLE update_history (
                id TEXT PRIMARY KEY,
                old_tag TEXT NOT NULL,
                new_tag TEXT NOT NULL,
                old_commit TEXT NOT NULL,
                new_commit TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('success', 'failed', 'rolled_back')),
                rollback_tag TEXT,
                failure_reason TEXT,
                degraded_subsystems TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL
            )
            """
        )
        await db.commit()


async def _insert_entry(db_path: Path, **kwargs) -> None:
    defaults = {
        "id": "id-1",
        "old_tag": "v0.3.0",
        "new_tag": "v0.3.1",
        "old_commit": "abc1234",
        "new_commit": "def5678",
        "status": "success",
        "rollback_tag": None,
        "failure_reason": None,
        "degraded_subsystems": None,
        "started_at": "2026-04-10T12:00:00+00:00",
        "completed_at": "2026-04-10T12:00:30+00:00",
    }
    defaults.update(kwargs)
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "INSERT INTO update_history "
            "(id, old_tag, new_tag, old_commit, new_commit, status, "
            " rollback_tag, failure_reason, degraded_subsystems, "
            " started_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                defaults["id"],
                defaults["old_tag"],
                defaults["new_tag"],
                defaults["old_commit"],
                defaults["new_commit"],
                defaults["status"],
                defaults["rollback_tag"],
                defaults["failure_reason"],
                defaults["degraded_subsystems"],
                defaults["started_at"],
                defaults["completed_at"],
            ),
        )
        await db.commit()


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Point the tool at an empty tmp_path-backed DB."""
    db_path = tmp_path / "genesis.db"
    monkeypatch.setattr(
        "genesis.mcp.health.update_history._DB_PATH", db_path,
    )
    return db_path


class TestUpdateHistoryRecent:

    @pytest.mark.asyncio
    async def test_missing_db_returns_note(self, tmp_db) -> None:
        """Fresh install, no genesis.db — must report state, not hide it."""
        assert not tmp_db.exists()
        result = await _impl_update_history_recent()

        assert result["count"] == 0
        assert result["success_rate"] is None
        assert result["entries"] == []
        assert "not found" in result["note"].lower()

    @pytest.mark.asyncio
    async def test_missing_table_returns_note(self, tmp_db) -> None:
        """DB exists but migration hasn't run — must report state, not hide it."""
        # Create an empty database (no update_history table).
        async with aiosqlite.connect(str(tmp_db)) as db:
            await db.execute("CREATE TABLE placeholder (id INTEGER)")
            await db.commit()

        result = await _impl_update_history_recent()
        assert result["count"] == 0
        assert result["success_rate"] is None
        assert result["entries"] == []
        assert "update_history" in result["note"]

    @pytest.mark.asyncio
    async def test_empty_table(self, tmp_db) -> None:
        """Table exists but no rows — success_rate is None, empty entries."""
        await _init_update_history(tmp_db)

        result = await _impl_update_history_recent()
        assert result["count"] == 0
        assert result["success_rate"] is None
        assert result["entries"] == []
        assert "note" not in result

    @pytest.mark.asyncio
    async def test_single_success(self, tmp_db) -> None:
        await _init_update_history(tmp_db)
        await _insert_entry(tmp_db, id="e1", status="success")

        result = await _impl_update_history_recent()
        assert result["count"] == 1
        assert result["success_rate"] == 1.0
        assert result["entries"][0]["id"] == "e1"
        assert result["entries"][0]["status"] == "success"
        assert result["entries"][0]["old_tag"] == "v0.3.0"
        assert result["entries"][0]["new_tag"] == "v0.3.1"

    @pytest.mark.asyncio
    async def test_mixed_status_success_rate(self, tmp_db) -> None:
        """2 success + 1 failed + 1 rolled_back = 0.5 success rate."""
        await _init_update_history(tmp_db)
        await _insert_entry(
            tmp_db, id="e1", status="success",
            started_at="2026-04-10T10:00:00+00:00",
        )
        await _insert_entry(
            tmp_db, id="e2", status="success",
            started_at="2026-04-10T11:00:00+00:00",
        )
        await _insert_entry(
            tmp_db, id="e3", status="failed",
            failure_reason="migration failed",
            started_at="2026-04-10T12:00:00+00:00",
        )
        await _insert_entry(
            tmp_db, id="e4", status="rolled_back",
            rollback_tag="v0.3.0", degraded_subsystems="memory,router",
            started_at="2026-04-10T13:00:00+00:00",
        )

        result = await _impl_update_history_recent()
        assert result["count"] == 4
        assert result["success_rate"] == 0.5
        # Newest first
        assert result["entries"][0]["id"] == "e4"
        assert result["entries"][0]["status"] == "rolled_back"
        assert result["entries"][0]["degraded_subsystems"] == "memory,router"
        assert result["entries"][-1]["id"] == "e1"

    @pytest.mark.asyncio
    async def test_limit_respects_bounds(self, tmp_db) -> None:
        """Limit is clamped to [1, 100] and the clamp is reported to the caller."""
        await _init_update_history(tmp_db)
        for i in range(5):
            await _insert_entry(
                tmp_db, id=f"e{i}", status="success",
                started_at=f"2026-04-10T{10 + i:02d}:00:00+00:00",
            )

        # Under-bound → clamped to 1, reported via limit_clamped
        result = await _impl_update_history_recent(limit=0)
        assert result["count"] == 1
        assert result["effective_limit"] == 1
        assert result["requested_limit"] == 0
        assert result["limit_clamped"] is True

        # In-range → effective_limit present, clamp fields absent
        result = await _impl_update_history_recent(limit=3)
        assert result["count"] == 3
        assert result["effective_limit"] == 3
        assert "limit_clamped" not in result
        assert "requested_limit" not in result

        # Over-bound → clamped to 100 (we only have 5 → returns 5)
        result = await _impl_update_history_recent(limit=9999)
        assert result["count"] == 5
        assert result["effective_limit"] == 100
        assert result["requested_limit"] == 9999
        assert result["limit_clamped"] is True

    @pytest.mark.asyncio
    async def test_failure_details_surface(self, tmp_db) -> None:
        """Failure reason and degraded subsystems are returned verbatim."""
        await _init_update_history(tmp_db)
        await _insert_entry(
            tmp_db, id="f1", status="failed",
            failure_reason="health check timeout after 60s",
            degraded_subsystems="awareness,memory",
        )

        result = await _impl_update_history_recent()
        entry = result["entries"][0]
        assert entry["failure_reason"] == "health check timeout after 60s"
        assert entry["degraded_subsystems"] == "awareness,memory"
