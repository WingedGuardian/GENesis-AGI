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
            scenario          TEXT,                -- "when to use this" trigger condition (ReMe omega)
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
            draft             INTEGER NOT NULL DEFAULT 1,  -- untested draft (was: speculative)
            invocation_count  INTEGER NOT NULL DEFAULT 0,
            attempted_workarounds TEXT,            -- JSON: array of {description, outcome, conditions}
            version           INTEGER NOT NULL DEFAULT 1,
            created_at        TEXT NOT NULL,
            activation_tier   TEXT NOT NULL DEFAULT 'DORMANT',  -- CORE/ADVISORY/LIBRARY/DORMANT promotion tier
            tool_trigger      TEXT,                        -- JSON array of tool names for CORE matching
            source            TEXT,                        -- JSON: {type, session_id?, observation_id?, triage_outcome?}
            promotion_history TEXT,                        -- JSON array: [{from_tier, to_tier, at, reason}]
            principle_embedding BLOB,                      -- qwen3-embedding(1024 float32) of `principle`, little-endian; read by proactive procedure hook
            surfaced_count    INTEGER NOT NULL DEFAULT 0   -- contextual-hook surfacings (proactive hook / tool advisor); honest funnel signal, NOT read by promoter
        )
    """,
    "attention_events": """
        CREATE TABLE IF NOT EXISTS attention_events (
            id                TEXT PRIMARY KEY,
            ts                TEXT NOT NULL,               -- event ts (utterance end), ISO8601 UTC
            session_id        TEXT NOT NULL,
            activation        TEXT NOT NULL,               -- hard | soft | suppressed
            score             REAL NOT NULL,
            triggers_fired    TEXT NOT NULL DEFAULT '[]',  -- JSON [{name,kind,contribution}] — NO transcript text
            suppressors       TEXT NOT NULL DEFAULT '[]',  -- JSON [name]
            window_ref        TEXT NOT NULL,               -- JSON {snapshot_id,session_id,utt_ids,ts_start,ts_end} — REFS ONLY
            mode_state        TEXT,
            clarity           REAL,
            l15_verdict       TEXT,                        -- JSON {real,perk,category,reason,...}; the judge's verdict (PR3d)
            acceptance_signal TEXT,                        -- should|shouldnt|skip; back-filled at shadow review (PR2)
            acceptance_note   TEXT,                        -- reviewer's optional one-line WHY (PR3d); their reasoning, not the judge's
            snapshot_id       TEXT,
            config_version    TEXT,
            created_at        TEXT NOT NULL,
            source            TEXT                         -- device provenance of the trigger utterance (e.g. omi / edge id)
            -- SHADOW/OFFLINE-ONLY firewall table: attention DECISIONS + REFERENCES only,
            -- NEVER ambient transcript text (that lives+dies in ambient.db on the edge).
            -- Not read by any cognition job (dream/ego/memory-synthesis).
        )
    """,
    "capability_shadow_events": """
        CREATE TABLE IF NOT EXISTS capability_shadow_events (
            id               TEXT PRIMARY KEY,
            observed_at      TEXT NOT NULL,      -- ISO8601 UTC — when the send was observed
            path             TEXT NOT NULL,      -- egress door: deliver | poll | reply
            channel          TEXT NOT NULL,      -- 'discord'
            cell_domain      TEXT NOT NULL,      -- capability cell domain ('discord')
            cell_verb        TEXT NOT NULL,      -- send | poll | reply
            cell_risk_class  TEXT NOT NULL,      -- bulk | standard | identity
            cell_state       TEXT,               -- capability_grants.state at observation; NULL => not_determined
            would_hold       INTEGER NOT NULL,   -- 1 = a live gate WOULD hold this send; 0 = would allow (GRANTED)
            target           TEXT,               -- routing target (webhook/channel_id/recipient) — NOT content
            content_preview  TEXT,               -- truncated excerpt (<=200 chars); NOT paired with content_hash
            content_hash     TEXT                -- hash over the FULL content; != content_preview
            -- WS5 Discord capability-gate SHADOW store: gate DECISIONS + refs only.
            -- Observe-only — no hold/approval is created here; the send always proceeds.
        )
    """,
    "immunity_shadow_events": """
        CREATE TABLE IF NOT EXISTS immunity_shadow_events (
            id            TEXT PRIMARY KEY,
            observed_at   TEXT NOT NULL,      -- ISO8601 UTC — when the recall/inject was observed
            gate          TEXT NOT NULL,      -- procedure | identity | autonomy | injection
            mode          TEXT NOT NULL,      -- shadow | enforce (mode at observation; 'off' never records)
            origin_class  TEXT NOT NULL,      -- blockable origin (external_untrusted for gate 4); a row is written only when the DERIVED origin is blockable
            would_block   INTEGER NOT NULL,   -- 1 = a live gate WOULD block; kept uniform for gates 1-3 forward-compat
            source_kind   TEXT,               -- site class: recall_inject | proactive_hook | ...
            source_ref    TEXT,               -- the site: 'mcp/memory/core.py::memory_recall'
            detail        TEXT,               -- freeform (e.g. blockable item count); NEVER recalled content
            process       TEXT                -- server | proactive_hook | outreach_mcp | ...
            -- WS-3 immunity SHADOW store: gate DECISIONS + provenance refs only. Observe-only
            -- — no recall is blocked or altered here; the item still reaches the prompt
            -- (wrapped as it already was). Never stores recalled content. Not read by any
            -- cognition job.
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
            content_hash     TEXT,
            surfaced_at      TEXT,
            surfaced_count   INTEGER NOT NULL DEFAULT 0,
            origin_class     TEXT
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
                'digest', 'surplus', 'approval', 'content', 'notification'
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
            -- NULL allowed via nullability (never via NULL-in-IN-list — that
            -- makes the CHECK a no-op under SQL three-valued logic, the
            -- 54e0fa72 bug). Vocabulary = POSITIVE_ENGAGEMENT_OUTCOMES ∪
            -- negatives; the _migrate_add_columns 'engaged' rebuild is the
            -- upgrade path and probes on the 'engaged' fragment.
            engagement_outcome  TEXT CHECK (
                engagement_outcome IS NULL OR engagement_outcome IN (
                'useful', 'engaged', 'acted_on', 'acknowledged',
                'not_useful', 'ambivalent', 'ignored'
            )),
            engagement_signal   TEXT,
            prediction_error    REAL,
            created_at          TEXT NOT NULL
        )
    """,
    "pending_outreach": """
        CREATE TABLE IF NOT EXISTS pending_outreach (
            id                  TEXT PRIMARY KEY,
            message             TEXT NOT NULL,
            category            TEXT NOT NULL,
            channel             TEXT NOT NULL DEFAULT 'telegram',
            urgency             TEXT NOT NULL DEFAULT 'low',
            deliver_after       TEXT,
            created_at          TEXT NOT NULL,
            delivered           INTEGER NOT NULL DEFAULT 0,
            delivered_at        TEXT,
            thread_id           TEXT,
            validated_recipient TEXT
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
            cache_read_tokens INTEGER,
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
            created_at       TEXT NOT NULL,
            dispatched       INTEGER NOT NULL DEFAULT 0
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
            attempt_count     INTEGER NOT NULL DEFAULT 0,
            not_before        TEXT,
            -- Verified-correctness verdict for insight-producing tasks (see
            -- surplus.types.INSIGHT_PRODUCING_TASK_TYPES), set by the
            -- measurement-only quality judge (surplus.quality_judge). 'useful' =
            -- judge passed the output; 'hollow' = judge failed it (harvested as a
            -- VERIFICATION_FAILED negative). NULL = action task, legacy row,
            -- unknown type, judge outage, or empty/too-short output (not penalized).
            outcome_quality   TEXT CHECK (outcome_quality IN ('useful', 'hollow')),
            -- Continuous [0,1] quality score + JSON rationale from the judge, for
            -- calibration/display (NOT read by the Outcome Bus harvester).
            judge_score       REAL,
            judge_detail      TEXT
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
                'active_context', 'pending_actions', 'state_flags',
                'resilience_degradation'
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
            rate_limit_resumes_at TEXT,
            origin_class     TEXT
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
            evaluated_content TEXT,
            drop_id        TEXT,
            batch_items    TEXT
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
            -- link_type is part of the PK (audit DLI-04 / D15): distinct
            -- relationship types between the same pair (e.g. supports AND
            -- contradicts) must coexist, not silently overwrite. Migration
            -- 0029 brings existing DBs to this shape.
            PRIMARY KEY (source_id, target_id, link_type)
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
            source_pipeline     TEXT,
            source_subsystem    TEXT
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
    "outcome_events": """
        CREATE TABLE IF NOT EXISTS outcome_events (
            id                TEXT PRIMARY KEY,
            source            TEXT NOT NULL,
            ref_type          TEXT NOT NULL,
            ref_id            TEXT NOT NULL,
            domain            TEXT,
            signal_type       TEXT NOT NULL,
            signal_class      TEXT NOT NULL DEFAULT 'implicit'
                                  CHECK (signal_class IN ('implicit', 'explicit')),
            signal_tier       INTEGER NOT NULL CHECK (signal_tier IN (1, 2, 3)),
            polarity          TEXT CHECK (polarity IN ('positive', 'negative', 'neutral')),
            value             REAL,
            stated_confidence REAL,
            prediction_error  REAL,
            reason            TEXT,
            reason_text       TEXT,
            metadata          TEXT,
            harvested_from    TEXT,
            occurred_at       TEXT NOT NULL,
            created_at        TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (source, ref_type, ref_id, signal_type)
        )
    """,
    "ego_calibration_snapshots": """
        CREATE TABLE IF NOT EXISTS ego_calibration_snapshots (
            id              TEXT PRIMARY KEY,
            domain          TEXT NOT NULL,
            ece             REAL NOT NULL,
            mce             REAL NOT NULL,
            sample_count    INTEGER NOT NULL,
            bucket_count    INTEGER NOT NULL,
            low_confidence  INTEGER NOT NULL DEFAULT 0,
            curve_json      TEXT NOT NULL,
            computed_at     TEXT NOT NULL DEFAULT (datetime('now'))
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
            resolved_by      TEXT,
            consumed_at      TEXT,
            content_hash     TEXT,                    -- SHA-256 of canonical(action_type,class,desc,ctx)
            previous_hash    TEXT,                    -- chain link: previous record's chain_hash
            chain_hash       TEXT                     -- SHA-256(previous_hash:content_hash)
        )
    """,
    # Capability-grant matrix (WS-8) — per-(domain, verb, risk_class) cells.
    # Replaces the linear L1–L7 ladder for ported channel-domains (email
    # first).  DARK in PR-B: no runtime reader/writer yet; autonomy_state
    # stays authoritative until PR-C ships enforcement + the L1–L7 read-out.
    "capability_grants": """
        CREATE TABLE IF NOT EXISTS capability_grants (
            id            TEXT PRIMARY KEY,           -- "{domain}:{verb}:{risk_class}"
            domain        TEXT NOT NULL,              -- channel-domain, e.g. 'email'
            verb          TEXT NOT NULL,              -- e.g. 'send'
            risk_class    TEXT NOT NULL DEFAULT 'standard' CHECK (risk_class IN (
                'standard', 'identity', 'bulk', 'financial'
            )),
            state         TEXT NOT NULL DEFAULT 'not_determined' CHECK (state IN (
                'not_determined', 'ask', 'granted', 'denied_permanent'
            )),
            successes     INTEGER NOT NULL DEFAULT 0,
            corrections   INTEGER NOT NULL DEFAULT 0,
            weighted_corrections REAL NOT NULL DEFAULT 0.0,  -- WS-8 PR-D: Σ consequence-weighted corrections (governs re-earn difficulty)
            granted_at    TEXT,                       -- when cell most recently entered 'granted' (decay-clock origin)
            last_used_at  TEXT,
            last_decayed_at TEXT,                     -- WS-8 PR-D: most recent staleness-decay sweep that touched this cell
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (domain, verb, risk_class)
        )
    """,
    # WS-8 email autonomy gate hold store — a held outbound email awaiting
    # owner approval (gate lives in outreach.pipeline._deliver).  request_id
    # UNIQUE = the schema-level double-send guard.
    "pending_email_sends": """
        CREATE TABLE IF NOT EXISTS pending_email_sends (
            id                  TEXT PRIMARY KEY,
            request_id          TEXT NOT NULL UNIQUE,
            thread_id           TEXT,
            validated_recipient TEXT NOT NULL,
            channel             TEXT NOT NULL DEFAULT 'email',
            category            TEXT NOT NULL,
            message             TEXT NOT NULL,
            cell_domain         TEXT NOT NULL,
            cell_verb           TEXT NOT NULL,
            cell_risk_class     TEXT NOT NULL,
            held_at             TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'held'
                                    CHECK (status IN ('held', 'sent', 'rejected', 'expired')),
            sent_at             TEXT,
            rejected_at         TEXT
        )
    """,
    # WS-8 PR-D autonomous-send ledger — one row per email sent autonomously
    # under a GRANTED capability cell (i.e. the gate allowed it without holding
    # for owner approval).  This is the keystone the owner-visibility "Activity"
    # tab, the flag-as-bad correction, and the per-cell rate-limit guard all read
    # (outreach_history carries no recipient/thread/cell column).  Approved (held
    # then owner-approved) sends are NOT logged here — only autonomous ones.
    "autonomous_email_sends": """
        CREATE TABLE IF NOT EXISTS autonomous_email_sends (
            id                  TEXT PRIMARY KEY,
            outreach_id         TEXT,                   -- link to outreach_history.id
            thread_id           TEXT,
            recipient           TEXT NOT NULL,
            subject             TEXT,
            cell_domain         TEXT NOT NULL,
            cell_verb           TEXT NOT NULL,
            cell_risk_class     TEXT NOT NULL,
            sent_at             TEXT NOT NULL,
            flagged_at          TEXT,                   -- owner flagged this send as bad → correction recorded on the cell
            created_at          TEXT NOT NULL DEFAULT (datetime('now'))
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
            intake_token     TEXT,
            source           TEXT NOT NULL DEFAULT 'user',
            created_at       TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """,
    "build_candidates": """
        CREATE TABLE IF NOT EXISTS build_candidates (
            id                  TEXT PRIMARY KEY,
            item_key            TEXT NOT NULL,
            item_title          TEXT NOT NULL,
            source_file         TEXT NOT NULL,
            batch_id            TEXT,
            eval_path           TEXT,
            verdict             TEXT NOT NULL CHECK (
                verdict IN ('build', 'dont_build', 'needs_discussion')
            ),
            verdict_reason      TEXT,
            confidence          TEXT,
            build_spec          TEXT,
            plan_path           TEXT,
            approval_request_id TEXT,
            user_decision       TEXT CHECK (
                user_decision IN ('approved', 'rejected', 'discussed')
            ),
            decided_at          TEXT,
            task_id             TEXT,
            branch              TEXT,
            pr_url              TEXT,
            outcome             TEXT NOT NULL DEFAULT 'pending' CHECK (
                outcome IN ('pending', 'submitted', 'built', 'pr_opened',
                            'scope_blocked', 'build_failed', 'abandoned')
            ),
            scope_gate_result   TEXT,
            created_at          TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """,
    "intake_tokens": """
        CREATE TABLE IF NOT EXISTS intake_tokens (
            token            TEXT PRIMARY KEY,
            created_at       TEXT NOT NULL,
            expires_at       TEXT NOT NULL,
            consumed_at      TEXT,
            task_id          TEXT
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
            retrieved_count  INTEGER NOT NULL DEFAULT 0,
            source_pipeline  TEXT,
            purpose          TEXT,
            ingestion_source TEXT,
            origin_class     TEXT,
            UNIQUE(project_type, domain, concept)
        )
    """,
    "credential_access_log": """
        CREATE TABLE IF NOT EXISTS credential_access_log (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            unit_id           TEXT NOT NULL,
            accessor_context  TEXT,
            accessed_at       TEXT NOT NULL,
            query_match_score REAL
        )
    """,
    # NOTE: intentionally NO foreign key to knowledge_units. Audit rows
    # outlive the entries they describe — deleting a reference entry (e.g.
    # password rotation: delete + re-store) must not cascade away the
    # access history.  The unit_id is a soft reference only.
    "knowledge_uploads": """
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
            chunks_total  INTEGER,
            chunks_done   INTEGER DEFAULT 0,
            created_at    TEXT NOT NULL,
            completed_at  TEXT
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
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            provider      TEXT NOT NULL,
            latency_ms    REAL NOT NULL,
            success       INTEGER NOT NULL DEFAULT 1,
            cache_hit     INTEGER NOT NULL DEFAULT 0,
            error_message TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
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
            compacted_into  TEXT,                    -- set when folded into compacted summary
            output_hash     TEXT,                    -- SHA-256 of output_text for audit trail
            output_size     INTEGER,                 -- byte count of output_text
            ego_source      TEXT,                    -- 'user_ego_cycle' or 'genesis_ego_cycle'
            previous_hash   TEXT,                    -- chain link: previous record's chain_hash
            chain_hash      TEXT                     -- SHA-256(previous_hash:output_hash)
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
            status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'executed', 'failed', 'tabled', 'withdrawn')),
            user_response   TEXT,                     -- rejection reason, approval notes
            cycle_id        TEXT,                     -- FK to ego_cycles.id
            batch_id        TEXT,                     -- groups proposals into digest batches
            created_at      TEXT NOT NULL,
            resolved_at     TEXT,
            expires_at      TEXT,                     -- auto-expiry timestamp
            rank            INTEGER,                  -- board position (lower = higher priority)
            execution_plan  TEXT,                     -- dispatch instructions for approved proposals
            recurring       INTEGER DEFAULT 0,        -- 1 if ongoing/recurring commitment
            memory_basis    TEXT DEFAULT '',           -- non-obvious memory attribution
            realist_verdict  TEXT,                     -- realist gate: pass/amend/reject
            realist_reasoning TEXT,                    -- realist gate: explanation
            ego_source       TEXT,                    -- which ego created this (user_ego_cycle / genesis_ego_cycle)
            goal_id          TEXT,                    -- FK to user_goals.id (nullable — not all proposals serve a goal)
            content_hash     TEXT,                    -- SHA-256 of content at creation time
            content_size     INTEGER,                 -- byte count of content at creation time
            original_content TEXT,                    -- pre-realist-amendment content (NULL if not amended)
            expected_outputs TEXT                     -- JSON: post-dispatch verification criteria (files, min_size, required_strings)
        )
    """,
    "ego_state": """
        CREATE TABLE IF NOT EXISTS ego_state (
            key             TEXT PRIMARY KEY,
            value           TEXT NOT NULL,
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """,
    # ── Ego Directives ─────────────────────────────────────────────────────
    "ego_directives": """
        CREATE TABLE IF NOT EXISTS ego_directives (
            id          TEXT PRIMARY KEY,
            content     TEXT NOT NULL,
            priority    TEXT NOT NULL DEFAULT 'normal'
                CHECK (priority IN ('low', 'normal', 'high', 'critical')),
            source      TEXT NOT NULL DEFAULT 'user',
            ego_target  TEXT NOT NULL DEFAULT 'user_ego',
            status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'completed', 'cancelled')),
            created_at  TEXT NOT NULL,
            resolved_at TEXT,
            resolution  TEXT
        )
    """,
    # ── Ego Intentions Queue ──────────────────────────────────────────────
    "ego_intentions": """
        CREATE TABLE IF NOT EXISTS ego_intentions (
            id                TEXT PRIMARY KEY,
            content           TEXT NOT NULL,          -- what to propose when triggered
            trigger_condition TEXT NOT NULL,           -- when to fire (natural language)
            ego_source        TEXT NOT NULL,           -- 'user_ego_cycle' or 'genesis_ego_cycle'
            status            TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'fired', 'expired', 'withdrawn')),
            created_at        TEXT NOT NULL,
            fired_at          TEXT,
            proposal_id       TEXT,                   -- FK to ego_proposals.id (set on fire)
            cycle_count       INTEGER NOT NULL DEFAULT 0,
            max_cycles        INTEGER NOT NULL DEFAULT 20,
            reasoning         TEXT,
            priority          TEXT NOT NULL DEFAULT 'normal'
                CHECK (priority IN ('low', 'normal', 'high'))
        )
    """,
    # ── Intervention Journal ────────────────────────────────────────────────
    "intervention_journal": """
        CREATE TABLE IF NOT EXISTS intervention_journal (
            id              TEXT PRIMARY KEY,
            ego_source      TEXT NOT NULL,               -- 'user_ego_cycle' or 'genesis_ego_cycle'
            proposal_id     TEXT,                        -- FK to ego_proposals.id
            cycle_id        TEXT,                        -- FK to ego_cycles.id
            action_type     TEXT NOT NULL,               -- from proposal.action_type
            action_summary  TEXT NOT NULL,               -- from proposal.content (truncated)
            expected_outcome TEXT NOT NULL DEFAULT '',   -- from proposal.rationale
            actual_outcome  TEXT,                        -- filled on resolution
            outcome_status  TEXT NOT NULL DEFAULT 'pending'
                CHECK (outcome_status IN ('pending', 'approved', 'rejected',
                       'executed', 'failed', 'tabled', 'withdrawn', 'expired')),
            user_response   TEXT,                        -- user feedback on resolve
            confidence      REAL DEFAULT 0.0,            -- from proposal.confidence
            created_at      TEXT NOT NULL,
            resolved_at     TEXT
        )
    """,
    # ── Capability Map ─────────────────────────────────────────────────────
    "capability_map": """
        CREATE TABLE IF NOT EXISTS capability_map (
            id              TEXT PRIMARY KEY,
            domain          TEXT NOT NULL UNIQUE,         -- e.g. 'investigate', 'outreach'
            confidence      REAL NOT NULL DEFAULT 0.0,    -- 0.0-1.0 composite score
            sample_size     INTEGER NOT NULL DEFAULT 0,   -- data points aggregated
            trend           TEXT DEFAULT 'stable'
                CHECK (trend IN ('improving', 'stable', 'declining')),
            evidence_summary TEXT,                        -- brief rationale
            updated_at      TEXT NOT NULL,
            previous_confidence REAL                      -- last refresh score for trend detection
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
            memory_class     TEXT DEFAULT 'fact',
            wing             TEXT,
            room             TEXT,
            valid_at         TEXT,
            invalid_at       TEXT,
            source_subsystem TEXT,
            deprecated       INTEGER NOT NULL DEFAULT 0,
            dream_cycle_run_id TEXT,
            origin_class     TEXT
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
    "follow_ups": """
        CREATE TABLE IF NOT EXISTS follow_ups (
            id               TEXT PRIMARY KEY,
            source           TEXT NOT NULL,
            source_session   TEXT,
            content          TEXT NOT NULL,
            reason           TEXT,
            strategy         TEXT NOT NULL CHECK (
                strategy IN ('scheduled_task', 'surplus_task', 'ego_judgment', 'user_input_needed')
            ),
            scheduled_at     TEXT,
            status           TEXT NOT NULL DEFAULT 'pending' CHECK (
                status IN ('pending', 'scheduled', 'in_progress', 'completed', 'failed', 'blocked')
            ),
            linked_task_id   TEXT,
            priority         TEXT NOT NULL DEFAULT 'medium' CHECK (
                priority IN ('low', 'medium', 'high', 'critical')
            ),
            created_at       TEXT NOT NULL,
            completed_at     TEXT,
            resolution_notes TEXT,
            blocked_reason   TEXT,
            escalated_to     TEXT,
            verified_at      TEXT,
            verification_notes TEXT,
            pinned           INTEGER NOT NULL DEFAULT 0,
            kind             TEXT NOT NULL DEFAULT 'follow_up' CHECK (
                kind IN ('follow_up', 'tabled')
            ),
            domain           TEXT CHECK (
                domain IN ('internal', 'user_world')
            ),
            goal_id          TEXT,
            dedup_key        TEXT
        )
    """,
    "file_modifications": """
        CREATE TABLE IF NOT EXISTS file_modifications (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id       TEXT,
            file_path        TEXT NOT NULL,
            action           TEXT NOT NULL,
            tool_name        TEXT,
            file_hash        TEXT,
            timestamp        TEXT NOT NULL
        )
    """,
    "cognitive_file_modifications": """
        CREATE TABLE IF NOT EXISTS cognitive_file_modifications (
            id              TEXT PRIMARY KEY,
            actor           TEXT NOT NULL,
            target_path     TEXT NOT NULL,
            prior_content   TEXT,
            applied_content TEXT NOT NULL,
            change_summary  TEXT,
            metadata        TEXT,
            status          TEXT NOT NULL DEFAULT 'applied',
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            rolled_back_at  TEXT
        )
    """,
    "tool_call_outcomes": """
        CREATE TABLE IF NOT EXISTS tool_call_outcomes (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id       TEXT,
            tool_name        TEXT NOT NULL,
            file_path        TEXT,
            success          INTEGER NOT NULL DEFAULT 1,
            error_snippet    TEXT,
            timestamp        TEXT NOT NULL
        )
    """,
    "direct_session_queue": """
        CREATE TABLE IF NOT EXISTS direct_session_queue (
            id              TEXT PRIMARY KEY,
            payload_json    TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'claimed', 'dispatched', 'failed')),
            session_id      TEXT,
            error_message   TEXT,
            created_at      TEXT NOT NULL,
            claimed_at      TEXT,
            dispatched_at   TEXT
        )
    """,
    "eval_events": """
        CREATE TABLE IF NOT EXISTS eval_events (
            id           TEXT PRIMARY KEY,
            timestamp    TEXT NOT NULL,
            dimension    TEXT NOT NULL
                         CHECK (dimension IN (
                             'memory', 'ego', 'procedure', 'cognitive', 'system'
                         )),
            event_type   TEXT NOT NULL,
            subject_id   TEXT,
            session_id   TEXT,
            metrics_json TEXT NOT NULL,
            created_at   TEXT NOT NULL
                         DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    """,
    "eval_snapshots": """
        CREATE TABLE IF NOT EXISTS eval_snapshots (
            id           TEXT PRIMARY KEY,
            period_start TEXT NOT NULL,
            period_end   TEXT NOT NULL,
            period_type  TEXT NOT NULL
                         CHECK (period_type IN ('daily', 'weekly')),
            dimension    TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            sample_count INTEGER NOT NULL,
            created_at   TEXT NOT NULL
                         DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    """,
    "entity_resolution_audit": """
        CREATE TABLE IF NOT EXISTS entity_resolution_audit (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          TEXT NOT NULL,
            action          TEXT NOT NULL
                            CHECK (action IN (
                                'auto_merge', 'llm_merge', 'contradiction',
                                'succeeded_by', 'flagged', 'skipped'
                            )),
            memory_id_a     TEXT NOT NULL,
            memory_id_b     TEXT NOT NULL,
            content_a       TEXT,
            content_b       TEXT,
            cosine_score    REAL,
            llm_verdict     TEXT,
            llm_reasoning   TEXT,
            survivor_id     TEXT,
            created_at      TEXT NOT NULL
                            DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    """,
    "centrality_cache": """
        CREATE TABLE IF NOT EXISTS centrality_cache (
            memory_id        TEXT PRIMARY KEY,
            centrality_score REAL NOT NULL,
            computed_at      TEXT NOT NULL
        )
    """,
    "eval_subsystem_grades": """
        CREATE TABLE IF NOT EXISTS eval_subsystem_grades (
            id           TEXT PRIMARY KEY,
            period_start TEXT NOT NULL,
            period_end   TEXT NOT NULL,
            period_type  TEXT NOT NULL
                         CHECK (period_type IN ('daily', 'weekly')),
            subsystem    TEXT NOT NULL
                         CHECK (subsystem IN (
                             'memory', 'ego', 'procedural', 'awareness', 'reflection'
                         )),
            grade        TEXT,
            score        REAL,
            factors_json TEXT NOT NULL,
            sample_count INTEGER NOT NULL,
            created_at   TEXT NOT NULL
                         DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    """,
    "user_goals": """
        CREATE TABLE IF NOT EXISTS user_goals (
            id              TEXT PRIMARY KEY,
            title           TEXT NOT NULL,
            description     TEXT,
            category        TEXT NOT NULL
                            CHECK (category IN (
                                'career', 'project', 'learning',
                                'relationship', 'financial', 'other'
                            )),
            priority        TEXT NOT NULL DEFAULT 'medium'
                            CHECK (priority IN ('low', 'medium', 'high', 'critical')),
            status          TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN (
                                'active', 'paused', 'achieved', 'abandoned'
                            )),
            timeline        TEXT,
            progress_notes  TEXT DEFAULT '[]',
            parent_goal_id  TEXT,
            evidence_source TEXT,
            confidence      REAL NOT NULL DEFAULT 0.5,
            goal_type       TEXT NOT NULL DEFAULT 'milestone'
                            CHECK (goal_type IN ('milestone', 'continuous')),
            -- Provenance: who owns this goal. 'user' = a user directive
            -- (NEVER autonomously mutable); 'genesis_ego' = ego-created
            -- (the ego may pause/deprioritize it without a proposal).
            -- Immutable after create — update() excludes it (migration 0063).
            origin          TEXT NOT NULL DEFAULT 'user'
                            CHECK (origin IN ('user', 'genesis_ego')),
            cadence_days    INTEGER,              -- per-goal review cadence override (NULL = global default)
            created_at      TEXT NOT NULL
                            DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at      TEXT NOT NULL
                            DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            achieved_at     TEXT
        )
    """,
    "user_contacts": """
        CREATE TABLE IF NOT EXISTS user_contacts (
            id                TEXT PRIMARY KEY,
            name              TEXT NOT NULL,
            relationship      TEXT,
            organization      TEXT,
            role              TEXT,
            relevance         TEXT,
            last_mentioned    TEXT,
            interaction_count INTEGER NOT NULL DEFAULT 1,
            context_notes     TEXT DEFAULT '[]',
            linked_goal_ids   TEXT DEFAULT '[]',
            source            TEXT NOT NULL DEFAULT 'conversation',
            created_at        TEXT NOT NULL
                              DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at        TEXT NOT NULL
                              DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    """,
    "memory_events": """
        CREATE TABLE IF NOT EXISTS memory_events (
            id                TEXT PRIMARY KEY,
            memory_id         TEXT NOT NULL,
            subject           TEXT NOT NULL,
            verb              TEXT NOT NULL,
            object            TEXT,
            event_date        TEXT,
            event_date_end    TEXT,
            confidence        REAL NOT NULL DEFAULT 0.5,
            source_session_id TEXT,
            created_at        TEXT NOT NULL
                              DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )
    """,
    "user_jobs": """
        CREATE TABLE IF NOT EXISTS user_jobs (
            id                TEXT PRIMARY KEY,
            title             TEXT NOT NULL,
            description       TEXT,
            cron_expression   TEXT NOT NULL,
            job_type          TEXT NOT NULL DEFAULT 'generic',
            config_json       TEXT,
            dispatch_prompt   TEXT NOT NULL,
            profile           TEXT NOT NULL DEFAULT 'observe',
            model             TEXT NOT NULL DEFAULT 'sonnet',
            effort            TEXT NOT NULL DEFAULT 'medium',
            status            TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'paused', 'disabled')),
            last_run_at       TEXT,
            last_status       TEXT CHECK (last_status IN ('passed', 'failed', 'running', NULL)),
            last_result_json  TEXT,
            next_run_at       TEXT,
            failure_count     INTEGER NOT NULL DEFAULT 0,
            created_at        TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """,
    "user_job_runs": """
        CREATE TABLE IF NOT EXISTS user_job_runs (
            id                TEXT PRIMARY KEY,
            job_id            TEXT NOT NULL REFERENCES user_jobs(id),
            status            TEXT NOT NULL DEFAULT 'running'
                              CHECK (status IN ('running', 'passed', 'failed')),
            session_id        TEXT,
            started_at        TEXT NOT NULL DEFAULT (datetime('now')),
            completed_at      TEXT,
            result_json       TEXT,
            error_message     TEXT
        )
    """,
    "task_type_watermarks": """
        CREATE TABLE IF NOT EXISTS task_type_watermarks (
            task_type            TEXT PRIMARY KEY,
            best_outcome         TEXT NOT NULL,
            best_outcome_at      TEXT NOT NULL,
            total_sessions       INTEGER NOT NULL DEFAULT 0,
            successful_sessions  INTEGER NOT NULL DEFAULT 0,
            last_session_at      TEXT NOT NULL,
            updated_at           TEXT NOT NULL
        )
    """,
    "prompt_versions": """
        CREATE TABLE IF NOT EXISTS prompt_versions (
            hash         TEXT NOT NULL,
            call_site    TEXT NOT NULL,
            first_seen   TEXT NOT NULL
                         DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            content_preview TEXT,
            PRIMARY KEY (hash, call_site)
        )
    """,
    # ── Reflection Corpus ────────────────────────────────────────────────
    "reflection_corpus": """
        CREATE TABLE IF NOT EXISTS reflection_corpus (
            id                    TEXT PRIMARY KEY,
            depth                 TEXT NOT NULL,
            focus_area            TEXT,
            prompt_text           TEXT NOT NULL,
            response_text         TEXT NOT NULL,
            parsed_ok             INTEGER,
            model_used            TEXT,
            quality_score         REAL,
            quality_label         TEXT,
            graded_at             TEXT,
            tick_id               TEXT,
            created_at            TEXT NOT NULL,
            used_in_optimization  INTEGER DEFAULT 0
        )
    """,
    "campaigns": """
        CREATE TABLE IF NOT EXISTS campaigns (
            id                TEXT PRIMARY KEY,
            name              TEXT NOT NULL UNIQUE,
            strategy_doc_path TEXT NOT NULL,
            cron_cadence      TEXT NOT NULL,
            model             TEXT NOT NULL DEFAULT 'sonnet',
            effort            TEXT NOT NULL DEFAULT 'medium',
            session_profile   TEXT NOT NULL DEFAULT 'campaign',
            status            TEXT NOT NULL DEFAULT 'active' CHECK (
                status IN ('active', 'paused', 'completed', 'failed')
            ),
            state_json        TEXT NOT NULL DEFAULT '{}',
            pre_checks        TEXT NOT NULL DEFAULT '["rate_limit", "budget", "slots_available"]',
            max_daily_cost_usd REAL NOT NULL DEFAULT 1.0,
            created_at        TEXT NOT NULL,
            paused_at         TEXT,
            last_run_at       TEXT,
            total_runs        INTEGER NOT NULL DEFAULT 0,
            total_cost_usd    REAL NOT NULL DEFAULT 0.0,
            jitter_seconds    INTEGER
        )
    """,
    "campaign_runs": """
        CREATE TABLE IF NOT EXISTS campaign_runs (
            id              TEXT PRIMARY KEY,
            campaign_id     TEXT NOT NULL REFERENCES campaigns(id),
            started_at      TEXT NOT NULL,
            finished_at     TEXT,
            trigger_type    TEXT NOT NULL DEFAULT 'scheduled' CHECK (
                trigger_type IN ('scheduled', 'manual', 'event')
            ),
            outcome         TEXT NOT NULL DEFAULT 'pending' CHECK (
                outcome IN ('pending', 'success', 'skip', 'error')
            ),
            skip_reason     TEXT,
            summary         TEXT,
            cost_usd        REAL NOT NULL DEFAULT 0.0,
            session_id      TEXT,
            state_snapshot   TEXT
        )
    """,
    # ─── Email thread tracking ───────────────────────────────────────────────
    "email_threads": """
        CREATE TABLE IF NOT EXISTS email_threads (
            id                TEXT PRIMARY KEY,
            sent_message_id   TEXT NOT NULL UNIQUE,
            owner             TEXT NOT NULL DEFAULT 'outreach',
            owner_ref         TEXT,
            recipient         TEXT NOT NULL,
            subject           TEXT,
            context           TEXT,
            status            TEXT NOT NULL DEFAULT 'awaiting_reply' CHECK (
                status IN ('awaiting_reply', 'replied', 'follow_up_sent', 'closed')
            ),
            follow_up_after   TEXT,
            follow_up_sent_at TEXT,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        )
    """,
    "email_thread_messages": """
        CREATE TABLE IF NOT EXISTS email_thread_messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id       TEXT NOT NULL REFERENCES email_threads(id),
            message_id      TEXT NOT NULL,
            direction       TEXT NOT NULL CHECK (direction IN ('sent', 'received')),
            sender          TEXT,
            subject         TEXT,
            body_preview    TEXT,
            received_at     TEXT NOT NULL,
            UNIQUE(message_id)
        )
    """,
    "otel_spans": """
        CREATE TABLE IF NOT EXISTS otel_spans (
            span_id         TEXT PRIMARY KEY,
            trace_id        TEXT NOT NULL,
            parent_span_id  TEXT,
            name            TEXT NOT NULL,
            kind            TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'ok'
                                CHECK (status IN ('ok', 'error')),
            status_message  TEXT,
            start_unix_us   INTEGER NOT NULL,
            end_unix_us     INTEGER,
            duration_us     INTEGER,
            session_id      TEXT,
            process         TEXT,
            call_site       TEXT,
            provider        TEXT,
            model_id        TEXT,
            input_tokens    INTEGER,
            output_tokens   INTEGER,
            cost_usd        REAL,
            cost_known      INTEGER,
            attributes_json TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """,
    # Entity layer (WS-H Pillar 2, Graphiti blueprint on SQLite+Qdrant).
    # NOTE: distinct from memory-pair dedup ("entity resolution" in
    # memory/entity_resolution.py) — these are typed entity NODES.
    "entities": """
        CREATE TABLE IF NOT EXISTS entities (
            entity_id   TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            norm_name   TEXT NOT NULL,
            entity_type TEXT NOT NULL CHECK (entity_type IN (
                'code_file','code_symbol','pr','commit',
                'product','device','repo','subsystem','person','org','concept'
            )),
            summary     TEXT,
            source      TEXT NOT NULL DEFAULT 'extracted',
            status      TEXT NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active','merged','gone')),
            merged_into TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL,
            UNIQUE (norm_name, entity_type)
        )
    """,
    "entity_mentions": """
        CREATE TABLE IF NOT EXISTS entity_mentions (
            memory_id  TEXT NOT NULL,
            entity_id  TEXT NOT NULL,
            provenance TEXT NOT NULL CHECK (
                provenance IN ('EXTRACTED','INFERRED','AMBIGUOUS')
            ),
            confidence REAL NOT NULL DEFAULT 0.7,
            source     TEXT,
            created_at TEXT NOT NULL,
            -- No validity columns: "M mentions E" is an event-time fact;
            -- memory-level validity lives in memory_metadata.
            PRIMARY KEY (memory_id, entity_id)
        )
    """,
    "entity_links": """
        CREATE TABLE IF NOT EXISTS entity_links (
            source_id          TEXT NOT NULL,
            target_id          TEXT NOT NULL,
            -- Free-form slug by design (LLM-first open vocabulary);
            -- deliberately NOT the memory_links CHECK registry.
            link_type          TEXT NOT NULL,
            provenance         TEXT NOT NULL CHECK (
                provenance IN ('EXTRACTED','INFERRED','AMBIGUOUS')
            ),
            confidence         REAL NOT NULL DEFAULT 0.7,
            evidence_memory_id TEXT,
            valid_at           TEXT,
            invalid_at         TEXT,
            invalidated_by     TEXT,
            created_at         TEXT NOT NULL,
            PRIMARY KEY (source_id, target_id, link_type)
        )
    """,
    # Session-manager durable spine (PR-2a, migration 0058). session_id is the
    # CC transcript session id (= cc_sessions.cc_session_id, NOT cc_sessions.id).
    # origin_prompt/origin_ts are write-once: nullable so MCP stubs can exist
    # pre-first-compaction; every writer fills origin only WHERE origin_prompt
    # IS NULL and never lists origin columns in a general UPDATE SET.
    "session_charters": """
        CREATE TABLE IF NOT EXISTS session_charters (
            session_id       TEXT PRIMARY KEY,
            transcript_path  TEXT,
            origin_prompt    TEXT,
            origin_ts        TEXT,
            mission          TEXT,
            pointers         TEXT NOT NULL DEFAULT '[]',
            compaction_count INTEGER NOT NULL DEFAULT 0,
            created_at       TEXT NOT NULL,
            updated_at       TEXT
        )
    """,
    # Data-migration framework ledger (WS-C). Kept in LOCKSTEP with migration
    # 0060 (upgrade path); this is the fresh-install path. See
    # db/data_migrations/ for the runner + contract.
    "data_migrations": """
        CREATE TABLE IF NOT EXISTS data_migrations (
            id           TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            status       TEXT NOT NULL CHECK (
                status IN ('pending','running','completed','failed','operator_pending')
            ),
            attempts     INTEGER NOT NULL DEFAULT 0,
            started_at   TEXT,
            completed_at TEXT,
            error        TEXT,
            summary      TEXT,
            updated_at   TEXT NOT NULL
        )
    """,
    "session_ledger": """
        CREATE TABLE IF NOT EXISTS session_ledger (
            id          TEXT PRIMARY KEY,
            session_id  TEXT NOT NULL,
            text        TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','in_progress','done','absorbed','dropped')),
            source_ref  TEXT,
            added_by    TEXT NOT NULL DEFAULT 'foreground'
                        CHECK(added_by IN ('foreground','ambient','pulse')),
            evidence    TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT
        )
    """,
    "session_ledger_shadow_runs": """
        CREATE TABLE IF NOT EXISTS session_ledger_shadow_runs (
            run_id         TEXT PRIMARY KEY,
            session_id     TEXT NOT NULL,
            started_at     TEXT NOT NULL,
            finished_at    TEXT,
            start_byte     INTEGER NOT NULL,
            end_byte       INTEGER NOT NULL,
            trigger        TEXT NOT NULL,
            status         TEXT NOT NULL
                           CHECK(status IN ('ok','failed','timeout','lock_busy','empty_delta')),
            truncated      INTEGER NOT NULL DEFAULT 0,
            n_user_turns   INTEGER NOT NULL DEFAULT 0,
            n_proposals    INTEGER NOT NULL DEFAULT 0,
            latency_ms     INTEGER,
            prompt_version TEXT,
            model          TEXT,
            mode           TEXT NOT NULL DEFAULT 'shadow',
            detail         TEXT
        )
    """,
    "session_ledger_shadow_events": """
        CREATE TABLE IF NOT EXISTS session_ledger_shadow_events (
            id              TEXT PRIMARY KEY,
            run_id          TEXT NOT NULL,
            observed_at     TEXT NOT NULL,
            session_id      TEXT NOT NULL,
            kind            TEXT NOT NULL CHECK(kind IN ('agreement','pivot')),
            text            TEXT NOT NULL,
            turn_ref        TEXT,
            quote_preview   TEXT,
            quote_hash      TEXT,
            quote_verified  INTEGER NOT NULL DEFAULT 0,
            match_kind      TEXT NOT NULL DEFAULT 'none'
                            CHECK(match_kind IN ('exact','fuzzy','none')),
            matched_item_id TEXT,
            match_score     REAL,
            duplicate_of    TEXT,
            mode            TEXT NOT NULL DEFAULT 'shadow'
        )
    """,
    # ── Repo-pulse annotator (session-manager PR-4a) ─────────────────────
    # Merged-PR ↔ open-ledger-item matches from the detached pulse worker.
    # Exact tier (explicit `Ledger: <id>` marker) auto-absorbs via
    # session_charters.ledger_update and records status='applied' here;
    # fuzzy tier (headless Haiku judge) is proposal-only in every mode.
    "repo_pulse_runs": """
        CREATE TABLE IF NOT EXISTS repo_pulse_runs (
            run_id         TEXT PRIMARY KEY,
            started_at     TEXT NOT NULL,
            finished_at    TEXT,
            trigger        TEXT NOT NULL,
            repo           TEXT,
            cursor_before  TEXT,
            cursor_after   TEXT,
            status         TEXT NOT NULL
                           CHECK(status IN ('ok','failed','timeout','lock_busy','no_new_prs')),
            n_prs          INTEGER NOT NULL DEFAULT 0,
            n_open_items   INTEGER NOT NULL DEFAULT 0,
            n_exact        INTEGER NOT NULL DEFAULT 0,
            n_fuzzy        INTEGER NOT NULL DEFAULT 0,
            latency_ms     INTEGER,
            prompt_version TEXT,
            model          TEXT,
            mode           TEXT NOT NULL DEFAULT 'live',
            detail         TEXT
        )
    """,
    "repo_pulse_annotations": """
        CREATE TABLE IF NOT EXISTS repo_pulse_annotations (
            id              TEXT PRIMARY KEY,
            run_id          TEXT NOT NULL,
            observed_at     TEXT NOT NULL,
            tier            TEXT NOT NULL CHECK(tier IN ('exact','fuzzy')),
            item_id         TEXT NOT NULL,
            item_session_id TEXT,
            item_text       TEXT,
            pr_number       INTEGER NOT NULL,
            pr_title        TEXT,
            pr_merged_at    TEXT,
            confidence      REAL,
            rationale       TEXT,
            status          TEXT NOT NULL
                            CHECK(status IN ('applied','proposed','confirmed',
                                             'rejected','superseded')),
            resolved_at     TEXT,
            resolution_ref  TEXT
        )
    """,
    # ── WS-2 sensor fabric (M9/M10) ──────────────────────────────────────
    # Per-run scheduled-job history. job_health is cumulative-only (one row
    # per job_name); this is the era-attribution time series the ledger
    # grades scheduled_job predictions against. Writes are debounced at the
    # source (runtime/_job_health.py): successes only when ≥1h since the last
    # success; failures on streak onset + hourly heartbeat — so a stuck 60s
    # poll costs ~24 rows/day, not ~1440. 90-day self-prune (drip retention).
    "job_run_events": """
        CREATE TABLE IF NOT EXISTS job_run_events (
            id             TEXT PRIMARY KEY,          -- uuid4 hex
            job_name       TEXT NOT NULL,
            status         TEXT NOT NULL CHECK (status IN ('success', 'failed')),
            run_started_at TEXT,                      -- NULL unless record_job_start ran
            duration_ms    INTEGER,                   -- NULL unless run_started_at present
            error          TEXT,                      -- failure detail (NULL on success)
            recorded_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """,
    # Persisted alert/incident store. Replaces the in-memory one-generation
    # _alert_history dict (mcp/health/__init__.py). One designated writer (the
    # awareness tick) reconciles the durable open-set: an open row (resolved_at
    # IS NULL) exists per currently-firing alert_id; resolution stamps
    # resolved_at. The partial UNIQUE INDEX on (alert_id) WHERE resolved_at IS
    # NULL makes INSERT OR IGNORE idempotent even under the cross-process race.
    "alert_events": """
        CREATE TABLE IF NOT EXISTS alert_events (
            id           TEXT PRIMARY KEY,            -- uuid4 hex
            alert_id     TEXT NOT NULL,               -- stable alert key (e.g. 'creds:corrupt')
            source       TEXT NOT NULL,               -- component that raised it
            severity     TEXT NOT NULL,               -- CRITICAL / WARNING / ... (computed)
            message      TEXT NOT NULL,
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at  TEXT                         -- NULL = still open
        )
    """,
    # ── WS-2 Cognitive Ledger (P1a) ──────────────────────────────────────
    # One row per falsifiable prediction ("P about subject S is TRUE by
    # deadline D, probability c"). Three-gate falsifiability: the CRUD writer
    # (db/crud/ledger_predictions.py) validates against the code registry
    # (genesis/ledger/metrics.py); these CHECKs are defense-in-depth against
    # raw-SQL writers; the grader (P2) alarms on registry-vanished metrics.
    # rationale is prose for humans, NEVER graded. The UNIQUE key makes the
    # commit-path writer hooks idempotent under retries/resends.
    "ledger_predictions": """
        CREATE TABLE IF NOT EXISTS ledger_predictions (
            id               TEXT PRIMARY KEY,                     -- uuid4 hex16
            created_at       TEXT NOT NULL DEFAULT (datetime('now')),
            action_class     TEXT NOT NULL CHECK (action_class IN
                               ('outreach_send','task_execution','scheduled_job',
                                'build_verdict','ego_proposal')),
            subject_ref_type TEXT NOT NULL,      -- 'outreach' | 'task' | 'job_day' | ...
            subject_ref_id   TEXT NOT NULL,      -- outreach_history.id / task_id / '<job>:<YYYY-MM-DD>' / ...
            domain           TEXT NOT NULL,      -- dotted coarse domain: 'outreach.<category>', 'task.<type>', 'job.<name>'
            metric           TEXT NOT NULL,      -- MUST exist in the code registry; grader refuses unknown metrics
            comparator       TEXT NOT NULL DEFAULT 'is_true'
                               CHECK (comparator IN ('is_true','le','ge')),
            threshold        REAL,               -- required iff comparator != 'is_true'
            confidence       REAL NOT NULL CHECK (confidence >= 0.01 AND confidence <= 0.99),
            deadline_at      TEXT NOT NULL,      -- ISO-8601 UTC; writer enforces now < deadline <= now + horizon cap
            provenance       TEXT NOT NULL CHECK (provenance IN ('stated','policy_prior')),
            predictor        TEXT NOT NULL,      -- component id: 'outreach_pipeline','task_executor',...
            source_session   TEXT,
            rationale        TEXT,               -- optional prose; NEVER graded
            status           TEXT NOT NULL DEFAULT 'open'
                               CHECK (status IN ('open','resolved','fuzzy_pending',
                                                 'fuzzy_resolved','void','unresolvable')),
            outcome_value    INTEGER CHECK (outcome_value IN (0,1)),
            resolved_at      TEXT,
            resolver         TEXT CHECK (resolver IN ('mechanical','mechanical_absence',
                                                      'llm_fallback','user')),
            evidence_ref     TEXT,               -- 'table:rowid' of the grading evidence
            brier            REAL,               -- (confidence - outcome_value)^2, set at grade time
            metadata         TEXT,               -- JSON
            CHECK ((comparator = 'is_true') = (threshold IS NULL)),
            UNIQUE (action_class, subject_ref_id, metric)
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
    "CREATE INDEX IF NOT EXISTS idx_attention_events_session ON attention_events(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_attention_events_ts ON attention_events(ts)",
    "CREATE INDEX IF NOT EXISTS idx_attention_events_unlabeled ON attention_events(acceptance_signal) WHERE acceptance_signal IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_capability_shadow_events_observed_at ON capability_shadow_events(observed_at)",
    "CREATE INDEX IF NOT EXISTS idx_capability_shadow_events_cell ON capability_shadow_events(cell_domain, cell_verb, cell_risk_class)",
    "CREATE INDEX IF NOT EXISTS idx_immunity_shadow_events_observed_at ON immunity_shadow_events(observed_at)",
    "CREATE INDEX IF NOT EXISTS idx_immunity_shadow_events_gate ON immunity_shadow_events(gate, observed_at)",
    "CREATE INDEX IF NOT EXISTS idx_procedural_task_type ON procedural_memory(task_type)",
    "CREATE INDEX IF NOT EXISTS idx_procedural_draft ON procedural_memory(draft)",
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
    "CREATE INDEX IF NOT EXISTS idx_observations_type_source_created ON observations(type, source, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_observations_surfaced ON observations(surfaced_at)",
    "CREATE INDEX IF NOT EXISTS idx_observations_surfaced_count ON observations(surfaced_count, resolved)",
    "CREATE INDEX IF NOT EXISTS idx_outreach_person ON outreach_history(person_id)",
    "CREATE INDEX IF NOT EXISTS idx_autonomy_person ON autonomy_state(person_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_autonomy_category ON autonomy_state(category)",
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
    "CREATE INDEX IF NOT EXISTS idx_surplus_tasks_type_completed ON surplus_tasks(task_type, status, completed_at DESC)",
    # awareness loop
    "CREATE INDEX IF NOT EXISTS idx_ticks_depth ON awareness_ticks(classified_depth)",
    "CREATE INDEX IF NOT EXISTS idx_ticks_created ON awareness_ticks(created_at)",
    # dead letter
    "CREATE INDEX IF NOT EXISTS idx_dead_letter_status ON dead_letter(status)",
    "CREATE INDEX IF NOT EXISTS idx_dead_letter_provider ON dead_letter(target_provider)",
    "CREATE INDEX IF NOT EXISTS idx_dead_letter_created ON dead_letter(created_at)",
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
    # credential access log (audit trail for reference store credential lookups)
    "CREATE INDEX IF NOT EXISTS idx_credential_access_unit ON credential_access_log(unit_id, accessed_at)",
    # inbox items
    "CREATE INDEX IF NOT EXISTS idx_inbox_items_status ON inbox_items(status)",
    "CREATE INDEX IF NOT EXISTS idx_inbox_items_file_path ON inbox_items(file_path)",
    "CREATE INDEX IF NOT EXISTS idx_inbox_items_batch_id ON inbox_items(batch_id)",
    "CREATE INDEX IF NOT EXISTS idx_inbox_items_drop ON inbox_items(drop_id)",
    # processed emails
    "CREATE INDEX IF NOT EXISTS idx_processed_emails_status ON processed_emails(status)",
    "CREATE INDEX IF NOT EXISTS idx_processed_emails_message_id ON processed_emails(message_id)",
    "CREATE INDEX IF NOT EXISTS idx_processed_emails_content_hash ON processed_emails(content_hash)",
    # deferred work queue
    "CREATE INDEX IF NOT EXISTS idx_deferred_work_status ON deferred_work_queue(status)",
    "CREATE INDEX IF NOT EXISTS idx_deferred_work_priority ON deferred_work_queue(priority)",
    "CREATE INDEX IF NOT EXISTS idx_deferred_work_type ON deferred_work_queue(work_type)",
    "CREATE INDEX IF NOT EXISTS idx_entities_norm ON entities(norm_name)",
    "CREATE INDEX IF NOT EXISTS idx_entity_mentions_entity ON entity_mentions(entity_id)",
    "CREATE INDEX IF NOT EXISTS idx_entity_links_target ON entity_links(target_id)",
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
    # outcome bus (self-improvement ledger)
    "CREATE INDEX IF NOT EXISTS idx_outcome_events_domain ON outcome_events(domain)",
    "CREATE INDEX IF NOT EXISTS idx_outcome_events_source ON outcome_events(source)",
    "CREATE INDEX IF NOT EXISTS idx_outcome_events_tier ON outcome_events(signal_tier)",
    "CREATE INDEX IF NOT EXISTS idx_outcome_events_signal_type ON outcome_events(signal_type)",
    "CREATE INDEX IF NOT EXISTS idx_outcome_events_ref ON outcome_events(ref_type, ref_id)",
    "CREATE INDEX IF NOT EXISTS idx_outcome_events_occurred ON outcome_events(occurred_at)",
    "CREATE INDEX IF NOT EXISTS idx_outcome_events_calibration "
    "ON outcome_events(domain, signal_tier) "
    "WHERE stated_confidence IS NOT NULL AND value IS NOT NULL",
    # ego calibration snapshots (measure-only trend)
    "CREATE INDEX IF NOT EXISTS idx_ego_calibration_domain_time "
    "ON ego_calibration_snapshots(domain, computed_at)",
    # approval requests (Phase 9)
    "CREATE INDEX IF NOT EXISTS idx_approval_status ON approval_requests(status)",
    "CREATE INDEX IF NOT EXISTS idx_approval_class ON approval_requests(action_class)",
    "CREATE INDEX IF NOT EXISTS idx_approval_timeout ON approval_requests(timeout_at)",
    # capability grants (WS-8 — per-(domain,verb,risk_class) cells, dark in PR-B)
    "CREATE INDEX IF NOT EXISTS idx_capability_grants_domain ON capability_grants(domain, state)",
    # WS-8 email gate hold store (drain queries WHERE status='held')
    "CREATE INDEX IF NOT EXISTS idx_pending_email_sends_status ON pending_email_sends(status)",
    # WS-8 PR-D autonomous-send ledger — per-cell rate-limit window + ledger ordering
    "CREATE INDEX IF NOT EXISTS idx_autonomous_email_sends_cell "
    "ON autonomous_email_sends(cell_domain, cell_verb, cell_risk_class, sent_at)",
    "CREATE INDEX IF NOT EXISTS idx_autonomous_email_sends_sent ON autonomous_email_sends(sent_at)",
    # task states (Phase 9)
    "CREATE INDEX IF NOT EXISTS idx_task_states_session ON task_states(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_task_states_phase ON task_states(current_phase)",
    # task steps (task executor)
    "CREATE INDEX IF NOT EXISTS idx_task_steps_status ON task_steps(status)",
    # build candidates (capability-build lane) — partial unique index is the
    # rescan guard: at most one OPEN (undecided) candidate per item_key
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_build_candidates_open_item "
    "ON build_candidates(item_key) WHERE user_decision IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_build_candidates_outcome ON build_candidates(outcome)",
    "CREATE INDEX IF NOT EXISTS idx_build_candidates_created ON build_candidates(created_at)",
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
    "CREATE INDEX IF NOT EXISTS idx_ego_proposals_rank ON ego_proposals(status, rank)",
    "CREATE INDEX IF NOT EXISTS idx_ego_proposals_goal ON ego_proposals(goal_id)",
    # ego directives
    "CREATE INDEX IF NOT EXISTS idx_ego_directives_status ON ego_directives(status)",
    "CREATE INDEX IF NOT EXISTS idx_ego_directives_created ON ego_directives(created_at)",
    # ego intentions
    "CREATE INDEX IF NOT EXISTS idx_ego_intentions_source_status ON ego_intentions(ego_source, status)",
    "CREATE INDEX IF NOT EXISTS idx_ego_intentions_status ON ego_intentions(status)",
    "CREATE INDEX IF NOT EXISTS idx_ego_intentions_created ON ego_intentions(created_at)",
    # intervention journal
    "CREATE INDEX IF NOT EXISTS idx_intervention_journal_status ON intervention_journal(outcome_status)",
    "CREATE INDEX IF NOT EXISTS idx_intervention_journal_proposal ON intervention_journal(proposal_id)",
    "CREATE INDEX IF NOT EXISTS idx_intervention_journal_created ON intervention_journal(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_intervention_journal_source ON intervention_journal(ego_source)",
    # capability map
    "CREATE INDEX IF NOT EXISTS idx_capability_map_confidence ON capability_map(confidence DESC)",
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
    "CREATE INDEX IF NOT EXISTS idx_memory_meta_valid_at ON memory_metadata(valid_at)",
    "CREATE INDEX IF NOT EXISTS idx_memory_meta_invalid_at ON memory_metadata(invalid_at)",
    "CREATE INDEX IF NOT EXISTS idx_memory_meta_deprecated ON memory_metadata(deprecated)",
    "CREATE INDEX IF NOT EXISTS idx_memory_meta_superseded_by ON memory_metadata(superseded_by)",
    # knowledge_units
    "CREATE INDEX IF NOT EXISTS idx_knowledge_units_qdrant_id ON knowledge_units(qdrant_id)",
    # codebase index
    "CREATE INDEX IF NOT EXISTS idx_code_symbols_module ON code_symbols(module_path)",
    "CREATE INDEX IF NOT EXISTS idx_code_symbols_name ON code_symbols(name)",
    "CREATE INDEX IF NOT EXISTS idx_code_symbols_type ON code_symbols(symbol_type)",
    "CREATE INDEX IF NOT EXISTS idx_code_imports_source ON code_imports(source_path)",
    "CREATE INDEX IF NOT EXISTS idx_code_imports_target ON code_imports(target_module)",
    # follow-ups (accountability ledger)
    "CREATE INDEX IF NOT EXISTS idx_follow_ups_status ON follow_ups(status)",
    "CREATE INDEX IF NOT EXISTS idx_follow_ups_scheduled ON follow_ups(scheduled_at)",
    "CREATE INDEX IF NOT EXISTS idx_follow_ups_source ON follow_ups(source)",
    "CREATE INDEX IF NOT EXISTS idx_follow_ups_linked_task ON follow_ups(linked_task_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_follow_ups_dedup ON follow_ups(dedup_key) WHERE dedup_key IS NOT NULL",
    # file modification audit trail
    "CREATE INDEX IF NOT EXISTS idx_file_mod_path ON file_modifications(file_path)",
    "CREATE INDEX IF NOT EXISTS idx_file_mod_session ON file_modifications(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_file_mod_ts ON file_modifications(timestamp)",
    # tool call outcomes (edit failure sensor)
    "CREATE INDEX IF NOT EXISTS idx_tco_tool_ts ON tool_call_outcomes(tool_name, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_tco_success ON tool_call_outcomes(success, timestamp)",
    # cognitive self-modification ledger (rollback)
    "CREATE INDEX IF NOT EXISTS idx_cog_file_mods_target ON cognitive_file_modifications(target_path)",
    "CREATE INDEX IF NOT EXISTS idx_cog_file_mods_actor ON cognitive_file_modifications(actor)",
    "CREATE INDEX IF NOT EXISTS idx_cog_file_mods_created ON cognitive_file_modifications(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_cog_file_mods_status ON cognitive_file_modifications(status)",
    # direct session queue
    "CREATE INDEX IF NOT EXISTS idx_dsq_status_created ON direct_session_queue(status, created_at)",
    # J-9 eval infrastructure
    "CREATE INDEX IF NOT EXISTS idx_eval_events_dimension ON eval_events(dimension, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_eval_events_type ON eval_events(event_type, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_eval_events_session ON eval_events(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_eval_snapshots_period ON eval_snapshots(dimension, period_end)",
    "CREATE INDEX IF NOT EXISTS idx_eval_subsystem_grades_period ON eval_subsystem_grades(subsystem, period_end)",
    # Entity resolution audit
    "CREATE INDEX IF NOT EXISTS idx_er_audit_run ON entity_resolution_audit(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_er_audit_action ON entity_resolution_audit(action, created_at)",
    # Centrality cache
    "CREATE INDEX IF NOT EXISTS idx_centrality_score ON centrality_cache(centrality_score DESC)",
    # SVO event calendar
    "CREATE INDEX IF NOT EXISTS idx_memory_events_memory ON memory_events(memory_id)",
    "CREATE INDEX IF NOT EXISTS idx_memory_events_date ON memory_events(event_date)",
    "CREATE INDEX IF NOT EXISTS idx_memory_events_subject ON memory_events(subject)",
    "CREATE INDEX IF NOT EXISTS idx_memory_events_verb ON memory_events(verb)",
    # user jobs
    "CREATE INDEX IF NOT EXISTS idx_user_jobs_status ON user_jobs(status)",
    "CREATE INDEX IF NOT EXISTS idx_user_jobs_next_run ON user_jobs(next_run_at)",
    "CREATE INDEX IF NOT EXISTS idx_user_job_runs_job ON user_job_runs(job_id, started_at DESC)",
    # reflection corpus
    "CREATE INDEX IF NOT EXISTS idx_corpus_depth ON reflection_corpus(depth)",
    "CREATE INDEX IF NOT EXISTS idx_corpus_quality ON reflection_corpus(quality_label)",
    # campaigns
    "CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status)",
    "CREATE INDEX IF NOT EXISTS idx_campaigns_name ON campaigns(name)",
    "CREATE INDEX IF NOT EXISTS idx_campaign_runs_campaign ON campaign_runs(campaign_id)",
    "CREATE INDEX IF NOT EXISTS idx_campaign_runs_outcome ON campaign_runs(outcome)",
    "CREATE INDEX IF NOT EXISTS idx_campaign_runs_started ON campaign_runs(started_at DESC)",
    # email threads
    "CREATE INDEX IF NOT EXISTS idx_email_threads_message_id ON email_threads(sent_message_id)",
    "CREATE INDEX IF NOT EXISTS idx_email_threads_status ON email_threads(status)",
    "CREATE INDEX IF NOT EXISTS idx_email_threads_follow_up ON email_threads(follow_up_after)",
    "CREATE INDEX IF NOT EXISTS idx_email_thread_messages_thread ON email_thread_messages(thread_id)",
    # otel spans (tracing backbone)
    "CREATE INDEX IF NOT EXISTS idx_otel_spans_trace ON otel_spans(trace_id)",
    "CREATE INDEX IF NOT EXISTS idx_otel_spans_parent ON otel_spans(parent_span_id)",
    "CREATE INDEX IF NOT EXISTS idx_otel_spans_start ON otel_spans(start_unix_us)",
    "CREATE INDEX IF NOT EXISTS idx_otel_spans_session ON otel_spans(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_otel_spans_roots "
    "ON otel_spans(start_unix_us) WHERE parent_span_id IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_session_ledger_session ON session_ledger(session_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_session_charters_updated_at ON session_charters(updated_at)",
    "CREATE INDEX IF NOT EXISTS idx_slsr_session ON session_ledger_shadow_runs(session_id, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_slsr_started ON session_ledger_shadow_runs(started_at)",
    "CREATE INDEX IF NOT EXISTS idx_slse_session ON session_ledger_shadow_events(session_id, observed_at)",
    "CREATE INDEX IF NOT EXISTS idx_slse_observed ON session_ledger_shadow_events(observed_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_rpa_dedupe "
    "ON repo_pulse_annotations(tier, item_id, pr_number)",
    "CREATE INDEX IF NOT EXISTS idx_rpa_status ON repo_pulse_annotations(status, observed_at)",
    "CREATE INDEX IF NOT EXISTS idx_rpa_session ON repo_pulse_annotations(item_session_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_rpr_started ON repo_pulse_runs(started_at)",
    # WS-2 sensor fabric (M9/M10)
    "CREATE INDEX IF NOT EXISTS idx_jre_job_time ON job_run_events(job_name, recorded_at)",
    "CREATE INDEX IF NOT EXISTS idx_jre_recorded ON job_run_events(recorded_at)",
    "CREATE INDEX IF NOT EXISTS idx_jre_status ON job_run_events(status, recorded_at)",
    # one open row per alert_id — makes the open-set reconcile idempotent
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ae_open_alert ON alert_events(alert_id) WHERE resolved_at IS NULL",
    "CREATE INDEX IF NOT EXISTS idx_ae_created ON alert_events(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_ae_alert ON alert_events(alert_id, created_at)",
    # WS-2 Cognitive Ledger (P1a) — the partial index carries the grader's
    # hot query (open rows past deadline)
    "CREATE INDEX IF NOT EXISTS idx_lp_open_deadline ON ledger_predictions(deadline_at) WHERE status = 'open'",
    "CREATE INDEX IF NOT EXISTS idx_lp_domain ON ledger_predictions(domain, action_class, metric)",
    "CREATE INDEX IF NOT EXISTS idx_lp_status ON ledger_predictions(status)",
    "CREATE INDEX IF NOT EXISTS idx_lp_subject ON ledger_predictions(subject_ref_type, subject_ref_id)",
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
    # 2026-04-17: health delta signals moved to Micro-only — they matter when
    # they flip state, not as persistent conditions driving Light reflections.
    ("software_error_spike", "health_mcp", 0.70, 0.70, 0.0, 1.0, '["Micro"]'),
    ("critical_failure", "health_mcp", 0.70, 0.70, 0.0, 1.0, '["Micro"]'),
    ("time_since_last_strategic", "clock", 0.50, 0.50, 0.0, 1.0, '["Strategic"]'),
    ("micro_count_since_light", "awareness_loop", 0.50, 0.50, 0.0, 1.0, '["Light"]'),
    ("cc_version_changed", "awareness_loop", 0.50, 0.50, 0.0, 1.0, '["Micro"]'),
    # 2026-04-17: cascade bridge — Light accumulation triggers Deep
    ("light_count_since_deep", "awareness_loop", 0.50, 0.50, 0.0, 1.0, '["Deep"]'),
    # 2026-04-17: subsystem activity signals — Micro delta detection
    ("sentinel_activity", "sentinel", 0.60, 0.60, 0.0, 1.0, '["Micro"]'),
    ("guardian_activity", "guardian", 0.50, 0.50, 0.0, 1.0, '["Micro"]'),
    ("surplus_activity", "surplus", 0.45, 0.45, 0.0, 1.0, '["Micro"]'),
    ("autonomy_activity", "autonomy", 0.60, 0.60, 0.0, 1.0, '["Micro"]'),
    # 2026-04-17: ghost signal activation — collector already exists, just needs weight
    ("stale_pending_items", "cognitive_state", 0.35, 0.35, 0.0, 1.0, '["Micro"]'),
    # 2026-04-30: user-facing signals for reflection rebalancing (Phase 2.5b)
    ("user_goal_staleness", "follow_ups+user_model", 0.40, 0.40, 0.0, 1.0, '["Light"]'),
    ("user_session_pattern", "cc_sessions", 0.35, 0.35, 0.0, 1.0, '["Light"]'),
]

DEPTH_THRESHOLDS_SEED = [
    # (depth_name, threshold, floor_seconds, ceiling_count, ceiling_window_seconds)
    # Thresholds tuned 2026-03-21: originals (0.5/0.8/0.55) were too conservative,
    # producing only ~12 reflections across 6800 ticks.  Deep lowered to 0.45 to
    # encourage more frequent consolidation (design doc says 48-72h floor).
    ("Micro", 0.50, 1800, 2, 3600),  # floor 30min, max 2/hr
    ("Light", 0.60, 10800, 1, 3600),  # floor 3h, max 1/hr
    ("Deep", 0.45, 172800, 1, 86400),  # floor 48h, max 1/day
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
