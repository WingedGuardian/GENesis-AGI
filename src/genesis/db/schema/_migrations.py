"""Schema migrations, table creation, and seed data insertion."""

from __future__ import annotations

import contextlib
import logging

import aiosqlite

from genesis.db.schema._tables import (
    BUDGET_SEED,
    DEPTH_THRESHOLDS_SEED,
    DRIVE_WEIGHTS_SEED,
    FTS5_DDL,
    INDEXES,
    KNOWLEDGE_FTS5_DDL,
    SIGNAL_WEIGHTS_SEED,
    TABLES,
)

logger = logging.getLogger(__name__)


async def create_all_tables(db: aiosqlite.Connection) -> None:
    """Create all Genesis tables and indexes."""
    for ddl in TABLES.values():
        await db.execute(ddl)

    # FTS5 — skip if not available (e.g., some in-memory test builds)
    with contextlib.suppress(Exception):
        await db.execute(FTS5_DDL)
    with contextlib.suppress(Exception):
        await db.execute(KNOWLEDGE_FTS5_DDL)

    # Schema migrations BEFORE indexes — migrations add columns that indexes may reference
    await _migrate_add_columns(db)

    for idx in INDEXES:
        await db.execute(idx)


async def _try_alter(db: aiosqlite.Connection, sql: str, label: str) -> None:
    """Run an ALTER TABLE idempotently — suppress 'duplicate column', log real errors."""
    try:
        await db.execute(sql)
    except Exception as exc:
        msg = str(exc).lower()
        if "duplicate column" not in msg and "already exists" not in msg:
            logger.error("Migration %s failed: %s", label, exc, exc_info=True)


async def _migrate_add_columns(db: aiosqlite.Connection) -> None:
    """Idempotent ALTER TABLE migrations for columns added after Phase 0."""

    # Phase 7: quarantined flag on procedural_memory
    await _try_alter(db,
        "ALTER TABLE procedural_memory ADD COLUMN quarantined INTEGER NOT NULL DEFAULT 0",
        "procedural_memory.quarantined")

    # Phase 8: delivery_id on outreach_history
    await _try_alter(db,
        "ALTER TABLE outreach_history ADD COLUMN delivery_id TEXT",
        "outreach_history.delivery_id")

    # Dedup enhancement: content_hash on outreach_history
    await _try_alter(db,
        "ALTER TABLE outreach_history ADD COLUMN content_hash TEXT",
        "outreach_history.content_hash")

    # Inbox audit: retry_count on inbox_items
    await _try_alter(db,
        "ALTER TABLE inbox_items ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
        "inbox_items.retry_count")

    # Inbox audit: evaluated_content on inbox_items (for delta-only re-evaluation)
    await _try_alter(db,
        "ALTER TABLE inbox_items ADD COLUMN evaluated_content TEXT",
        "inbox_items.evaluated_content")

    # Phase 9: thread_id on cc_sessions (for forum topic multi-session)
    await _try_alter(db,
        "ALTER TABLE cc_sessions ADD COLUMN thread_id TEXT",
        "cc_sessions.thread_id")

    # Phase 9: rate limit tracking on cc_sessions
    await _try_alter(db,
        "ALTER TABLE cc_sessions ADD COLUMN rate_limited_at TEXT",
        "cc_sessions.rate_limited_at")
    await _try_alter(db,
        "ALTER TABLE cc_sessions ADD COLUMN rate_limit_resumes_at TEXT",
        "cc_sessions.rate_limit_resumes_at")

    # Post-Phase-9: content_hash for observation dedup
    await _try_alter(db,
        "ALTER TABLE observations ADD COLUMN content_hash TEXT",
        "observations.content_hash")

    # Procedure activation: tier + tool trigger for layered procedure surfacing
    await _try_alter(db,
        "ALTER TABLE procedural_memory ADD COLUMN activation_tier TEXT NOT NULL DEFAULT 'L4'",
        "procedural_memory.activation_tier")
    await _try_alter(db,
        "ALTER TABLE procedural_memory ADD COLUMN tool_trigger TEXT",
        "procedural_memory.tool_trigger")

    # Reflection starvation fix: add micro_count_since_light signal weight
    await db.execute(
        "INSERT OR IGNORE INTO signal_weights "
        "(signal_name, source_mcp, current_weight, initial_weight, min_weight, max_weight, feeds_depths) "
        "VALUES ('micro_count_since_light', 'awareness_loop', 0.5, 0.5, 0.0, 1.0, '[\"Light\"]')"
    )

    # Reflection starvation fix: tighten strategic ceiling from 7d to 3d
    # Only apply if still at default 604800 to avoid overwriting manual tuning
    await db.execute(
        "UPDATE depth_thresholds SET ceiling_window_seconds = 259200 "
        "WHERE depth_name = 'Strategic' AND ceiling_window_seconds = 604800"
    )

    # Threshold retuning 2026-03-21: lower conservative defaults that produced
    # only ~12 reflections across 6800 ticks.  Guard conditions prevent
    # overwriting manually tuned values.
    await db.execute(
        "UPDATE depth_thresholds SET threshold = 0.30 "
        "WHERE depth_name = 'Micro' AND threshold = 0.50"
    )
    await db.execute(
        "UPDATE depth_thresholds SET threshold = 0.60 "
        "WHERE depth_name = 'Light' AND threshold = 0.80"
    )
    await db.execute(
        "UPDATE depth_thresholds SET threshold = 0.45 "
        "WHERE depth_name = 'Deep' AND threshold = 0.55"
    )

    # Dashboard Phase 4: CC shadow cost tracking on cc_sessions
    await _try_alter(db,
        "ALTER TABLE cc_sessions ADD COLUMN cost_usd REAL DEFAULT 0.0",
        "cc_sessions.cost_usd")
    await _try_alter(db,
        "ALTER TABLE cc_sessions ADD COLUMN input_tokens INTEGER DEFAULT 0",
        "cc_sessions.input_tokens")
    await _try_alter(db,
        "ALTER TABLE cc_sessions ADD COLUMN output_tokens INTEGER DEFAULT 0",
        "cc_sessions.output_tokens")

    # Dashboard Phase 4: call site last run tracking
    # CREATE TABLE IF NOT EXISTS is inherently idempotent — no suppress needed
    await db.execute("""
        CREATE TABLE IF NOT EXISTS call_site_last_run (
            call_site_id TEXT PRIMARY KEY,
            last_run_at TEXT NOT NULL,
            provider_used TEXT,
            model_id TEXT,
            response_text TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            success INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        )
    """)

    # Dashboard Phase 5: backfill call_site_last_run from cost_events history
    # Uses a correlated subquery to select columns from the actual most-recent row
    # per call site (not arbitrary values from GROUP BY).
    try:
        await db.execute("""
            INSERT OR IGNORE INTO call_site_last_run
                (call_site_id, last_run_at, provider_used, model_id,
                 response_text, input_tokens, output_tokens, success, updated_at)
            SELECT
                json_extract(ce.metadata, '$.call_site'),
                ce.created_at,
                ce.provider,
                ce.model,
                NULL,
                ce.input_tokens,
                ce.output_tokens,
                1,
                ce.created_at
            FROM cost_events ce
            WHERE json_extract(ce.metadata, '$.call_site') IS NOT NULL
              AND ce.created_at = (
                  SELECT MAX(ce2.created_at)
                  FROM cost_events ce2
                  WHERE json_extract(ce2.metadata, '$.call_site') = json_extract(ce.metadata, '$.call_site')
              )
        """)
    except Exception:
        logger.warning("Backfill of call_site_last_run from cost_events skipped", exc_info=True)

    # Job health persistence — survives restarts (was in-memory only before)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS job_health (
            job_name         TEXT PRIMARY KEY,
            last_run         TEXT,
            last_success     TEXT,
            last_failure     TEXT,
            last_error       TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            total_runs       INTEGER NOT NULL DEFAULT 0,
            total_successes  INTEGER NOT NULL DEFAULT 0,
            total_failures   INTEGER NOT NULL DEFAULT 0,
            updated_at       TEXT NOT NULL
        )
    """)

    # Dashboard Phase 4: manual error resolution tracking
    # CREATE TABLE IF NOT EXISTS is inherently idempotent — no suppress needed
    await db.execute("""
        CREATE TABLE IF NOT EXISTS resolved_errors (
            id TEXT PRIMARY KEY,
            error_group_key TEXT NOT NULL UNIQUE,
            resolved_by TEXT NOT NULL DEFAULT 'user',
            resolved_at TEXT NOT NULL,
            notes TEXT
        )
    """)

    # Telegram V2 deferred: add direction column + rebuild table to replace
    # the old UNIQUE(chat_id, message_id) with UNIQUE(chat_id, message_id, direction).
    # SQLite cannot ALTER constraints, so we must rebuild the table.
    try:
        # Check if migration is needed (direction column doesn't exist yet)
        cursor = await db.execute("PRAGMA table_info(telegram_messages)")
        columns = {row[1] for row in await cursor.fetchall()}
        if "direction" not in columns:
            await db.execute("""
                CREATE TABLE telegram_messages_new (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id          INTEGER NOT NULL,
                    message_id       INTEGER NOT NULL,
                    thread_id        INTEGER,
                    sender           TEXT NOT NULL,
                    content          TEXT NOT NULL,
                    timestamp        TEXT NOT NULL,
                    reply_to_message_id INTEGER,
                    direction        TEXT NOT NULL DEFAULT 'inbound',
                    UNIQUE(chat_id, message_id, direction)
                )
            """)
            # Copy existing data, flipping negative IDs to positive + outbound
            await db.execute("""
                INSERT OR IGNORE INTO telegram_messages_new
                    (id, chat_id, message_id, thread_id, sender, content,
                     timestamp, reply_to_message_id, direction)
                SELECT id, chat_id,
                       CASE WHEN message_id < 0 THEN -message_id ELSE message_id END,
                       thread_id, sender, content, timestamp, reply_to_message_id,
                       CASE WHEN message_id < 0 THEN 'outbound' ELSE 'inbound' END
                FROM telegram_messages
            """)
            await db.execute("DROP TABLE telegram_messages")
            await db.execute(
                "ALTER TABLE telegram_messages_new RENAME TO telegram_messages"
            )
            await db.commit()
            logger.info("telegram_messages table rebuilt with direction column")
    except Exception:
        logger.error("telegram_messages direction migration failed", exc_info=True)
        raise  # Don't continue with a potentially broken schema

    # Fix tool_registry CHECK constraint: add 'provider' to allowed tool_types.
    # SQLite cannot ALTER CHECK constraints, so rebuild the table.
    try:
        cursor = await db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tool_registry'"
        )
        row = await cursor.fetchone()
        if row and "'provider'" not in (row[0] or ""):
            await db.execute("""
                CREATE TABLE tool_registry_new (
                    id               TEXT PRIMARY KEY,
                    name             TEXT NOT NULL UNIQUE,
                    category         TEXT NOT NULL,
                    description      TEXT NOT NULL,
                    tool_type        TEXT NOT NULL CHECK (tool_type IN (
                        'builtin', 'mcp', 'script', 'workflow', 'proposed', 'provider'
                    )),
                    provider         TEXT,
                    cost_tier        TEXT CHECK (cost_tier IN ('free', 'cheap', 'moderate', 'expensive', NULL)),
                    success_rate     REAL,
                    avg_latency_ms   REAL,
                    last_used_at     TEXT,
                    usage_count      INTEGER NOT NULL DEFAULT 0,
                    created_at       TEXT NOT NULL,
                    metadata         TEXT,
                    updated_at       TEXT
                )
            """)
            await db.execute("""
                INSERT INTO tool_registry_new
                    (id, name, category, description, tool_type, provider,
                     cost_tier, success_rate, avg_latency_ms, last_used_at,
                     usage_count, created_at, metadata, updated_at)
                SELECT id, name, category, description, tool_type, provider,
                       cost_tier, success_rate, avg_latency_ms, last_used_at,
                       usage_count, created_at, metadata, updated_at
                FROM tool_registry
            """)
            await db.execute("DROP TABLE tool_registry")
            await db.execute("ALTER TABLE tool_registry_new RENAME TO tool_registry")
            await db.commit()
            logger.info("tool_registry table rebuilt with 'provider' tool_type")
    except Exception:
        logger.error("tool_registry CHECK constraint migration failed", exc_info=True)

    # Memory photographic: extraction watermark tracking on cc_sessions
    await _try_alter(db,
        "ALTER TABLE cc_sessions ADD COLUMN last_extracted_at TEXT",
        "cc_sessions.last_extracted_at")
    await _try_alter(db,
        "ALTER TABLE cc_sessions ADD COLUMN last_extracted_line INTEGER DEFAULT 0",
        "cc_sessions.last_extracted_line")

    # Memory photographic: expand memory_links CHECK constraint to support
    # typed relationships from conversation extraction (discussed_in,
    # evaluated_for, decided, etc.).  SQLite can't ALTER CHECK constraints,
    # so we rebuild the table following the established pattern.
    try:
        cursor = await db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_links'"
        )
        row = await cursor.fetchone()
        if row and "'discussed_in'" not in (row[0] or ""):
            await db.execute("""
                CREATE TABLE memory_links_new (
                    source_id   TEXT NOT NULL,
                    target_id   TEXT NOT NULL,
                    link_type   TEXT NOT NULL CHECK (
                        link_type IN (
                            'supports','contradicts','extends','elaborates',
                            'discussed_in','evaluated_for','decided',
                            'action_item_for','categorized_as','related_to',
                            'succeeded_by','preceded_by'
                        )
                    ),
                    strength    REAL NOT NULL DEFAULT 0.5,
                    created_at  TEXT NOT NULL,
                    PRIMARY KEY (source_id, target_id)
                )
            """)
            await db.execute("""
                INSERT INTO memory_links_new
                    (source_id, target_id, link_type, strength, created_at)
                SELECT source_id, target_id, link_type, strength, created_at
                FROM memory_links
            """)
            await db.execute("DROP TABLE memory_links")
            await db.execute(
                "ALTER TABLE memory_links_new RENAME TO memory_links"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_links_source "
                "ON memory_links(source_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_links_target "
                "ON memory_links(target_id)"
            )
            await db.commit()
            logger.info(
                "memory_links table rebuilt with expanded link types "
                "(discussed_in, evaluated_for, decided, etc.)"
            )
    except Exception:
        logger.error(
            "memory_links CHECK constraint migration failed", exc_info=True
        )

    # Bookmark fix: add source column to session_bookmarks
    await _try_alter(db,
        "ALTER TABLE session_bookmarks ADD COLUMN source TEXT NOT NULL DEFAULT 'auto'",
        "session_bookmarks.source")

    # Memory retrieval fix: add tags column to memory_fts (matches knowledge_fts).
    # FTS5 virtual tables can't be ALTERed — must rebuild via CREATE/COPY/DROP/RENAME.
    try:
        cursor = await db.execute("PRAGMA table_info(memory_fts)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "tags" not in cols:
            await db.execute("""
                CREATE VIRTUAL TABLE memory_fts_new USING fts5(
                    memory_id UNINDEXED,
                    content,
                    source_type,
                    tags,
                    collection UNINDEXED,
                    tokenize='porter ascii'
                )
            """)
            await db.execute("""
                INSERT INTO memory_fts_new(memory_id, content, source_type, tags, collection)
                SELECT memory_id, content, source_type, '', collection
                FROM memory_fts
            """)
            await db.execute("DROP TABLE memory_fts")
            await db.execute("ALTER TABLE memory_fts_new RENAME TO memory_fts")
            await db.commit()
            logger.info("memory_fts rebuilt with tags column")
    except Exception:
        logger.warning("memory_fts tags migration skipped", exc_info=True)

    # Mail monitor paralegal/judge redesign
    await _try_alter(db,
        "ALTER TABLE processed_emails ADD COLUMN layer1_brief TEXT",
        "processed_emails.layer1_brief")
    await _try_alter(db,
        "ALTER TABLE processed_emails ADD COLUMN layer2_decision TEXT",
        "processed_emails.layer2_decision")

    # Session indexing: topic + keywords for structured session search
    await _try_alter(db,
        "ALTER TABLE cc_sessions ADD COLUMN topic TEXT DEFAULT ''",
        "cc_sessions.topic")
    await _try_alter(db,
        "ALTER TABLE cc_sessions ADD COLUMN keywords TEXT DEFAULT ''",
        "cc_sessions.keywords")

    # Phase 1.5: backfill memory_metadata from Qdrant + pending_embeddings.
    # New memories write metadata at store time, but pre-existing memories
    # lack rows. Without backfill, the "recent" dashboard view is empty.
    await _migrate_backfill_memory_metadata(db)


async def _migrate_backfill_memory_metadata(db: aiosqlite.Connection) -> None:
    """Backfill memory_metadata for memories that predate the table.

    Data sources (in priority order):
    1. Qdrant point payload — has created_at, confidence, known collection
    2. pending_embeddings — has created_at for FTS5-only memories
    3. Epoch fallback — for memories with no Qdrant point or pending record

    Idempotent: uses INSERT OR IGNORE on memory_id PRIMARY KEY.
    Resilient: skips gracefully if Qdrant is unreachable.
    """
    # Check if backfill is needed
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM memory_metadata")
        meta_count = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM memory_fts")
        fts_count = (await cursor.fetchone())[0]
        if meta_count >= fts_count or fts_count == 0:
            return  # Already backfilled or nothing to backfill
    except Exception:
        logger.warning("memory_metadata backfill: count check failed, skipping", exc_info=True)
        return

    # Get all FTS5 memory_ids that lack metadata
    try:
        cursor = await db.execute("""
            SELECT f.memory_id, f.collection
            FROM memory_fts f
            LEFT JOIN memory_metadata m ON f.memory_id = m.memory_id
            WHERE m.memory_id IS NULL
        """)
        missing = await cursor.fetchall()
    except Exception:
        logger.warning("memory_metadata backfill: missing-row query failed, skipping", exc_info=True)
        return

    if not missing:
        return

    # Try Qdrant for timestamps + confidence (best source)
    qdrant_data: dict[str, dict] = {}
    try:
        from genesis.qdrant.collections import get_client, scroll_points

        client = get_client()
        for coll in ("episodic_memory", "knowledge_base"):
            offset = None
            while True:
                points, offset = scroll_points(
                    client, collection=coll, limit=500, offset=offset,
                )
                for p in points:
                    qdrant_data[p["id"]] = {
                        "created_at": p["payload"].get(
                            "created_at", "1970-01-01T00:00:00+00:00"
                        ),
                        "confidence": p["payload"].get("confidence"),
                        "collection": coll,
                    }
                if offset is None:
                    break
    except Exception:
        logger.warning(
            "memory_metadata backfill: Qdrant unavailable, using fallback timestamps",
            exc_info=True,
        )

    # Pending embeddings fallback timestamps
    pending_ts: dict[str, str] = {}
    try:
        cursor = await db.execute("SELECT memory_id, created_at FROM pending_embeddings")
        for row in await cursor.fetchall():
            pending_ts[row[0]] = row[1]
    except Exception:
        logger.debug("pending_embeddings lookup skipped (table may not exist yet)", exc_info=True)

    # Insert metadata rows
    inserted = 0
    for memory_id, fts_collection in missing:
        if memory_id in qdrant_data:
            d = qdrant_data[memory_id]
            created_at = d["created_at"]
            collection = d["collection"]
            confidence = d["confidence"]
            status = "embedded"
        elif memory_id in pending_ts:
            created_at = pending_ts[memory_id]
            collection = fts_collection or "episodic_memory"
            confidence = None
            status = "pending"
        else:
            created_at = "1970-01-01T00:00:00+00:00"
            collection = fts_collection or "episodic_memory"
            confidence = None
            status = "fts5_only"

        await db.execute(
            "INSERT OR IGNORE INTO memory_metadata "
            "(memory_id, created_at, collection, confidence, embedding_status) "
            "VALUES (?, ?, ?, ?, ?)",
            (memory_id, created_at, collection, confidence, status),
        )
        inserted += 1

    await db.commit()
    logger.info(
        "Backfilled %d memory_metadata rows (%d from Qdrant, %d from pending, %d epoch fallback)",
        inserted,
        sum(1 for mid, _ in missing if mid in qdrant_data),
        sum(1 for mid, _ in missing if mid not in qdrant_data and mid in pending_ts),
        sum(1 for mid, _ in missing if mid not in qdrant_data and mid not in pending_ts),
    )


async def seed_data(db: aiosqlite.Connection) -> None:
    """Insert initial seed data (signal weights, drive weights)."""
    await db.executemany(
        """INSERT OR IGNORE INTO signal_weights
           (signal_name, source_mcp, current_weight, initial_weight,
            min_weight, max_weight, feeds_depths)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        SIGNAL_WEIGHTS_SEED,
    )
    # Migrate existing rows from "agent_zero" → "genesis" source (AZ decoupling)
    await db.execute(
        """UPDATE signal_weights SET source_mcp = 'genesis'
           WHERE source_mcp = 'agent_zero'
           AND signal_name IN ('conversations_since_reflection', 'task_completion_quality')""",
    )
    await db.executemany(
        """INSERT OR IGNORE INTO drive_weights
           (drive_name, current_weight, initial_weight, min_weight, max_weight)
           VALUES (?, ?, ?, ?, ?)""",
        DRIVE_WEIGHTS_SEED,
    )
    await db.executemany(
        """INSERT OR IGNORE INTO depth_thresholds
           (depth_name, threshold, floor_seconds, ceiling_count, ceiling_window_seconds)
           VALUES (?, ?, ?, ?, ?)""",
        DEPTH_THRESHOLDS_SEED,
    )
    await db.executemany(
        """INSERT OR IGNORE INTO budgets
           (id, budget_type, limit_usd, warning_pct, active, created_at, updated_at)
           VALUES (?, ?, ?, ?, 1, ?, ?)""",
        BUDGET_SEED,
    )
