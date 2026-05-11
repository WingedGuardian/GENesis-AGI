# Current Build Phase: Phase 8 COMPLETE — Basic Outreach

**Status:** COMPLETE
**Dependencies:** Phase 3 (COMPLETE), Phase 6 (COMPLETE), Phase 7 (COMPLETE)

## What Phase 8 Delivered

### Outreach Pipeline (`genesis.outreach`)

- **Types**: OutreachCategory (blocker/alert/surplus/digest), OutreachStatus, GovernanceVerdict StrEnums + OutreachRequest, GovernanceResult, OutreachResult, FreshEyesResult frozen dataclasses
- **Config**: YAML loader (`config/outreach.yaml`) with quiet hours, channel prefs, thresholds, rate limits, morning report timing, engagement timeout
- **GovernanceGate**: Deterministic (no LLM) pre-send checks — salience threshold, quiet hours, dedup (24h), daily rate limit, surplus quota. Blockers/alerts bypass all checks. Morning reports only check dedup.
- **OutreachPipeline**: governance → fresh-eyes (surplus only) → draft → format → deliver → record. Failed deliveries deferred to DeferredWorkQueue.
- **FreshEyesReview**: Cross-model validation for surplus outreach (call site `23_outreach_review`, renamed from `23_fresh_eyes_review` 2026-05-10). Score 1-5, approved if >= 3.0.
- **MorningReportGenerator**: Assembles HealthDataService snapshot + cognitive state + pending items + engagement summary, drafts via ContentDrafter.
- **EngagementTracker**: Timeout detection (marks as 'ignored' after N hours), reply recording via delivery_id lookup.
- **OutreachScheduler**: APScheduler with 4 jobs — morning report (daily at configured time), surplus outreach (daily 10:00), engagement poll (hourly), calibration reconciliation (daily).
- **Dashboard API**: Flask blueprint at `/api/genesis/outreach/` — queue, engagement summary, surplus approval/rejection, config endpoints.
- **MCP**: 5 stubs replaced with real implementations (outreach_send, outreach_queue, outreach_engagement, outreach_preferences, outreach_digest) + `init_outreach_mcp()` wiring.

### Schema Changes

- `delivery_id TEXT` column on outreach_history (DDL + migration)
- `predictions` table (id, action_id, confidence, confidence_bucket, domain, outcome, correct)
- `calibration_curves` table (domain × confidence_bucket → actual_success_rate, correction_factor)
- `find_by_delivery_id()` CRUD function

### Bayesian Calibration Infrastructure (`genesis.calibration`)

- **PredictionLogger**: Auto-bucketed prediction logging (0.1-width buckets)
- **PredictionReconciler**: Batch job matching unmatched predictions to outreach engagement outcomes
- **CalibrationCurveComputer**: Per domain × confidence bucket accuracy computation + persistence
- **CRUD**: Full predictions + calibration_curves CRUD module

### Signal Collector

- **OutreachEngagementCollector**: Real implementation replacing stub. Queries outreach_history for 7-day engagement ratio (0.0 = all ignored, 1.0 = all engaged).

### Runtime Wiring

- `GenesisRuntime._init_outreach()` (Step 13): Creates all outreach + calibration components, starts scheduler, wires MCP
- `register_channel(name, adapter)` for dynamic channel adapter registration from bridge/terminal
- Real OutreachEngagementCollector wired into `_init_learning()` replacing stub
- Call site 23 (fresh_eyes_review) added to model_routing.yaml

### Tests: ~68 new (1752 cumulative)

## GROUNDWORK Tags

- `GROUNDWORK(perplexity-search)`: PerplexityAdapter stub (from pre-Phase-8)
- `GROUNDWORK(research-synthesis)`: LLM synthesis in ResearchOrchestrator (from pre-Phase-8)

## Next Tracks

- **Phase 9**: Basic Autonomy (depends on Phase 6+8) — UNBLOCKED
- **V4**: Strategic reflection — spec at `docs/plans/v4-strategic-reflection-spec.md`
- **GL-4**: Streaming and live feedback — PLANNED
- **Dashboard Frontend**: Outreach UI, identity audit page, CC chat widget — separate plan

## Completed Go-Live Milestones

- **GL-1**: Background reflections via CC — COMPLETE
- **GL-2**: Terminal conversation (foreground CC) — COMPLETE (2026-03-10)
- **GL-3**: Telegram relay via genesis-bridge.service — COMPLETE (2026-03-10)
- **GL-3.1**: Bridge/terminal wired to GenesisRuntime — all subsystems active during user conversation (2026-03-11)
- **GL-3.2**: Phase 0-7 wiring audit — SurplusScheduler wired into runtime, ConversationCollector real implementation, systemd service file (2026-03-11)
