"""Tests for Genesis v3 database schema — tables, constraints, indexes, seeds."""

import json
import sqlite3

import pytest

EXPECTED_TABLES = [
    "procedural_memory",
    "observations",
    "execution_traces",
    "surplus_insights",
    "signal_weights",
    "capability_gaps",
    "speculative_claims",
    "autonomy_state",
    "outreach_history",
    "brainstorm_log",
    "user_model_cache",
    "tool_registry",
    "drive_weights",
    "cost_events",
    "budgets",
    "awareness_ticks",
    "depth_thresholds",
    "dead_letter",
    "surplus_tasks",
    "cognitive_state",
    "message_queue",
    "cc_sessions",
    "memory_links",
    "inbox_items",
    "deferred_work_queue",
    "pending_embeddings",
    "predictions",
    "calibration_curves",
    "events",
    "approval_requests",
    "task_states",
    "knowledge_units",
    "evolution_proposals",
    "telegram_messages",
    "session_bookmarks",
    "activity_log",
    "module_config",
    "telegram_topics",
    "pending_outreach",
    "task_steps",
    "memory_metadata",
    "credential_access_log",
    "session_heartbeats",
]


async def _get_tables(db):
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    return [row[0] for row in await cursor.fetchall()]


# ─── Table existence ──────────────────────────────────────────────────────────


async def test_all_tables_exist(db):
    tables = await _get_tables(db)
    for expected in EXPECTED_TABLES:
        assert expected in tables, f"Missing table: {expected}"


async def test_no_unexpected_tables(db):
    tables = await _get_tables(db)
    known = set(EXPECTED_TABLES) | {
        "memory_fts", "memory_fts_data", "memory_fts_idx",
        "memory_fts_content", "memory_fts_docsize", "memory_fts_config",
        "knowledge_fts", "knowledge_fts_data", "knowledge_fts_idx",
        "knowledge_fts_content", "knowledge_fts_docsize", "knowledge_fts_config",
        "call_site_last_run", "resolved_errors", "job_health",
        "processed_emails", "task_steps",
        "ego_cycles", "ego_proposals", "ego_state",
        "behavioral_corrections", "behavioral_themes", "behavioral_treatments",
        "memory_metadata",
        "code_modules", "code_symbols", "code_imports",
        "follow_ups", "surplus_tasks", "surplus_insights",
        "knowledge_uploads",
        "file_modifications",
    }
    for table in tables:
        assert table in known, f"Unexpected table: {table}"


# ─── Signal weights seed data ────────────────────────────────────────────────


async def test_signal_weights_seeded(db):
    cursor = await db.execute("SELECT COUNT(*) FROM signal_weights")
    count = (await cursor.fetchone())[0]
    # 10 → 16 on 2026-04-17: +6 new signals (light_cascade, sentinel,
    # guardian, surplus, autonomy activity, stale_pending_items).
    assert count == 16


async def test_signal_weights_values(db):
    cursor = await db.execute(
        "SELECT signal_name, current_weight, feeds_depths FROM signal_weights "
        "WHERE signal_name = 'critical_failure'"
    )
    row = await cursor.fetchone()
    assert row is not None
    # 2026-04-17: critical_failure moved to Micro-only at weight 0.70
    assert row[1] == 0.70
    depths = json.loads(row[2])
    assert "Micro" in depths


async def test_unprocessed_memory_backlog_migration_removes_existing_row(db):
    """Migration must clear stale unprocessed_memory_backlog rows on upgrade.

    Fresh-DB seeding never inserts this row (removed 2026-04-11), so the
    standard seed/idempotent tests cover only the new-install path. This
    test simulates the upgrade path: a DB that already has the row from a
    pre-cleanup install, then runs the migration, and asserts the row is
    gone. Also verifies idempotency by running the migration twice.
    """
    from genesis.db.schema._migrations import _migrate_add_columns

    # Inject the legacy row exactly as it appeared in pre-2026-04-11 seeds.
    await db.execute(
        "INSERT OR REPLACE INTO signal_weights "
        "(signal_name, source_mcp, current_weight, initial_weight, "
        " min_weight, max_weight, feeds_depths) "
        "VALUES ('unprocessed_memory_backlog', 'memory_mcp', "
        "        0.30, 0.30, 0.0, 1.0, '[\"Deep\"]')"
    )
    await db.commit()

    # Sanity check: row exists before migration runs.
    cur = await db.execute(
        "SELECT COUNT(*) FROM signal_weights "
        "WHERE signal_name = 'unprocessed_memory_backlog'"
    )
    assert (await cur.fetchone())[0] == 1

    # Run migration once — row should be removed.
    await _migrate_add_columns(db)
    await db.commit()
    cur = await db.execute(
        "SELECT COUNT(*) FROM signal_weights "
        "WHERE signal_name = 'unprocessed_memory_backlog'"
    )
    assert (await cur.fetchone())[0] == 0

    # Run migration again — must be idempotent (no error, still zero).
    await _migrate_add_columns(db)
    await db.commit()
    cur = await db.execute(
        "SELECT COUNT(*) FROM signal_weights "
        "WHERE signal_name = 'unprocessed_memory_backlog'"
    )
    assert (await cur.fetchone())[0] == 0


# ─── Drive weights seed data ─────────────────────────────────────────────────


async def test_drive_weights_seeded(db):
    cursor = await db.execute("SELECT COUNT(*) FROM drive_weights")
    count = (await cursor.fetchone())[0]
    assert count == 4


async def test_drive_weights_bounds(db):
    cursor = await db.execute("SELECT drive_name, min_weight, max_weight FROM drive_weights")
    rows = await cursor.fetchall()
    for row in rows:
        assert row[1] == 0.10, f"{row[0]} min_weight should be 0.10"
        assert row[2] == 0.50, f"{row[0]} max_weight should be 0.50"


# ─── CHECK constraints ───────────────────────────────────────────────────────


async def test_observations_rejects_invalid_priority(db):
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO observations (id, source, type, content, priority, created_at) "
            "VALUES ('test', 'test', 'test', 'test', 'INVALID', '2026-01-01T00:00:00')"
        )


async def test_autonomy_state_rejects_level_out_of_range(db):
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO autonomy_state (id, category, current_level, earned_level, updated_at) "
            "VALUES ('test', 'test', 8, 1, '2026-01-01T00:00:00')"
        )


async def test_surplus_rejects_invalid_drive(db):
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO surplus_insights "
            "(id, content, source_task_type, generating_model, drive_alignment, "
            "created_at, ttl) "
            "VALUES ('test', 'c', 's', 'm', 'INVALID', '2026-01-01', '2026-02-01')"
        )


async def test_tool_registry_rejects_invalid_type(db):
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO tool_registry (id, name, category, description, tool_type, created_at) "
            "VALUES ('test', 'n', 'c', 'd', 'INVALID', '2026-01-01')"
        )


async def test_cost_events_rejects_invalid_event_type(db):
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO cost_events (id, event_type, cost_usd, created_at) "
            "VALUES ('test', 'INVALID', 0.01, '2026-01-01')"
        )


async def test_budgets_rejects_invalid_budget_type(db):
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO budgets (id, budget_type, limit_usd, created_at, updated_at) "
            "VALUES ('test', 'INVALID', 10.0, '2026-01-01', '2026-01-01')"
        )


# ─── NOT NULL constraints ────────────────────────────────────────────────────


async def test_procedural_memory_requires_task_type(db):
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO procedural_memory "
            "(id, task_type, principle, steps, tools_used, context_tags, created_at) "
            "VALUES ('test', NULL, 'p', '[]', '[]', '[]', '2026-01-01')"
        )


async def test_execution_traces_requires_user_request(db):
    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO execution_traces (id, user_request, plan, sub_agents, created_at) "
            "VALUES ('test', NULL, '[]', '[]', '2026-01-01')"
        )


# ─── Indexes exist ───────────────────────────────────────────────────────────


async def test_key_indexes_exist(db):
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
    )
    indexes = {row[0] for row in await cursor.fetchall()}
    expected = {
        "idx_procedural_task_type",
        "idx_observations_source",
        "idx_observations_priority",
        "idx_traces_outcome",
        "idx_surplus_status",
        "idx_gaps_status",
        "idx_claims_speculative",
        "idx_outreach_channel",
        "idx_brainstorm_type",
        # GROUNDWORK(multi-person)
        "idx_observations_person",
        "idx_outreach_person",
        "idx_autonomy_person",
        "idx_traces_person",
        # cost tracking
        "idx_cost_events_task",
        "idx_cost_events_created",
        "idx_cost_events_person",
        "idx_cost_events_type",
        "idx_budgets_type",
        "idx_budgets_active",
        # awareness loop
        "idx_ticks_depth",
        "idx_ticks_created",
        # dead letter
        "idx_dead_letter_status",
        "idx_dead_letter_provider",
    }
    for idx in expected:
        assert idx in indexes, f"Missing index: {idx}"


# ─── Seed idempotency ────────────────────────────────────────────────────────


async def test_seed_is_idempotent(db):
    from genesis.db.schema import seed_data

    await seed_data(db)
    await db.commit()
    cursor = await db.execute("SELECT COUNT(*) FROM signal_weights")
    # 10 → 16 on 2026-04-17: +6 new signals (awareness scoring overhaul).
    assert (await cursor.fetchone())[0] == 16
    cursor = await db.execute("SELECT COUNT(*) FROM drive_weights")
    assert (await cursor.fetchone())[0] == 4


# ─── Awareness Loop tables ──────────────────────────────────────────────────


async def test_awareness_ticks_table_exists(db):
    """awareness_ticks table was created by create_all_tables."""
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='awareness_ticks'"
    )
    row = await cursor.fetchone()
    assert row is not None


async def test_depth_thresholds_table_exists(db):
    """depth_thresholds table was created by create_all_tables."""
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='depth_thresholds'"
    )
    row = await cursor.fetchone()
    assert row is not None


async def test_dead_letter_table_columns(db):
    """dead_letter table has expected columns."""
    cursor = await db.execute("PRAGMA table_info(dead_letter)")
    cols = {row[1] for row in await cursor.fetchall()}
    expected = {
        "id", "operation_type", "payload", "target_provider",
        "failure_reason", "created_at", "retry_count", "last_retry_at", "status",
    }
    assert expected == cols


async def test_budget_seed_data(db):
    """Budget seed data is present after seed_data()."""
    cursor = await db.execute("SELECT id, budget_type, limit_usd FROM budgets ORDER BY limit_usd")
    rows = await cursor.fetchall()
    assert len(rows) == 3
    assert rows[0]["id"] == "budget_daily"
    assert rows[0]["limit_usd"] == 2.00
    assert rows[2]["id"] == "budget_monthly"
    assert rows[2]["limit_usd"] == 30.00


async def test_budget_seed_idempotent(db):
    """Running seed_data twice doesn't duplicate budget rows."""
    from genesis.db.schema import seed_data

    await seed_data(db)
    await db.commit()
    cursor = await db.execute("SELECT COUNT(*) FROM budgets")
    assert (await cursor.fetchone())[0] == 3


async def test_depth_thresholds_seeded(db):
    """depth_thresholds has seed data for all four depths."""
    cursor = await db.execute("SELECT depth_name FROM depth_thresholds ORDER BY depth_name")
    rows = await cursor.fetchall()
    names = [r["depth_name"] for r in rows]
    assert names == ["Deep", "Light", "Micro", "Strategic"]
