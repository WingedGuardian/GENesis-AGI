"""Tests for campaign MCP tools — standalone fallback and runner-absent paths."""

from __future__ import annotations

from unittest.mock import patch

import aiosqlite
import pytest

import genesis.mcp.health.campaign_tools as ct_mod


@pytest.fixture
async def _test_db(tmp_path):
    """In-memory-ish DB with campaigns + campaign_runs tables."""
    db_path = tmp_path / "test.db"
    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    await db.execute("""
        CREATE TABLE IF NOT EXISTS campaigns (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            strategy_doc_path TEXT NOT NULL,
            cron_cadence TEXT NOT NULL,
            model TEXT DEFAULT 'sonnet',
            effort TEXT DEFAULT 'medium',
            session_profile TEXT DEFAULT 'research',
            status TEXT DEFAULT 'active',
            state_json TEXT DEFAULT '{}',
            pre_checks TEXT DEFAULT '[]',
            max_daily_cost_usd REAL DEFAULT 1.0,
            created_at TEXT NOT NULL,
            paused_at TEXT,
            last_run_at TEXT,
            total_runs INTEGER DEFAULT 0,
            total_cost_usd REAL DEFAULT 0.0
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS campaign_runs (
            id TEXT PRIMARY KEY,
            campaign_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            trigger_type TEXT DEFAULT 'scheduled',
            outcome TEXT DEFAULT 'pending',
            skip_reason TEXT,
            summary TEXT,
            cost_usd REAL DEFAULT 0.0,
            session_id TEXT,
            state_snapshot TEXT
        )
    """)
    # Seed a test campaign
    await db.execute(
        """INSERT INTO campaigns
           (id, name, strategy_doc_path, cron_cadence, status, state_json,
            max_daily_cost_usd, created_at, total_runs, total_cost_usd)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "test-id-1", "test-campaign", "/tmp/strategy.md",
            "0 */8 * * *", "active", '{"posts_today": 0}',
            1.0, "2026-06-01T00:00:00+00:00", 3, 0.50,
        ),
    )
    await db.commit()
    try:
        yield db
    finally:
        await db.close()


class TestStandaloneFallback:
    """Campaign tools should work when _db=None by using _get_db() fallback."""

    @pytest.mark.asyncio
    async def test_campaign_status_standalone(self, _test_db, tmp_path):
        """_impl_campaign_status should fall back to _get_db when _db is None."""
        old_db, old_runner = ct_mod._db, ct_mod._runner
        try:
            ct_mod._db = None
            ct_mod._runner = None
            # Patch _get_db to return our test DB
            with patch.object(ct_mod, "_get_db", return_value=_test_db):
                result = await ct_mod._impl_campaign_status("test-campaign")
            assert "error" not in result
            assert result["name"] == "test-campaign"
            assert result["cadence"] == "0 */8 * * *"
            assert result["status"] == "active"
            assert result["total_runs"] == 3
        finally:
            ct_mod._db = old_db
            ct_mod._runner = old_runner

    @pytest.mark.asyncio
    async def test_campaign_list_standalone(self, _test_db):
        """_impl_campaign_list should fall back to _get_db when _db is None."""
        old_db, old_runner = ct_mod._db, ct_mod._runner
        try:
            ct_mod._db = None
            ct_mod._runner = None
            with patch.object(ct_mod, "_get_db", return_value=_test_db):
                result = await ct_mod._impl_campaign_list()
            assert "error" not in result
            assert result["count"] == 1
            assert result["campaigns"][0]["name"] == "test-campaign"
        finally:
            ct_mod._db = old_db
            ct_mod._runner = old_runner

    @pytest.mark.asyncio
    async def test_campaign_status_not_found(self, _test_db):
        """Should return error for non-existent campaign."""
        old_db, old_runner = ct_mod._db, ct_mod._runner
        try:
            ct_mod._db = None
            ct_mod._runner = None
            with patch.object(ct_mod, "_get_db", return_value=_test_db):
                result = await ct_mod._impl_campaign_status("nonexistent")
            assert "error" in result
            assert "not found" in result["error"]
        finally:
            ct_mod._db = old_db
            ct_mod._runner = old_runner


class TestRunnerAbsent:
    """Tests for operations when _runner is None (standalone MCP mode)."""

    @pytest.mark.asyncio
    async def test_campaign_trigger_without_runner(self):
        """_impl_campaign_trigger should return helpful error without runner."""
        old_db, old_runner = ct_mod._db, ct_mod._runner
        try:
            ct_mod._db = None
            ct_mod._runner = None
            result = await ct_mod._impl_campaign_trigger("test-campaign")
            assert "error" in result
            assert "main Genesis server" in result["error"]
            assert "campaign_status" in result["error"]
        finally:
            ct_mod._db = old_db
            ct_mod._runner = old_runner

    @pytest.mark.asyncio
    async def test_campaign_update_cadence_without_runner(self, _test_db, tmp_path):
        """Cadence update should succeed in DB and include restart note."""
        db_path = tmp_path / "test.db"
        old_db, old_runner = ct_mod._db, ct_mod._runner
        try:
            ct_mod._db = None
            ct_mod._runner = None
            # _get_db returns the test DB; own_db=True will close it after use
            with patch.object(ct_mod, "_get_db", return_value=_test_db):
                result = await ct_mod._impl_campaign_update(
                    "test-campaign", cron_cadence="0 */12 * * *",
                )
            assert "error" not in result
            assert "cron_cadence" in result["updated"]
            assert "note" in result
            assert "Restart genesis-server" in result["note"]

            # Verify DB was actually updated (re-open since own_db closed it)
            async with aiosqlite.connect(str(db_path)) as verify_db:
                verify_db.row_factory = aiosqlite.Row
                cursor = await verify_db.execute(
                    "SELECT cron_cadence FROM campaigns WHERE name = ?",
                    ("test-campaign",),
                )
                row = await cursor.fetchone()
                assert row["cron_cadence"] == "0 */12 * * *"
        finally:
            ct_mod._db = old_db
            ct_mod._runner = old_runner

    @pytest.mark.asyncio
    async def test_campaign_pause_without_runner(self, _test_db):
        """Pause should succeed in DB and include note when runner absent."""
        old_db, old_runner = ct_mod._db, ct_mod._runner
        try:
            ct_mod._db = None
            ct_mod._runner = None
            with patch.object(ct_mod, "_get_db", return_value=_test_db):
                result = await ct_mod._impl_campaign_pause("test-campaign")
            assert result["status"] == "paused"
            assert "note" in result
            assert "Restart genesis-server" in result["note"]
        finally:
            ct_mod._db = old_db
            ct_mod._runner = old_runner

    @pytest.mark.asyncio
    async def test_campaign_resume_without_runner(self, _test_db):
        """Resume should succeed in DB and include note when runner absent."""
        old_db, old_runner = ct_mod._db, ct_mod._runner
        try:
            ct_mod._db = None
            ct_mod._runner = None
            with patch.object(ct_mod, "_get_db", return_value=_test_db):
                result = await ct_mod._impl_campaign_resume("test-campaign")
            assert result["status"] == "active"
            assert "note" in result
        finally:
            ct_mod._db = old_db
            ct_mod._runner = old_runner


class TestCampaignCreateValidation:
    """campaign_create must validate `profile` against VALID_PROFILES before persisting
    (mirrors user_job_tools / direct_session_tools). Guards against a bogus profile that
    would silently fail every tick at DirectSession init."""

    @pytest.mark.asyncio
    async def test_create_rejects_invalid_profile(self, _test_db):
        """An unknown profile is rejected and NOTHING is persisted."""
        old_db, old_runner = ct_mod._db, ct_mod._runner
        try:
            ct_mod._db = _test_db
            ct_mod._runner = None
            result = await ct_mod._impl_campaign_create(
                name="bogus-profile-campaign",
                strategy_doc_path="/tmp/s.md",
                cron_cadence="0 */8 * * *",
                profile="not-a-real-profile",
            )
            assert "error" in result
            assert "Invalid profile" in result["error"]
            cursor = await _test_db.execute(
                "SELECT COUNT(*) AS n FROM campaigns WHERE name = ?",
                ("bogus-profile-campaign",),
            )
            row = await cursor.fetchone()
            assert row["n"] == 0
        finally:
            ct_mod._db = old_db
            ct_mod._runner = old_runner

    @pytest.mark.asyncio
    async def test_create_accepts_valid_profile(self, _test_db):
        """A valid profile passes validation and flows through to creation."""
        from unittest.mock import AsyncMock

        old_db, old_runner = ct_mod._db, ct_mod._runner
        try:
            ct_mod._db = _test_db
            ct_mod._runner = None
            with patch(
                "genesis.db.crud.campaigns.get_campaign_by_name",
                new=AsyncMock(return_value=None),
            ), patch(
                "genesis.db.crud.campaigns.create_campaign", new=AsyncMock(),
            ) as mock_create:
                result = await ct_mod._impl_campaign_create(
                    name="valid-profile-campaign",
                    strategy_doc_path="/tmp/s.md",
                    cron_cadence="0 */8 * * *",
                    profile="community-responder",
                )
            assert "error" not in result
            mock_create.assert_awaited_once()
            assert mock_create.call_args.kwargs["session_profile"] == "community-responder"
        finally:
            ct_mod._db = old_db
            ct_mod._runner = old_runner


class TestWiredDbPath:
    """When _db is wired (e.g., via init_campaign_tools), _get_db is not called."""

    @pytest.mark.asyncio
    async def test_campaign_status_uses_wired_db(self, _test_db):
        """When _db is set, _get_db should NOT be called."""
        old_db, old_runner = ct_mod._db, ct_mod._runner
        try:
            ct_mod._db = _test_db
            ct_mod._runner = None
            with patch.object(ct_mod, "_get_db") as mock_get:
                result = await ct_mod._impl_campaign_status("test-campaign")
            mock_get.assert_not_called()
            assert result["name"] == "test-campaign"
        finally:
            ct_mod._db = old_db
            ct_mod._runner = old_runner
