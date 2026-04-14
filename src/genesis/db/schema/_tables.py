"""Genesis v3 database schema — table DDL, indexes, FTS5, and seed data constants.

All tables live in a single genesis.db with WAL mode.
Schema is derived from docs/architecture/genesis-v3-autonomous-behavior-design.md.
"""

# ─── Table DDL ────────────────────────────────────────────────────────────────

TABLES = {
    "procedural_memory": """
        CREATE TABLE IF NOT EXISTS procedural_memory (
            id               TEXT PRIMARY KEY,
            person_id        TEXT,               -- GROUNDWORK(multi-person)
            task_type         TEXT NOT NULL,
            principle         TEXT NOT NULL,
            steps             TEXT NOT NULL,       -- JSON array of step strings
            tools_used        TEXT NOT NULL,       -- JSON array of tool names
            context_tags      TEXT NOT NULL,       -- JSON array of tags
            success_count     INTEGER NOT NULL DEFAULT 0,
            failure_count     INTEGER NOT NULL DEFAULT 0,
            failure_modes     TEXT,                -- JSON: array of {description, conditions, times_hit, transient}
            confidence        REAL NOT NULL DEFAULT 0.0,
            last_used         TEXT,                -- ISO datetime
            last_validated    TEXT,                -- ISO datetime
            deprecated        INTEGER NOT NULL DEFAULT 0,
            deprecated_reason TEXT,
            superseded_by     TEXT,                -- FK to procedural_memory.id
            speculative       INTEGER NOT NULL DEFAULT 1,
            invocation_count  INTEGER NOT NULL DEFAULT 0,
            attempted_workarounds TEXT,            -- JSON: array of {description, outcome, conditions}
            version           INTEGER NOT NULL DEFAULT 1,
            created_at        TEXT NOT NULL,
            activation_tier   TEXT NOT NULL DEFAULT 'L4',  -- L1/L2/L3/L4 promotion tier
            tool_trigger      TEXT                         -- JSON array of tool names for L1 matching
        )
    """,
    "observations": """
        CREATE TABLE IF NOT EXISTS observations (
            id               TEXT PRIMARY KEY,
            person_id        TEXT,               -- GROUNDWORK(multi-person)
            source           TEXT NOT NULL,
            type             TEXT NOT NULL,
            category         TEXT,
            content          TEXT NOT NULL,
            priority         TEXT NOT NULL CHECK (priority IN ('low', 'medium', 'high', 'critical')),
            speculative      INTEGER NOT NULL DEFAULT 0,
            retrieved_count  INTEGER NOT NULL DEFAULT 0,
            influenced_action INTEGER NOT NULL DEFAULT 0,
            resolved         INTEGER NOT NULL DEFAULT 0,
            resolved_at      TEXT,
            resolution_notes TEXT,
            created_at       TEXT NOT NULL,
            expires_at       TEXT,
            content_hash     TEXT
        )
    """,
    "execution_traces": """
        CREATE TABLE IF NOT EXISTS execution_traces (
            id                    TEXT PRIMARY KEY,
            person_id             TEXT,            -- GROUNDWORK(multi-person)
            initiated_by          TEXT NOT NULL DEFAULT 'user',  -- 'user', 'awareness_loop', 'surplus', 'reflection'
            user_request          TEXT NOT NULL,
            plan                  TEXT NOT NULL,       -- JSON array of planned steps
            sub_agents            TEXT NOT NULL,       -- JSON array of sub-agent records
            quality_gate          TEXT,                -- JSON: {passed, reason, action}
            total_cost_usd        REAL NOT NULL DEFAULT 0.0,
            procedural_extractions TEXT,               -- JSON array of proc IDs
            retrospective_id      TEXT,
            outcome_class         TEXT CHECK (outcome_class IN (
                'success', 'approach_failure', 'capability_gap',
                'external_blocker', 'workaround_success'
            )),
            request_delivery_delta TEXT,               -- JSON: scope evolution + delta + attribution
            created_at            TEXT NOT NULL,
            completed_at          TEXT
        )
    """,
    "surplus_insights": """
        CREATE TABLE IF NOT EXISTS surplus_insights (
            id                   TEXT PRIMARY KEY,
            content              TEXT NOT NULL,
            source_task_type     TEXT NOT NULL,
            generating_model     TEXT NOT NULL,
            drive_alignment      TEXT NOT NULL CHECK (drive_alignment IN (
                'curiosity', 'competence', 'cooperation', 'preservation'
            )),
            confidence           REAL NOT NULL DEFAULT 0.0,
            engagement_prediction REAL,
            created_at           TEXT NOT NULL,
            ttl                  TEXT NOT NULL,        -- ISO datetime expiry
            promoted_to          TEXT,
            promotion_status     TEXT NOT NULL DEFAULT 'pending' CHECK (
                promotion_status IN ('pending', 'promoted', 'discarded')
            )
        )
    """,
    "signal_weights": """
        CREATE TABLE IF NOT EXISTS signal_weights (
            signal_name    TEXT PRIMARY KEY,
            source_mcp     TEXT NOT NULL,
            current_weight REAL NOT NULL,
            initial_weight REAL NOT NULL,
            min_weight     REAL NOT NULL DEFAULT 0.0,
            max_weight     REAL NOT NULL DEFAULT 1.0,
            feeds_depths   TEXT NOT NULL,             -- JSON array: ["Micro", "Light"] etc.
            last_adapted_at TEXT,
            adaptation_notes TEXT
        )
    """,
    "capability_gaps": """
        CREATE TABLE IF NOT EXISTS capability_gaps (
            id              TEXT PRIMARY KEY,
            description     TEXT NOT NULL,
            task_context    TEXT,
            frequency       INTEGER NOT NULL DEFAULT 1,
            first_seen      TEXT NOT NULL,
            last_seen       TEXT NOT NULL,
            gap_type        TEXT NOT NULL CHECK (gap_type IN ('capability_gap', 'external_blocker')),
            blocker_class   TEXT CHECK (blocker_class IN (
                'user_rectifiable', 'future_capability', 'permanent', NULL
            )),
            feasibility     TEXT CHECK (feasibility IN ('low', 'medium', 'high', NULL)),
            revisit_after   TEXT,
            proposed_tool_id TEXT,
            status          TEXT NOT NULL DEFAULT 'open' CHECK (
                status IN ('open', 'proposed', 'resolved', 'archived')
            ),
            resolved_at     TEXT,
            resolution_notes TEXT
        )
    """,
    "speculative_claims": """
        CREATE TABLE IF NOT EXISTS speculative_claims (
            id                   TEXT PRIMARY KEY,
            claim                TEXT NOT NULL,
            speculative          INTEGER NOT NULL DEFAULT 1,
            evidence_count       INTEGER NOT NULL DEFAULT 0,
            hypothesis_expiry    TEXT NOT NULL,
            confirmed_by         TEXT,               -- JSON array of memory IDs
            source_reflection_id TEXT,
            created_at           TEXT NOT NULL,
            archived_at          TEXT
        )
    """,
    "autonomy_state": """
        CREATE TABLE IF NOT EXISTS autonomy_state (
            id                     TEXT PRIMARY KEY,
            person_id              TEXT,            -- GROUNDWORK(multi-person)
            category               TEXT NOT NULL,
            current_level          INTEGER NOT NULL DEFAULT 1 CHECK (current_level BETWEEN 1 AND 7),
            earned_level           INTEGER NOT NULL DEFAULT 1 CHECK (earned_level BETWEEN 1 AND 7),
            context_ceiling        TEXT CHECK (context_ceiling IN (
                'direct_session', 'background_cognitive', 'sub_agent', 'outreach', NULL
            )),
            consecutive_corrections INTEGER NOT NULL DEFAULT 0,
            total_successes        INTEGER NOT NULL DEFAULT 0,
            total_corrections      INTEGER NOT NULL DEFAULT 0,
            last_correction_at     TEXT,
            last_regression_at     TEXT,
            regression_reason      TEXT,
            updated_at             TEXT NOT NULL
        )
    """,
    "outreach_history": """
        CREATE TABLE IF NOT EXISTS outreach_history (
            id                  TEXT PRIMARY KEY,
            person_id           TEXT,               -- GROUNDWORK(multi-person)
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
    """,
    "pending_outreach": """
        CREATE TABLE IF NOT EXISTS pending_outreach (
            id              TEXT PRIMARY KEY,
            message         TEXT NOT NULL,
            category        TEXT NOT NULL,
            channel         TEXT NOT NULL DEFAULT 'telegram',
            urgency         TEXT NOT NULL DEFAULT 'low',
            deliver_after   TEXT,
            created_at      TEXT NOT NULL,
            delivered       INTEGER NOT NULL DEFAULT 0,
            delivered_at    TEXT
        )
    """,
    "brainstorm_log": """
        CREATE TABLE IF NOT EXISTS brainstorm_log (
            id                TEXT PRIMARY KEY,
            session_type      TEXT NOT NULL CHECK (session_type IN ('upgrade_user', 'upgrade_self')),
            model_used        TEXT NOT NULL,
            outputs           TEXT NOT NULL,         -- JSON array of staging items
            staging_ids       TEXT,                  -- JSON array of surplus_insights IDs
            promoted_count    INTEGER DEFAULT 0,
            discarded_count   INTEGER DEFAULT 0,
            journal_entry_ref TEXT,
            created_at        TEXT NOT NULL
        )
    """,
    "user_model_cache": """
        CREATE TABLE IF NOT EXISTS user_model_cache (
            id              TEXT PRIMARY KEY DEFAULT 'current',
            person_id       TEXT,                -- GROUNDWORK(multi-person)
            model_json      TEXT NOT NULL,
            version         INTEGER NOT NULL DEFAULT 1,
            synthesized_at  TEXT NOT NULL,
            synthesized_by  TEXT NOT NULL,
            evidence_count  INTEGER NOT NULL DEFAULT 0,
            last_change_type TEXT,
            last_changed_at TEXT
        )
    """,
    "tool_registry": """
        CREATE TABLE IF NOT EXISTS tool_registry (
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
    """,
    "cost_events": """
        CREATE TABLE IF NOT EXISTS cost_events (
            id               TEXT PRIMARY KEY,
            event_type       TEXT NOT NULL CHECK (event_type IN (
                'llm_call', 'api_call', 'tool_use', 'sub_agent'
            )),
            model            TEXT,
            provider         TEXT,
            engine           TEXT,
            task_id          TEXT,
            person_id        TEXT,               -- GROUNDWORK(multi-person)
            input_tokens     INTEGER,
            output_tokens    INTEGER,
            cost_usd         REAL NOT NULL DEFAULT 0.0,
            cost_known       INTEGER NOT NULL DEFAULT 1,  -- 0 if litellm couldn't price
            metadata         TEXT,               -- JSON
            created_at       TEXT NOT NULL
        )
    """,
    "budgets": """
        CREATE TABLE IF NOT EXISTS budgets (
            id               TEXT PRIMARY KEY,
            budget_type      TEXT NOT NULL CHECK (budget_type IN (
                'daily', 'weekly', 'monthly', 'task', 'workaround'
            )),
            person_id        TEXT,               -- GROUNDWORK(multi-person)
            scope            TEXT,
            limit_usd        REAL NOT NULL,
            warning_pct      REAL NOT NULL DEFAULT 0.80,
            active           INTEGER NOT NULL DEFAULT 1,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        )
    """,
    "dead_letter": """
        CREATE TABLE IF NOT EXISTS dead_letter (
            id              TEXT PRIMARY KEY,
            operation_type  TEXT NOT NULL,
            payload         TEXT NOT NULL,
            target_provider TEXT NOT NULL,
            failure_reason  TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            retry_count     INTEGER DEFAULT 0,
            last_retry_at   TEXT,
            status          TEXT DEFAULT 'pending'
        )
    """,
    "awareness_ticks": """
        CREATE TABLE IF NOT EXISTS awareness_ticks (
            id               TEXT PRIMARY KEY,
            source           TEXT NOT NULL CHECK (source IN ('scheduled', 'critical_bypass', 'recovery_catchup')),
            signals_json     TEXT NOT NULL,
            scores_json      TEXT NOT NULL,
            classified_depth TEXT,
            trigger_reason   TEXT,
            created_at       TEXT NOT NULL
        )
    """,
    "depth_thresholds": """
        CREATE TABLE IF NOT EXISTS depth_thresholds (
            depth_name              TEXT PRIMARY KEY,
            threshold               REAL NOT NULL,
            floor_seconds           INTEGER NOT NULL,
            ceiling_count           INTEGER NOT NULL,
            ceiling_window_seconds  INTEGER NOT NULL
        )
    """,
    "surplus_tasks": """
        CREATE TABLE IF NOT EXISTS surplus_tasks (
            id                TEXT PRIMARY KEY,
            task_type         TEXT NOT NULL,
            compute_tier      TEXT NOT NULL,
            priority          REAL NOT NULL DEFAULT 0.5,
            drive_alignment   TEXT NOT NULL,
            status            TEXT NOT NULL DEFAULT 'pending' CHECK (
                status IN ('pending', 'running', 'completed', 'failed', 'cancelled')
            ),
            payload           TEXT,
            created_at        TEXT NOT NULL,
            started_at        TEXT,
            completed_at      TEXT,
            result_staging_id TEXT,
            failure_reason    TEXT,
            attempt_count     INTEGER NOT NULL DEFAULT 0
        )
    """,
    "drive_weights": """
        CREATE TABLE IF NOT EXISTS drive_weights (
            drive_name     TEXT PRIMARY KEY,
            current_weight REAL NOT NULL,
            initial_weight REAL NOT NULL,
            min_weight     REAL NOT NULL DEFAULT 0.10,
            max_weight     REAL NOT NULL DEFAULT 0.50
        )
    """,
    "cognitive_state": """
        CREATE TABLE IF NOT EXISTS cognitive_state (
            id           TEXT PRIMARY KEY,
            content      TEXT NOT NULL,
            section      TEXT NOT NULL CHECK (section IN (
                'active_context', 'pending_actions', 'state_flags'
            )),
            generated_by TEXT,
            created_at   TEXT NOT NULL,
            expires_at   TEXT
        )
    """,
    "message_queue": """
        CREATE TABLE IF NOT EXISTS message_queue (
            id             TEXT PRIMARY KEY,
            task_id        TEXT,
            source         TEXT NOT NULL,
            target         TEXT NOT NULL,
            message_type   TEXT NOT NULL CHECK (message_type IN (
                'question', 'decision', 'error', 'finding', 'completion', 'progress'
            )),
            priority       TEXT NOT NULL DEFAULT 'medium' CHECK (priority IN (
                'high', 'medium', 'low'
            )),
            content        TEXT NOT NULL,
            response       TEXT,
            session_id     TEXT,
            created_at     TEXT NOT NULL,
            responded_at   TEXT,
            expired_at     TEXT
        )
    """,
    "cc_sessions": """
        CREATE TABLE IF NOT EXISTS cc_sessions (
            id               TEXT PRIMARY KEY,
            session_type     TEXT NOT NULL CHECK (session_type IN (
                'foreground', 'background_reflection', 'background_task'
            )),
            user_id          TEXT,
            channel          TEXT,
            model            TEXT NOT NULL,
            effort           TEXT NOT NULL DEFAULT 'medium',
            status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN (
                'active', 'checkpointed', 'completed', 'failed', 'expired'
            )),
            pid              INTEGER,
            started_at       TEXT NOT NULL,
            last_activity_at TEXT NOT NULL,
            checkpointed_at  TEXT,
            completed_at     TEXT,
            source_tag       TEXT NOT NULL DEFAULT 'foreground',
            metadata         TEXT,
            cc_session_id    TEXT,
            thread_id        TEXT,
            rate_limited_at  TEXT,
            rate_limit_resumes_at TEXT
        )
    """,
    "inbox_items": """
        CREATE TABLE IF NOT EXISTS inbox_items (
            id             TEXT PRIMARY KEY,
            file_path      TEXT NOT NULL,
            content_hash   TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'pending' CHECK (
                status IN ('pending', 'processing', 'completed', 'failed')
            ),
            batch_id       TEXT,
            response_path  TEXT,
            created_at     TEXT NOT NULL,
            processed_at   TEXT,
            error_message  TEXT,
            retry_count    INTEGER NOT NULL DEFAULT 0,
            evaluated_content TEXT
        )
    """,
    "processed_emails": """
        CREATE TABLE IF NOT EXISTS processed_emails (
            id              TEXT PRIMARY KEY,
            message_id      TEXT NOT NULL,
            imap_uid        INTEGER,
            sender          TEXT NOT NULL,
            subject         TEXT NOT NULL,
            received_at     TEXT,
            body_preview    TEXT,
            layer1_verdict  TEXT,
            status          TEXT NOT NULL DEFAULT 'pending' CHECK (
                status IN ('pending', 'processing', 'completed', 'skipped', 'failed')
            ),
            batch_id        TEXT,
            created_at      TEXT NOT NULL,
            processed_at    TEXT,
            error_message   TEXT,
            retry_count     INTEGER NOT NULL DEFAULT 0,
            content_hash    TEXT
        )
    """,
    "memory_links": """
        CREATE TABLE IF NOT EXISTS memory_links (
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
    """,
    "deferred_work_queue": """
        CREATE TABLE IF NOT EXISTS deferred_work_queue (
            id              TEXT PRIMARY KEY,
            work_type       TEXT NOT NULL,
            call_site_id    TEXT,
            priority        INTEGER NOT NULL DEFAULT 50,
            payload_json    TEXT NOT NULL,
            deferred_at     TEXT NOT NULL,
            deferred_reason TEXT NOT NULL,
            staleness_policy TEXT NOT NULL DEFAULT 'drain',
            staleness_ttl_s INTEGER,
            status          TEXT NOT NULL DEFAULT 'pending',
            attempts        INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT,
            completed_at    TEXT,
            error_message   TEXT,
            created_at      TEXT NOT NULL
        )
    """,
    "pending_embeddings": """
        CREATE TABLE IF NOT EXISTS pending_embeddings (
            id              TEXT PRIMARY KEY,
            memory_id       TEXT NOT NULL,
            content         TEXT NOT NULL,
            memory_type     TEXT NOT NULL,
            tags            TEXT,
            collection      TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            embedded_at     TEXT,
            error_message   TEXT,
            source          TEXT,
            confidence          REAL,
            source_session_id   TEXT,
            transcript_path     TEXT,
            source_line_range   TEXT,
            extraction_timestamp TEXT,
            source_pipeline     TEXT
        )
    """,
    "predictions": """
        CREATE TABLE IF NOT EXISTS predictions (
            id                TEXT PRIMARY KEY,
            action_id         TEXT NOT NULL,
            timestamp         TEXT NOT NULL DEFAULT (datetime('now')),
            prediction        TEXT NOT NULL,
            confidence        REAL NOT NULL,
            confidence_bucket TEXT NOT NULL,
            domain            TEXT NOT NULL CHECK (domain IN ('outreach', 'triage', 'procedure', 'routing')),
            reasoning         TEXT NOT NULL,
            outcome           TEXT,
            correct           INTEGER,
            matched_at        TEXT
        )
    """,
    "events": """
        CREATE TABLE IF NOT EXISTS events (
            id               TEXT PRIMARY KEY,
            timestamp        TEXT NOT NULL,
            subsystem        TEXT NOT NULL,
            severity         TEXT NOT NULL,
            event_type       TEXT NOT NULL,
            message          TEXT NOT NULL,
            details          TEXT,
            session_id       TEXT,
            created_at       TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """,
    "calibration_curves": """
        CREATE TABLE IF NOT EXISTS calibration_curves (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            domain               TEXT NOT NULL,
            confidence_bucket    TEXT NOT NULL,
            predicted_confidence REAL NOT NULL,
            actual_success_rate  REAL NOT NULL,
            sample_count         INTEGER NOT NULL,
            correction_factor    REAL NOT NULL,
            computed_at          TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(domain, confidence_bucket)
        )
    """,
    "approval_requests": """
        CREATE TABLE IF NOT EXISTS approval_requests (
            id               TEXT PRIMARY KEY,
            action_type      TEXT NOT NULL,
            action_class     TEXT NOT NULL CHECK (action_class IN (
                'reversible', 'costly_reversible', 'irreversible'
            )),
            description      TEXT NOT NULL,
            context          TEXT,
            status           TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
                'pending', 'approved', 'rejected', 'expired', 'cancelled'
            )),
            timeout_at       TEXT,
            created_at       TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at      TEXT,
            resolved_by      TEXT
        )
    """,
    "task_states": """
        CREATE TABLE IF NOT EXISTS task_states (
            task_id          TEXT PRIMARY KEY,
            description      TEXT NOT NULL,
            current_phase    TEXT NOT NULL DEFAULT 'planning',
            decisions        TEXT,
            blockers         TEXT,
            outputs          TEXT,
            session_id       TEXT,
            created_at       TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """,
    "task_steps": """
        CREATE TABLE IF NOT EXISTS task_steps (
            task_id          TEXT NOT NULL,
            step_idx         INTEGER NOT NULL,
            step_type        TEXT NOT NULL DEFAULT 'code',
            description      TEXT NOT NULL DEFAULT '',
            status           TEXT NOT NULL DEFAULT 'pending',
            result_json      TEXT,
            cost_usd         REAL NOT NULL DEFAULT 0.0,
            model_used       TEXT,
            session_id       TEXT,
            started_at       TEXT,
            completed_at     TEXT,
            PRIMARY KEY (task_id, step_idx)
        )
    """,
    "knowledge_units": """
        CREATE TABLE IF NOT EXISTS knowledge_units (
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
            retrieved_count  INTEGER NOT NULL DEFAULT 0
        )
    """,
    "evolution_proposals": """
        CREATE TABLE IF NOT EXISTS evolution_proposals (
            id                    TEXT PRIMARY KEY,
            proposal_type         TEXT NOT NULL,
            current_content       TEXT NOT NULL,
            proposed_change       TEXT NOT NULL,
            rationale             TEXT NOT NULL,
            source_reflection_id  TEXT,
            status                TEXT NOT NULL DEFAULT 'pending'
                                  CHECK (status IN ('pending', 'approved', 'rejected', 'withdrawn')),
            created_at            TEXT NOT NULL,
            reviewed_at           TEXT
        )
    """,
    "session_bookmarks": """
        CREATE TABLE IF NOT EXISTS session_bookmarks (
            id                TEXT PRIMARY KEY,
            cc_session_id     TEXT,
            genesis_session_id TEXT,
            bookmark_type     TEXT NOT NULL
                              CHECK (bookmark_type IN ('micro', 'rich', 'topic')),
            topic             TEXT,
            tags              TEXT,
            transcript_path   TEXT,
            has_rich_summary  INTEGER NOT NULL DEFAULT 0,
            created_at        TEXT NOT NULL,
            enriched_at       TEXT,
            resumed_count     INTEGER NOT NULL DEFAULT 0,
            last_resumed_at   TEXT,
            source            TEXT NOT NULL DEFAULT 'auto'
        )
    """,
    "telegram_messages": """
        CREATE TABLE IF NOT EXISTS telegram_messages (
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
    """,
    "activity_log": """
        CREATE TABLE IF NOT EXISTS activity_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            provider    TEXT NOT NULL,
            latency_ms  REAL NOT NULL,
            success     INTEGER NOT NULL DEFAULT 1,
            cache_hit   INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """,
    "module_config": """
        CREATE TABLE IF NOT EXISTS module_config (
            module_name TEXT PRIMARY KEY,
            enabled     INTEGER NOT NULL DEFAULT 1,
            config_json TEXT NOT NULL DEFAULT '{}',
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """,
    "telegram_topics": """
        CREATE TABLE IF NOT EXISTS telegram_topics (
            category    TEXT NOT NULL,
            thread_id   INTEGER NOT NULL,
            chat_id     INTEGER NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (category, chat_id)
        )
    """,
    # ─── Ego subsystem ───────────────────────────────────────────────────────
    "ego_cycles": """
        CREATE TABLE IF NOT EXISTS ego_cycles (
            id              TEXT PRIMARY KEY,
            output_text     TEXT NOT NULL,           -- full ego output (reasoning + decisions)
            proposals_json  TEXT NOT NULL DEFAULT '[]', -- JSON array of proposals from this cycle
            focus_summary   TEXT NOT NULL DEFAULT '', -- one-line summary for reflection injection
            model_used      TEXT NOT NULL DEFAULT '',
            cost_usd        REAL NOT NULL DEFAULT 0.0,
            input_tokens    INTEGER NOT NULL DEFAULT 0,
            output_tokens   INTEGER NOT NULL DEFAULT 0,
            duration_ms     INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL,
            compacted_into  TEXT                     -- set when folded into compacted summary
        )
    """,
    "ego_proposals": """
        CREATE TABLE IF NOT EXISTS ego_proposals (
            id              TEXT PRIMARY KEY,
            action_type     TEXT NOT NULL,            -- e.g., investigate, outreach, maintenance
            action_category TEXT NOT NULL DEFAULT '',  -- for per-category graduation tracking
            content         TEXT NOT NULL,             -- what the ego wants to do
            rationale       TEXT NOT NULL DEFAULT '',  -- why
            confidence      REAL NOT NULL DEFAULT 0.0, -- 0.0-1.0
            urgency         TEXT NOT NULL DEFAULT 'normal' CHECK (urgency IN ('low', 'normal', 'high', 'critical')),
            alternatives    TEXT NOT NULL DEFAULT '',  -- what else was considered
            status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'executed', 'failed')),
            user_response   TEXT,                     -- rejection reason, approval notes
            cycle_id        TEXT,                     -- FK to ego_cycles.id
            batch_id        TEXT,                     -- groups proposals into digest batches
            created_at      TEXT NOT NULL,
            resolved_at     TEXT,
            expires_at      TEXT                      -- auto-expiry timestamp
        )
    """,
    "ego_state": """
        CREATE TABLE IF NOT EXISTS ego_state (
            key             TEXT PRIMARY KEY,
            value           TEXT NOT NULL,
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """,
    # ── Behavioral Immune System (BIS) ───────────────────────────────────────
    # GROUNDWORK(bis): Tables for graduated behavioral correction.
    # See docs/plans/2026-03-27-behavioral-immune-system-design.md
    "behavioral_corrections": """
        CREATE TABLE IF NOT EXISTS behavioral_corrections (
            id              TEXT PRIMARY KEY,
            raw_user_text   TEXT NOT NULL,
            context         TEXT NOT NULL,
            severity        REAL NOT NULL,
            theme_id        TEXT,
            embedding_id    TEXT,
            created_at      TEXT NOT NULL
        )
    """,
    "behavioral_themes": """
        CREATE TABLE IF NOT EXISTS behavioral_themes (
            id                 TEXT PRIMARY KEY,
            name               TEXT NOT NULL,
            description        TEXT NOT NULL,
            correction_count   INTEGER DEFAULT 0,
            last_correction_at TEXT,
            created_at         TEXT NOT NULL
        )
    """,
    "behavioral_treatments": """
        CREATE TABLE IF NOT EXISTS behavioral_treatments (
            id                 TEXT PRIMARY KEY,
            theme_id           TEXT NOT NULL,
            treatment_type     TEXT NOT NULL,
            treatment_ref      TEXT NOT NULL,
            level              INTEGER NOT NULL,
            branch             TEXT NOT NULL,
            status             TEXT NOT NULL DEFAULT 'active',
            violation_count    INTEGER DEFAULT 0,
            last_violation_at  TEXT,
            last_adjusted_at   TEXT,
            adjustment_history TEXT NOT NULL DEFAULT '[]',
            created_at         TEXT NOT NULL
        )
    """,
    "memory_metadata": """
        CREATE TABLE IF NOT EXISTS memory_metadata (
            memory_id        TEXT PRIMARY KEY,
            created_at       TEXT NOT NULL,
            collection       TEXT NOT NULL DEFAULT 'episodic_memory',
            confidence       REAL,
            embedding_status TEXT NOT NULL DEFAULT 'embedded',
            memory_class     TEXT DEFAULT 'fact'
        )
    """,
    "code_modules": """
        CREATE TABLE IF NOT EXISTS code_modules (
            path             TEXT PRIMARY KEY,
            package          TEXT NOT NULL,
            module_name      TEXT NOT NULL,
            docstring        TEXT,
            loc              INTEGER NOT NULL,
            num_functions    INTEGER NOT NULL DEFAULT 0,
            num_classes      INTEGER NOT NULL DEFAULT 0,
            file_mtime       REAL NOT NULL,
            last_indexed_at  TEXT NOT NULL
        )
    """,
    "code_symbols": """
        CREATE TABLE IF NOT EXISTS code_symbols (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            module_path      TEXT NOT NULL REFERENCES code_modules(path) ON DELETE CASCADE,
            name             TEXT NOT NULL,
            symbol_type      TEXT NOT NULL,
            line_start       INTEGER NOT NULL,
            line_end         INTEGER,
            signature        TEXT,
            docstring        TEXT,
            is_public        INTEGER NOT NULL DEFAULT 1,
            parent_class     TEXT
        )
    """,
    "code_imports": """
        CREATE TABLE IF NOT EXISTS code_imports (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path      TEXT NOT NULL REFERENCES code_modules(path) ON DELETE CASCADE,
            target_module    TEXT NOT NULL,
            imported_names   TEXT,
            is_relative      INTEGER NOT NULL DEFAULT 0
        )
    """,
}

# FTS5 virtual tables (in-memory SQLite does NOT support FTS5 unless compiled with it)
FTS5_DDL = """
    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
        memory_id UNINDEXED,
        content,
        source_type,
        tags,
        collection UNINDEXED,
        tokenize='porter ascii'
    )
"""

KNOWLEDGE_FTS5_DDL = """
    CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
        unit_id UNINDEXED,
        concept,
        body,
        tags,
        domain UNINDEXED,
        project_type UNINDEXED,
        tokenize='porter ascii'
    )
"""

# ─── Indexes ──────────────────────────────────────────────────────────────────

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_procedural_task_type ON procedural_memory(task_type)",
    "CREATE INDEX IF NOT EXISTS idx_procedural_speculative ON procedural_memory(speculative)",
    "CREATE INDEX IF NOT EXISTS idx_procedural_activation_tier ON procedural_memory(activation_tier)",
    "CREATE INDEX IF NOT EXISTS idx_observations_source ON observations(source)",
    "CREATE INDEX IF NOT EXISTS idx_observations_type ON observations(type)",
    "CREATE INDEX IF NOT EXISTS idx_observations_resolved ON observations(resolved)",
    "CREATE INDEX IF NOT EXISTS idx_observations_priority ON observations(priority, resolved)",
    "CREATE INDEX IF NOT EXISTS idx_traces_outcome ON execution_traces(outcome_class)",
    "CREATE INDEX IF NOT EXISTS idx_traces_created ON execution_traces(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_surplus_status ON surplus_insights(promotion_status)",
    "CREATE INDEX IF NOT EXISTS idx_surplus_drive ON surplus_insights(drive_alignment)",
    "CREATE INDEX IF NOT EXISTS idx_surplus_ttl ON surplus_insights(ttl)",
    "CREATE INDEX IF NOT EXISTS idx_gaps_status ON capability_gaps(status)",
    "CREATE INDEX IF NOT EXISTS idx_gaps_type ON capability_gaps(gap_type)",
    "CREATE INDEX IF NOT EXISTS idx_gaps_frequency ON capability_gaps(frequency DESC)",
    "CREATE INDEX IF NOT EXISTS idx_claims_speculative ON speculative_claims(speculative)",
    "CREATE INDEX IF NOT EXISTS idx_claims_expiry ON speculative_claims(hypothesis_expiry)",
    "CREATE INDEX IF NOT EXISTS idx_outreach_channel ON outreach_history(channel)",
    "CREATE INDEX IF NOT EXISTS idx_outreach_category ON outreach_history(category)",
    "CREATE INDEX IF NOT EXISTS idx_outreach_delivered ON outreach_history(delivered_at)",
    "CREATE INDEX IF NOT EXISTS idx_outreach_outcome ON outreach_history(engagement_outcome)",
    "CREATE INDEX IF NOT EXISTS idx_outreach_dedup ON outreach_history(signal_type, topic, category, delivered_at)",
    "CREATE INDEX IF NOT EXISTS idx_outreach_content_hash ON outreach_history(signal_type, category, content_hash, delivered_at)",
    "CREATE INDEX IF NOT EXISTS idx_brainstorm_type ON brainstorm_log(session_type)",
    "CREATE INDEX IF NOT EXISTS idx_brainstorm_date ON brainstorm_log(created_at)",
    # GROUNDWORK(multi-person)
    "CREATE INDEX IF NOT EXISTS idx_observations_person ON observations(person_id)",
    "CREATE INDEX IF NOT EXISTS idx_observations_content_hash ON observations(source, content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_outreach_person ON outreach_history(person_id)",
    "CREATE INDEX IF NOT EXISTS idx_autonomy_person ON autonomy_state(person_id)",
    "CREATE INDEX IF NOT EXISTS idx_traces_person ON execution_traces(person_id)",
    # cost tracking
    "CREATE INDEX IF NOT EXISTS idx_cost_events_task ON cost_events(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_cost_events_created ON cost_events(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_cost_events_person ON cost_events(person_id)",
    "CREATE INDEX IF NOT EXISTS idx_cost_events_type ON cost_events(event_type)",
    "CREATE INDEX IF NOT EXISTS idx_budgets_type ON budgets(budget_type)",
    "CREATE INDEX IF NOT EXISTS idx_budgets_active ON budgets(active)",
    # surplus tasks
    "CREATE INDEX IF NOT EXISTS idx_surplus_tasks_status ON surplus_tasks(status)",
    "CREATE INDEX IF NOT EXISTS idx_surplus_tasks_priority ON surplus_tasks(priority DESC)",
    "CREATE INDEX IF NOT EXISTS idx_surplus_tasks_tier ON surplus_tasks(compute_tier)",
    # awareness loop
    "CREATE INDEX IF NOT EXISTS idx_ticks_depth ON awareness_ticks(classified_depth)",
    "CREATE INDEX IF NOT EXISTS idx_ticks_created ON awareness_ticks(created_at)",
    # dead letter
    "CREATE INDEX IF NOT EXISTS idx_dead_letter_status ON dead_letter(status)",
    "CREATE INDEX IF NOT EXISTS idx_dead_letter_provider ON dead_letter(target_provider)",
    # cognitive state
    "CREATE INDEX IF NOT EXISTS idx_cognitive_state_section ON cognitive_state(section)",
    # message queue
    "CREATE INDEX IF NOT EXISTS idx_mq_target ON message_queue(target)",
    "CREATE INDEX IF NOT EXISTS idx_mq_type ON message_queue(message_type)",
    "CREATE INDEX IF NOT EXISTS idx_mq_priority ON message_queue(priority)",
    "CREATE INDEX IF NOT EXISTS idx_mq_session ON message_queue(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_mq_created ON message_queue(created_at)",
    # cc sessions
    "CREATE INDEX IF NOT EXISTS idx_cc_sess_type ON cc_sessions(session_type)",
    "CREATE INDEX IF NOT EXISTS idx_cc_sess_status ON cc_sessions(status)",
    "CREATE INDEX IF NOT EXISTS idx_cc_sess_user_ch ON cc_sessions(user_id, channel)",
    "CREATE INDEX IF NOT EXISTS idx_cc_sess_activity ON cc_sessions(last_activity_at)",
    # memory links
    "CREATE INDEX IF NOT EXISTS idx_memory_links_source ON memory_links(source_id)",
    "CREATE INDEX IF NOT EXISTS idx_memory_links_target ON memory_links(target_id)",
    # inbox items
    "CREATE INDEX IF NOT EXISTS idx_inbox_items_status ON inbox_items(status)",
    "CREATE INDEX IF NOT EXISTS idx_inbox_items_file_path ON inbox_items(file_path)",
    "CREATE INDEX IF NOT EXISTS idx_inbox_items_batch_id ON inbox_items(batch_id)",
    # processed emails
    "CREATE INDEX IF NOT EXISTS idx_processed_emails_status ON processed_emails(status)",
    "CREATE INDEX IF NOT EXISTS idx_processed_emails_message_id ON processed_emails(message_id)",
    "CREATE INDEX IF NOT EXISTS idx_processed_emails_content_hash ON processed_emails(content_hash)",
    # deferred work queue
    "CREATE INDEX IF NOT EXISTS idx_deferred_work_status ON deferred_work_queue(status)",
    "CREATE INDEX IF NOT EXISTS idx_deferred_work_priority ON deferred_work_queue(priority)",
    "CREATE INDEX IF NOT EXISTS idx_deferred_work_type ON deferred_work_queue(work_type)",
    # pending embeddings
    "CREATE INDEX IF NOT EXISTS idx_pending_embeddings_status ON pending_embeddings(status)",
    "CREATE INDEX IF NOT EXISTS idx_pending_embeddings_memory ON pending_embeddings(memory_id)",
    # events (persistent observability)
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_events_subsystem ON events(subsystem)",
    "CREATE INDEX IF NOT EXISTS idx_events_severity ON events(severity)",
    "CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)",
    # calibration
    "CREATE INDEX IF NOT EXISTS idx_predictions_domain ON predictions(domain)",
    "CREATE INDEX IF NOT EXISTS idx_predictions_bucket ON predictions(confidence_bucket)",
    "CREATE INDEX IF NOT EXISTS idx_predictions_unmatched ON predictions(outcome) WHERE outcome IS NULL",
    # approval requests (Phase 9)
    "CREATE INDEX IF NOT EXISTS idx_approval_status ON approval_requests(status)",
    "CREATE INDEX IF NOT EXISTS idx_approval_class ON approval_requests(action_class)",
    "CREATE INDEX IF NOT EXISTS idx_approval_timeout ON approval_requests(timeout_at)",
    # task states (Phase 9)
    "CREATE INDEX IF NOT EXISTS idx_task_states_session ON task_states(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_task_states_phase ON task_states(current_phase)",
    # task steps (task executor)
    "CREATE INDEX IF NOT EXISTS idx_task_steps_status ON task_steps(status)",
    # cc_sessions: thread-aware lookup (Phase 9)
    "CREATE INDEX IF NOT EXISTS idx_cc_sess_user_ch_thread ON cc_sessions(user_id, channel, thread_id)",
    # telegram messages
    "CREATE INDEX IF NOT EXISTS idx_tg_msg_chat_ts ON telegram_messages(chat_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_tg_msg_thread ON telegram_messages(chat_id, thread_id, timestamp)",
    # session bookmarks
    "CREATE INDEX IF NOT EXISTS idx_session_bookmarks_cc ON session_bookmarks(cc_session_id)",
    "CREATE INDEX IF NOT EXISTS idx_session_bookmarks_type ON session_bookmarks(bookmark_type)",
    "CREATE INDEX IF NOT EXISTS idx_session_bookmarks_created ON session_bookmarks(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_session_bookmarks_cc_source ON session_bookmarks(cc_session_id, source)",
    # activity log (provider activity tracking)
    "CREATE INDEX IF NOT EXISTS idx_activity_log_created ON activity_log(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_activity_log_provider ON activity_log(provider, created_at)",
    # ego subsystem
    "CREATE INDEX IF NOT EXISTS idx_ego_cycles_created ON ego_cycles(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_ego_cycles_compacted ON ego_cycles(compacted_into)",
    "CREATE INDEX IF NOT EXISTS idx_ego_proposals_status ON ego_proposals(status)",
    "CREATE INDEX IF NOT EXISTS idx_ego_proposals_created ON ego_proposals(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_ego_proposals_cycle ON ego_proposals(cycle_id)",
    "CREATE INDEX IF NOT EXISTS idx_ego_proposals_category ON ego_proposals(action_category, status)",
    "CREATE INDEX IF NOT EXISTS idx_ego_proposals_batch ON ego_proposals(batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_ego_proposals_expires ON ego_proposals(expires_at)",
    # behavioral immune system (BIS)
    "CREATE INDEX IF NOT EXISTS idx_bis_corrections_theme ON behavioral_corrections(theme_id)",
    "CREATE INDEX IF NOT EXISTS idx_bis_corrections_created ON behavioral_corrections(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_bis_corrections_severity ON behavioral_corrections(severity)",
    "CREATE INDEX IF NOT EXISTS idx_bis_themes_name ON behavioral_themes(name)",
    "CREATE INDEX IF NOT EXISTS idx_bis_treatments_theme ON behavioral_treatments(theme_id)",
    "CREATE INDEX IF NOT EXISTS idx_bis_treatments_status ON behavioral_treatments(status)",
    "CREATE INDEX IF NOT EXISTS idx_bis_treatments_branch ON behavioral_treatments(branch)",
    # memory metadata (companion to FTS5)
    "CREATE INDEX IF NOT EXISTS idx_memory_metadata_created ON memory_metadata(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_memory_metadata_collection ON memory_metadata(collection)",
    # codebase index
    "CREATE INDEX IF NOT EXISTS idx_code_symbols_module ON code_symbols(module_path)",
    "CREATE INDEX IF NOT EXISTS idx_code_symbols_name ON code_symbols(name)",
    "CREATE INDEX IF NOT EXISTS idx_code_symbols_type ON code_symbols(symbol_type)",
    "CREATE INDEX IF NOT EXISTS idx_code_imports_source ON code_imports(source_path)",
    "CREATE INDEX IF NOT EXISTS idx_code_imports_target ON code_imports(target_module)",
]

# ─── Seed Data ────────────────────────────────────────────────────────────────

SIGNAL_WEIGHTS_SEED = [
    ("conversations_since_reflection", "genesis", 0.40, 0.40, 0.0, 1.0, '["Micro","Light"]'),
    ("task_completion_quality", "genesis", 0.50, 0.50, 0.0, 1.0, '["Micro","Light"]'),
    ("outreach_engagement_data", "outreach_mcp", 0.45, 0.45, 0.0, 1.0, '["Micro","Deep"]'),
    ("recon_findings_pending", "recon_mcp", 0.35, 0.35, 0.0, 1.0, '["Light","Deep"]'),
    # (Removed 2026-04-11) unprocessed_memory_backlog — retrieval-coverage
    # metric was being misread as reflection urgency by the Deep scorer.
    # Signal collectors and cognitive-state flag removed in the same sweep.
    # Existing rows cleaned up by _migrate_add_columns() on next boot.
    ("budget_pct_consumed", "health_mcp", 0.40, 0.40, 0.0, 1.0, '["Light","Deep"]'),
    ("software_error_spike", "health_mcp", 0.70, 0.70, 0.0, 1.0, '["Micro"]'),
    ("critical_failure", "health_mcp", 0.70, 0.70, 0.0, 1.0, '["Micro"]'),
    ("time_since_last_strategic", "clock", 0.50, 0.50, 0.0, 1.0, '["Strategic"]'),
    ("micro_count_since_light", "awareness_loop", 0.50, 0.50, 0.0, 1.0, '["Light"]'),
    ("cc_version_changed", "awareness_loop", 0.60, 0.60, 0.0, 1.0, '["Light"]'),
]

DEPTH_THRESHOLDS_SEED = [
    # (depth_name, threshold, floor_seconds, ceiling_count, ceiling_window_seconds)
    # Thresholds tuned 2026-03-21: originals (0.5/0.8/0.55) were too conservative,
    # producing only ~12 reflections across 6800 ticks.  Deep lowered to 0.45 to
    # encourage more frequent consolidation (design doc says 48-72h floor).
    ("Micro", 0.30, 1800, 2, 3600),         # floor 30min, max 2/hr
    ("Light", 0.60, 10800, 1, 3600),         # floor 3h, max 1/hr
    ("Deep", 0.45, 172800, 1, 86400),        # floor 48h, max 1/day
    ("Strategic", 0.40, 604800, 1, 604800),  # floor 7d, max 1/wk
]

BUDGET_SEED = [
    ("budget_daily", "daily", 2.00, 0.80, "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    ("budget_weekly", "weekly", 10.00, 0.80, "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
    ("budget_monthly", "monthly", 30.00, 0.80, "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
]

DRIVE_WEIGHTS_SEED = [
    ("preservation", 0.35, 0.35, 0.10, 0.50),
    ("curiosity", 0.25, 0.25, 0.10, 0.50),
    ("cooperation", 0.25, 0.25, 0.10, 0.50),
    ("competence", 0.15, 0.15, 0.10, 0.50),
]

