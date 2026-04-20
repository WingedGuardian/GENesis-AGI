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

    # 2026-04-11: remove unprocessed_memory_backlog signal weight.
    # The retrieval-coverage metric was being misinterpreted by the Deep
    # depth scorer as reflection urgency — a high value meant "many obs
    # never retrieved," which is a retrieval pipeline health issue, not a
    # cue to schedule Deep reflections. Signal collectors, cognitive-state
    # flag, and this weight row all removed in the same sweep.
    await db.execute(
        "DELETE FROM signal_weights WHERE signal_name = 'unprocessed_memory_backlog'"
    )

    # Cognitive state catch-22: stale_pending_items signal was collected
    # but had no weight row, contributing zero to Deep scorer.
    await db.execute(
        "INSERT OR IGNORE INTO signal_weights "
        "(signal_name, source_mcp, current_weight, initial_weight, "
        "min_weight, max_weight, feeds_depths) "
        "VALUES ('stale_pending_items', 'genesis', 0.45, 0.45, 0.0, 1.0, "
        "'[\"Deep\"]')"
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

    # Fix outreach_history CHECK constraint: add 'approval' category for
    # autonomous CLI approval prompts that route to the Approvals supergroup
    # topic.  SQLite cannot ALTER CHECK constraints, so rebuild the table
    # following the same pattern as tool_registry above.  Idempotent: the
    # rebuild is skipped if the stored DDL already contains the specific
    # trailing fragment 'digest', 'surplus', 'approval' — matching on the
    # exact fragment rather than a loose "approval" substring so future
    # unrelated columns named `approval_*` don't accidentally skip the
    # rebuild on upgrade paths.
    _APPROVAL_FRAGMENT = "'digest', 'surplus', 'approval'"
    try:
        cursor = await db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='outreach_history'"
        )
        row = await cursor.fetchone()
        if row and _APPROVAL_FRAGMENT not in (row[0] or ""):
            await db.execute("""
                CREATE TABLE outreach_history_new (
                    id                  TEXT PRIMARY KEY,
                    person_id           TEXT,
                    signal_type         TEXT NOT NULL,
                    topic               TEXT NOT NULL,
                    category            TEXT NOT NULL CHECK (category IN (
                        'blocker', 'alert', 'finding', 'insight', 'opportunity',
                        'digest', 'surplus', 'approval'
                    )),
                    salience_score      REAL NOT NULL,
                    channel             TEXT NOT NULL,
                    message_content     TEXT NOT NULL,
                    drive_alignment     TEXT,
                    labeled_surplus     INTEGER DEFAULT 0,
                    content_hash        TEXT,
                    delivery_id         TEXT,
                    delivered_at        TEXT,
                    opened_at           TEXT,
                    user_response       TEXT,
                    action_taken        TEXT,
                    engagement_outcome  TEXT CHECK (engagement_outcome IN (
                        'useful', 'not_useful', 'ambivalent', 'ignored', NULL
                    )),
                    engagement_signal   TEXT,
                    prediction_error    REAL,
                    created_at          TEXT NOT NULL
                )
            """)
            await db.execute("""
                INSERT INTO outreach_history_new
                    (id, person_id, signal_type, topic, category, salience_score,
                     channel, message_content, drive_alignment, labeled_surplus,
                     content_hash, delivery_id, delivered_at, opened_at,
                     user_response, action_taken, engagement_outcome,
                     engagement_signal, prediction_error, created_at)
                SELECT
                     id, person_id, signal_type, topic, category, salience_score,
                     channel, message_content, drive_alignment, labeled_surplus,
                     content_hash, delivery_id, delivered_at, opened_at,
                     user_response, action_taken, engagement_outcome,
                     engagement_signal, prediction_error, created_at
                FROM outreach_history
            """)
            await db.execute("DROP TABLE outreach_history")
            await db.execute(
                "ALTER TABLE outreach_history_new RENAME TO outreach_history"
            )
            # Recreate indexes that lived on outreach_history (DROP TABLE
            # removes them).  These must stay in sync with INDEXES in
            # _tables.py; if you add a new outreach_history index there,
            # add it here too.
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_outreach_channel "
                "ON outreach_history(channel)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_outreach_category "
                "ON outreach_history(category)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_outreach_delivered "
                "ON outreach_history(delivered_at)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_outreach_outcome "
                "ON outreach_history(engagement_outcome)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_outreach_dedup "
                "ON outreach_history(signal_type, topic, category, delivered_at)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_outreach_content_hash "
                "ON outreach_history(signal_type, category, content_hash, delivered_at)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_outreach_person "
                "ON outreach_history(person_id)"
            )
            await db.commit()
            logger.info("outreach_history table rebuilt with 'approval' category")
    except Exception:
        logger.error(
            "outreach_history CHECK constraint migration failed", exc_info=True,
        )

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

    # Reference store: add UNIQUE(project_type, domain, concept) to knowledge_units.
    # SQLite cannot ALTER constraints, so rebuild the table.  Idempotent via
    # sql-text check for the UNIQUE fragment.  Pre-existing rows with duplicate
    # (project_type, domain, concept) are deduplicated via INSERT OR IGNORE —
    # the first row wins.
    try:
        cursor = await db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='knowledge_units'"
        )
        row = await cursor.fetchone()
        if row and "UNIQUE(project_type, domain, concept)" not in (row[0] or ""):
            await db.execute("""
                CREATE TABLE knowledge_units_new (
                    id               TEXT PRIMARY KEY,
                    project_type     TEXT NOT NULL,
                    domain           TEXT NOT NULL,
                    source_doc       TEXT NOT NULL,
                    source_platform  TEXT,
                    section_title    TEXT,
                    concept          TEXT NOT NULL,
                    body             TEXT NOT NULL,
                    relationships    TEXT,
                    caveats          TEXT,
                    tags             TEXT,
                    confidence       REAL DEFAULT 0.85,
                    source_date      TEXT,
                    ingested_at      TEXT NOT NULL,
                    qdrant_id        TEXT,
                    embedding_model  TEXT,
                    retrieved_count  INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(project_type, domain, concept)
                )
            """)
            await db.execute("""
                INSERT OR IGNORE INTO knowledge_units_new
                    (id, project_type, domain, source_doc, source_platform,
                     section_title, concept, body, relationships, caveats, tags,
                     confidence, source_date, ingested_at, qdrant_id,
                     embedding_model, retrieved_count)
                SELECT id, project_type, domain, source_doc, source_platform,
                       section_title, concept, body, relationships, caveats, tags,
                       confidence, source_date, ingested_at, qdrant_id,
                       embedding_model, retrieved_count
                FROM knowledge_units
            """)
            await db.execute("DROP TABLE knowledge_units")
            await db.execute(
                "ALTER TABLE knowledge_units_new RENAME TO knowledge_units"
            )
            await db.commit()
            logger.info(
                "knowledge_units table rebuilt with UNIQUE(project_type, domain, concept)"
            )
    except Exception:
        logger.error(
            "knowledge_units UNIQUE constraint migration failed", exc_info=True
        )

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

    # Memory rebalance: add memory_class column to memory_metadata for
    # rule/fact/reference classification with activation weight boost.
    await _try_alter(db,
        "ALTER TABLE memory_metadata ADD COLUMN memory_class TEXT DEFAULT 'fact'",
        "memory_metadata.memory_class")

    # Memory rebalance: add provenance columns to pending_embeddings so the
    # recovery worker can reconstruct full Qdrant payloads (source, confidence,
    # session ID, transcript path, etc.) instead of losing this metadata.
    for col, col_type in [
        ("source", "TEXT"), ("confidence", "REAL"),
        ("source_session_id", "TEXT"), ("transcript_path", "TEXT"),
        ("source_line_range", "TEXT"), ("extraction_timestamp", "TEXT"),
        ("source_pipeline", "TEXT"),
    ]:
        await _try_alter(db,
            f"ALTER TABLE pending_embeddings ADD COLUMN {col} {col_type}",
            f"pending_embeddings.{col}")

    # Memory rebalance: resolve expired observations whose TTL has passed
    # but weren't caught by the 24h scheduler (e.g., runtime was down).
    # Idempotent — UPDATE WHERE is a no-op once resolved.
    try:
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        cursor = await db.execute(
            "UPDATE observations SET resolved = 1, resolved_at = ?, "
            "resolution_notes = 'auto-expired (TTL, migration sweep)' "
            "WHERE resolved = 0 AND expires_at IS NOT NULL AND expires_at < ?",
            (now, now),
        )
        expired_count = cursor.rowcount
        if expired_count:
            await db.commit()
            logger.info("Resolved %d expired observations (migration sweep)", expired_count)
    except Exception:
        logger.warning("Expired observation sweep skipped", exc_info=True)

    # 2026-04-18: Backfill expires_at on pre-TTL observations that have no
    # expiry despite belonging to a TTL-governed type, then resolve any whose
    # computed expiry is already past.  Also resolve stale persistent
    # observations (>60 days, low/medium priority).  Idempotent.
    try:
        from datetime import UTC, datetime, timedelta

        from genesis.db.crud.observations import _TTL_BY_TYPE, _TTL_PREFIX

        now = datetime.now(UTC)
        now_iso = now.isoformat()

        # Phase 1: backfill expires_at per type
        backfilled = 0
        for obs_type, ttl in _TTL_BY_TYPE.items():
            secs = int(ttl.total_seconds())
            cursor = await db.execute(
                "UPDATE observations SET expires_at = datetime(created_at, ? || ' seconds') "
                "WHERE resolved = 0 AND expires_at IS NULL AND type = ?",
                (str(secs), obs_type),
            )
            backfilled += cursor.rowcount
        for prefix, ttl in _TTL_PREFIX:
            secs = int(ttl.total_seconds())
            cursor = await db.execute(
                "UPDATE observations SET expires_at = datetime(created_at, ? || ' seconds') "
                "WHERE resolved = 0 AND expires_at IS NULL AND type LIKE ?",
                (str(secs), f"{prefix}%"),
            )
            backfilled += cursor.rowcount
        if backfilled:
            await db.commit()
            logger.info("Backfilled expires_at on %d pre-TTL observations", backfilled)

        # Phase 2: resolve any that are now past their backfilled expiry
        cursor = await db.execute(
            "UPDATE observations SET resolved = 1, resolved_at = ?, "
            "resolution_notes = 'auto-expired (TTL backfill)' "
            "WHERE resolved = 0 AND expires_at IS NOT NULL AND expires_at < ?",
            (now_iso, now_iso),
        )
        newly_expired = cursor.rowcount
        if newly_expired:
            await db.commit()
            logger.info("Resolved %d observations past backfilled TTL", newly_expired)

        # Phase 3: resolve stale persistent-type observations (>60 days, low/medium)
        stale_cutoff = (now - timedelta(days=60)).isoformat()
        cursor = await db.execute(
            "UPDATE observations SET resolved = 1, resolved_at = ?, "
            "resolution_notes = 'auto-resolved (stale persistent, >60 days)' "
            "WHERE resolved = 0 AND expires_at IS NULL "
            "AND created_at < ? AND priority IN ('low', 'medium')",
            (now_iso, stale_cutoff),
        )
        stale_resolved = cursor.rowcount
        if stale_resolved:
            await db.commit()
            logger.info("Resolved %d stale persistent observations (>60d)", stale_resolved)
    except Exception:
        logger.warning("Observation TTL backfill migration skipped", exc_info=True)

    # Memory rebalance: purge orphaned memory_links whose source/target
    # memories were deleted but links were never cascade-cleaned.  MemoryStore
    # .delete() now cascades, but ~1,600 stale links accumulated before that.
    # Idempotent — DELETE WHERE NOT IN is a no-op once clean.
    try:
        cursor = await db.execute(
            "DELETE FROM memory_links "
            "WHERE source_id NOT IN (SELECT memory_id FROM memory_metadata) "
            "   OR target_id NOT IN (SELECT memory_id FROM memory_metadata)"
        )
        orphan_count = cursor.rowcount
        if orphan_count:
            await db.commit()
            logger.info("Purged %d orphaned memory_links", orphan_count)
    except Exception:
        logger.warning("Orphaned memory_links cleanup skipped", exc_info=True)

    # Cost tracking: cost_known flag on cost_events
    await _try_alter(db,
        "ALTER TABLE cost_events ADD COLUMN cost_known INTEGER NOT NULL DEFAULT 1",
        "cost_events.cost_known")

    # Memory taxonomy: add wing/room columns to memory_metadata for
    # structural domain classification (MemPalace-inspired navigational retrieval).
    await _try_alter(db,
        "ALTER TABLE memory_metadata ADD COLUMN wing TEXT",
        "memory_metadata.wing")
    await _try_alter(db,
        "ALTER TABLE memory_metadata ADD COLUMN room TEXT",
        "memory_metadata.room")

    # 2026-04-14: Move critical_failure and software_error_spike to Micro only.
    # These are delta signals — they matter when they flip state, not as
    # persistent conditions driving hourly Light reflections.
    await db.execute(
        "UPDATE signal_weights SET feeds_depths = '[\"Micro\"]', "
        "current_weight = 0.70, initial_weight = 0.70 "
        "WHERE signal_name = 'critical_failure'"
    )
    await db.execute(
        "UPDATE signal_weights SET feeds_depths = '[\"Micro\"]' "
        "WHERE signal_name = 'software_error_spike'"
    )

    # 2026-04-14: Reduce Light floor from 6h to 3h.
    # 6h was never enforced (floor_seconds was unused in classifier).
    # Now that floor enforcement is active, 3h is appropriate for Light.
    await db.execute(
        "UPDATE depth_thresholds SET floor_seconds = 10800 "
        "WHERE depth_name = 'Light' AND floor_seconds = 21600"
    )

    # 2026-04-17: Signal redistribution — cc_version_changed to Micro-only.
    # (critical_failure and software_error_spike already migrated above.)
    await db.execute(
        "UPDATE signal_weights SET feeds_depths = '[\"Micro\"]', "
        "current_weight = 0.50, initial_weight = 0.50 "
        "WHERE signal_name = 'cc_version_changed'"
    )

    # 2026-04-17: New signals — cascade bridge + subsystem activity + ghost activation.
    # INSERT OR IGNORE so re-running is idempotent.
    _new_signals = [
        ("light_count_since_deep", "awareness_loop", 0.50, 0.50, 0.0, 1.0, '["Deep"]'),
        ("sentinel_activity", "sentinel", 0.60, 0.60, 0.0, 1.0, '["Micro"]'),
        ("guardian_activity", "guardian", 0.50, 0.50, 0.0, 1.0, '["Micro"]'),
        ("surplus_activity", "surplus", 0.45, 0.45, 0.0, 1.0, '["Micro"]'),
        ("autonomy_activity", "autonomy", 0.60, 0.60, 0.0, 1.0, '["Micro"]'),
        ("stale_pending_items", "cognitive_state", 0.35, 0.35, 0.0, 1.0, '["Micro"]'),
    ]
    for row in _new_signals:
        await db.execute(
            "INSERT OR IGNORE INTO signal_weights "
            "(signal_name, source_mcp, current_weight, initial_weight, min_weight, max_weight, feeds_depths) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            row,
        )

    # Cross-session awareness: heartbeat table for real-time session tracking.
    # Separate from cc_sessions because hooks need simple fast UPSERT and
    # cc_sessions rows may not exist for direct user CC sessions.
    await db.execute("""
        CREATE TABLE IF NOT EXISTS session_heartbeats (
            cc_session_id   TEXT PRIMARY KEY,
            source_tag      TEXT NOT NULL DEFAULT 'foreground',
            model           TEXT,
            topic           TEXT,
            user_summary    TEXT,
            genesis_summary TEXT,
            updated_at      TEXT NOT NULL
        )
    """)
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_heartbeat_updated "
        "ON session_heartbeats(updated_at)"
    )

    # Knowledge pipeline: source_pipeline, purpose, ingestion_source
    await _try_alter(db,
        "ALTER TABLE knowledge_units ADD COLUMN source_pipeline TEXT",
        "knowledge_units.source_pipeline")
    await _try_alter(db,
        "ALTER TABLE knowledge_units ADD COLUMN purpose TEXT",
        "knowledge_units.purpose")
    await _try_alter(db,
        "ALTER TABLE knowledge_units ADD COLUMN ingestion_source TEXT",
        "knowledge_units.ingestion_source")

    # Knowledge upload tracking table (dashboard file uploads).
    await db.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_uploads (
            id            TEXT PRIMARY KEY,
            filename      TEXT NOT NULL,
            file_path     TEXT NOT NULL,
            file_size     INTEGER NOT NULL,
            mime_type     TEXT,
            project_type  TEXT,
            domain        TEXT,
            purpose       TEXT,
            status        TEXT NOT NULL DEFAULT 'uploaded'
                          CHECK (status IN ('uploaded', 'processing', 'completed', 'failed')),
            error_message TEXT,
            unit_ids      TEXT,
            created_at    TEXT NOT NULL,
            completed_at  TEXT
        )
    """)

    # Knowledge upload chunk progress tracking
    await _try_alter(db,
        "ALTER TABLE knowledge_uploads ADD COLUMN chunks_total INTEGER",
        "knowledge_uploads.chunks_total")
    await _try_alter(db,
        "ALTER TABLE knowledge_uploads ADD COLUMN chunks_done INTEGER DEFAULT 0",
        "knowledge_uploads.chunks_done")

    # Approval resume tracking — atomic consumed_at column
    await _try_alter(db,
        "ALTER TABLE approval_requests ADD COLUMN consumed_at TEXT",
        "approval_requests.consumed_at")

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
