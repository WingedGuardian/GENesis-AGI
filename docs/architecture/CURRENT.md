# Genesis — Current Architecture (the Subsystem Map)

This is the **canonical judgment-layer map** of Genesis: what each subsystem is
FOR, the mechanisms auditors keep forgetting exist, what is LIVE vs shadow vs
dark, and the do-not-touch edges. It answers "does Genesis have X?" — consult
it FIRST (via the `subsystem-map` skill) before any capability claim, audit, or
competitive comparison. The package-level structural companion is
`.claude/skills/genesis-development/references/codebase-map.md`; the
philosophical "why" is `docs/architecture/genesis-v3-vision.md`.

**How this file stays honest.** Every entry claims its top-level
`src/genesis` modules in a fenced `yaml subsystem-map` block and carries a
`verified: <short-sha> <date>` stamp. CI (`subsystem-map-check`, backed by
`scripts/check_subsystem_map.py`) fails the build if a module is unmapped,
claimed twice, or vanished; stale stamps only warn. After changing a
subsystem's capabilities, update its entry and bump its stamp (PR-template
checkbox).

**Naming trap.** The `capability_map` DB table and `ego/capability_aggregator.py`
are the ego's per-domain *self-confidence model* — completely unrelated to this
document. Everything here is "subsystem map".

Maturity vocabulary: **LIVE** = wired into the runtime path and running;
**shadow** = running but observe/log-only; **dark** = built, no live caller
(usually `# GROUNDWORK(id)` — intentional, never delete as dead code);
**gated** = present but off until an env var / config / user grant enables it.

---

## 1. Memory — retrieval, consolidation, vector store

Persistent hybrid memory: FTS5 + Qdrant episodic/knowledge retrieval on the
read side, extraction and dream-cycle consolidation on the write/maintenance
side.

```yaml subsystem-map
entry: memory
modules: [memory, qdrant]
verified: 36563f95 2026-07-10
```

**Retrieval is TIERED — the hottest auto-fired paths carry the thinnest
stack.** Deep path: `memory/retrieval.py` `HybridRetriever.recall` (bitemporal
`invalid_at` filter, entrenchment, activation/decay, graph boost, diversity
penalty). The diversity penalty only shapes ORDERING —
`RetrievalResult.retrieval_score` carries the pre-penalty score and is what
J-9 quality logging reads (the MCP MEM-003 enrichment reads it too).
Easy-to-forget mechanisms:

- **CRAG** lives in the MCP-wrapper only (`memory/corrective.py`
  `maybe_correct_recall`; `top_score >= 0.75` skips grading) — not in
  `retrieval.py`.
- **Recall is read-MOSTLY, not read-only**: every hit bumps
  `retrieved_count`/`last_retrieved_at` in Qdrant + SQLite (retrieval stage
  11, the MCP drift fallback, `memory_core_facts`). Eval harnesses reading a
  frozen snapshot suppress this via `GENESIS_MEMORY_WRITEBACKS_OFF`
  (`env.memory_writebacks_off()`); any NEW write inside a read path must
  honor the same seam.
- **VoyageReranker** (`memory/reranker.py`, rerank-2.5) exists and is
  API_KEY_VOYAGE-gated; the `rerank=` param is off by default at the retriever
  and applied by callers.
- **`drift_recall`** (`memory/drift.py`) is the degraded-mode fallback; its
  FTS drilldown searches every collection in `source_collections`,
  rank-merged across collections.
- The proactive per-prompt path is `scripts/proactive_memory_hook.py` — an
  **independent reimplementation** (own FTS5→Qdrant→RRF pipeline), not a
  `HybridRetriever` caller. The `memory_proactive` MCP tool is registered but
  has zero internal callers — the hook is the live path.
- `procedure_recall` deliberately uses Jaccard tag-overlap
  (`learning/procedural/matcher.py find_relevant`), not hybrid retrieval.
- External-world recall results are provenance-wrapped (`wrap_external_recall`)
  — first-party memory vs knowledge-base is a load-bearing distinction.
- **Entity layer (WS-H Pillar 2)** — typed entity nodes with identity:
  `entities`/`entity_mentions`/`entity_links` tables (migration 0051),
  `db/crud/entities.py` (recursive-CTE traversal, bi-temporal edge validity,
  EXTRACTED/INFERRED/AMBIGUOUS provenance), `memory/entity_registry.py`
  (string→ID resolution tiering; fuzzy matches queue `entity_adjudication`),
  `memory/entity_seed.py` (curated spine incl. the repo-split rule).
  Distinct from `memory/entity_resolution.py`, which is near-duplicate
  memory-PAIR dedup. Bitemporal timestamps are canonicalized at the write
  gate (`db/timeutil.canonical_iso`, migration 0050).

**Consolidation (dream cycle)** — `memory/dream_cycle.py` (~1480 LOC):
weekly clustering (Sun 4am) persists a value-ranked worklist to
`deferred_work_queue` (`work_type="dream_synthesis_slice"`); a daily drain
(8am) processes a budgeted top-value slice. Destructive merges are gated on
`GENESIS_DREAM_CYCLE_LIVE` (env var, NOT a config key) and the drain is
**shadow-hardwired** (`dry_run=True`) — the live flip is a separate user-gated
change (#892). `_CapacityBreaker` aborts on consecutive provider exhaustion.
`_cross_wing_scan` writes `memory_links` even under dry_run — intentional
additive layer, not a leak.

**Do not touch:** the drain's shadow hardwiring; the dry_run-independent link
write. **Trap:** with no embedding provider registered, memory silently
degrades to FTS5-only (see routing-providers entry).

**origin_class (WS-3 B0):** every store stamps
`owner | first_party | external_untrusted` into the Qdrant payload,
`memory_metadata`, and (KB paths) `knowledge_units` — derived in
`provenance.derive_origin_class` (explicit kwarg wins; external pipelines
outrank `source_subsystem`; `curated` is external BY DECISION — authority
tier, not authorship). Store-time derivation is conservative-first-party for
unknown internal writers; the fail-closed unknown→external rule lives only
at gate time (`security/immunity.py`). Migration 0053 backfilled history
(no owner heuristics); `scripts/backfill_origin_class_qdrant.py` mirrors the
payloads idempotently.

## 2. Execution — CC sessions (DirectSession)

Spawning, tracking, and recovering Claude Code sessions — Genesis's hands for
any task bigger than an LLM call.

```yaml subsystem-map
entry: execution-cc
modules: [cc]
verified: e01e0c49 2026-07-09
```

- `cc/direct_session.py` + `cc/conversation.py` (both >1000 LOC; split
  candidates). Profile machinery: `PROFILES`, `_PROFILE_ADDENDA`,
  `_PROFILE_SKILLS`, `_PROFILE_TO_MCP` (direct_session.py) +
  `session_config._MCP_PROFILES` (profile → MCP-server allowlist).
- **Spawn autonomy circuit breaker** (direct_session.py ~:600-635):
  `bayesian_posterior < 0.15 and total_corrections > 3` blocks non-foreground
  dispatch — flagged for review as a visible lever (Design Principle 3).
- Recovery: `recover_stale_claims` on boot (queue claims); the
  `session_reaper` job on the **learning** scheduler (CronTrigger every 6h
  + a boot-time kick) routes through `SessionManager.cleanup_stale` —
  stale non-foreground 'active' rows → `expired` (outcome unknown),
  end-hooks fired. Known interruptions record `failed`: `_run_session` has
  an explicit `CancelledError` handler, and `GenesisRuntime.shutdown()`
  cancel-and-awaits the runner's in-flight tasks (`DirectSessionRunner
  .shutdown`, 10s grace) BEFORE closing the DB so that handler can persist
  (2026-07-09; the old crud `reap_stale`, which relabeled orphans
  'completed', is deleted). J-9 counts only `completed` as success.
- **Perimeter-session hardening:** `_NO_WEB_TOOLS` / `_NO_OUTREACH_EXTRAS`
  blocklists strip risky tools from perimeter profiles — a security edge, not
  configuration convenience.
- `cc/context_injector.py` (memory→session injection) lives HERE, not in
  memory. GROUNDWORK: `reflection_bridge/_bridge.py` (v4-executor),
  `session_config.py` (hook-inheritance).

## 3. Autonomy & egress gating

Every autonomous action on the outside world funnels through deterministic
in-code gates. Owner-facing delivery (Telegram/voice/email-to-owner) is NEVER
gated — that contract is one-directional.

```yaml subsystem-map
entry: autonomy-egress
modules: [autonomy, outreach, distribution, content, campaigns]
verified: 9037d45b 2026-07-07
```

- **The chokepoint is `outreach/pipeline.py _deliver`** — ~12 send paths
  converge there. `EmailAutonomyGate` (`autonomy/email_gate.py`, WS-8
  capability cells) sits below the LLM tool layer, unbypassable: HOLD writes
  the `approval_requests` row FIRST, then `pending_email_sends`; the
  `email_gate_watcher` job (every 5 min, learning scheduler) drains approved
  sends.
- **Discord is shadow-gated** (`autonomy/shadow_gate.py`): three doors —
  `pipeline._deliver`, `outreach_poll` webhook, discord-bot `send_reply` —
  observe-only into `capability_shadow`, best-effort so it can NEVER break the
  real send. Retention-pruned >45d via `scripts/prune_capability_shadow.py`
  (disk-hygiene), mirroring the immunity shadow store. Enforcement
  (hold-for-approval) is the designed next stage. CI
  backstop: `scripts/check_external_io.py` fails on new ungated egress
  endpoints.
- **`content/egress.py gate()` is LIVE** in the pipeline: anti-slop scrub +
  PII scan for EXTERNAL channels and `content`-category drafts only. Never
  applied to owner channels — don't add them.
- `_NEVER_DISPATCH_ACTION_TYPES` lives in `ego/session.py`, not here.
- **`DistributionManager` is not dead code** — instantiated by
  `modules/content_pipeline`, but its autonomous publish path is
  GROUNDWORK(autonomous-distribution) dark; the live Medium path is the
  `content-publish` CC skill (browser automation).
- **campaigns/** ships infrastructure only — a hard public/private contract:
  campaign names/prompts/targets are USER DATA (DB + private backups), never
  tracked source; zero shipped defaults. `CampaignRunner` cron-ticks
  programmatic prechecks then dispatches DirectSessions; a 120s reaper
  reconciles finished sessions.
- GROUNDWORK across the entry: cross-vendor-review, per-step-verify,
  trace-verify, task-verify (built, dark), outreach-voice,
  autonomous-distribution.

## 4. Scheduling & background work

Genesis's system jobs, surplus-compute usage, and deferred-work accountability.
Note: the *learning* package hosts the other big scheduler (see entry 10).

```yaml subsystem-map
entry: scheduling-background
modules: [surplus, scheduler, follow_ups]
verified: d8d9b5e4 2026-07-09
```

- `surplus/scheduler.py` (~790 LOC) is the system-job hub (dream cycle, recon,
  pipeline cycles, maintenance, code index, model evals…); job bodies
  live in `surplus/jobs/` (gates/runners/dream/gitnexus) and the dispatch
  pipeline in `surplus/dispatch.py`, with the scheduler keeping every method
  name as a thin delegate/facade. (The long-disabled `schedule_code_audit`
  job was removed 2026-07; `CodeAuditExecutor` + the CODE_AUDIT task type
  remain for dispatch/judge consumers and manual enqueue.)
  `dispatch_once()` is **idle-gated** — surplus tasks only run when idle;
  follow-up dispatch is deliberately NOT idle-gated.
- **Durability model:** no persistent jobstore — jobs are re-registered at
  every boot + CronTrigger + `misfire_grace_time`, backed by three durable DB
  queues (`surplus_tasks`, `dead_letter`, `deferred_work_queue`).
  **IntervalTrigger resets on restart** — anything >1h must be a CronTrigger
  (documented bug class). Boot sweeps reclaim orphans immediately: the
  surplus scheduler resets `running` rows at start() without burning
  attempt_count (restart ≠ task failure), and the learning init kicks the
  recovery orchestrator at boot. Both assume SINGLE-WORKER dispatch —
  re-gate on worker ownership if v4-parallel-dispatch ships.
- **`surplus/intake.py`** (intelligence intake: atomize → score → route)
  auto-ingests curated sources into the knowledge base with NO manifest gate —
  an INTENTIONAL bypass of the conversational confirm-first path; don't "fix"
  it.
- **`scheduler/` (top-level package) is the UserJobScheduler** — user-authored
  cron jobs (via MCP) that dispatch background DirectSessions. Distinct from
  `surplus/scheduler.py`.
- `follow_ups/` = accountability ledger + dispatcher (every 5 min) that turns
  follow-ups into surplus tasks; retention sweep on the learning scheduler.
- GROUNDWORK: v4-parallel-dispatch, v4-surplus-tasks, v4-rate-tracking.

## 5. Information intake & research

Everything that pulls outside information IN: knowledge ingestion, the inbox
drop folder, web search/fetch, recon jobs, and the research pipeline.

```yaml subsystem-map
entry: intake-research
modules: [knowledge, inbox, research, recon, web, pipeline]
verified: 9037d45b 2026-07-07
```

- **knowledge/**: orchestrator + manifest + tree index. Content-hash gate
  (`has_unchanged_source`) makes re-ingest of changed sources re-distill;
  `remove_unit` tombstones a source when its last unit is deleted (invariant:
  only that method may tombstone). The conversational path
  (`knowledge_ingest_source` MCP) requires explicit user confirmation —
  contrast the intake bypass in entry 4.
- **inbox/**: file-drop monitor with approval-gated dispatch; phase order
  resume → detect → create → dispatch; `approval_key_stable=True` (ONE
  site-level approval key). The refresh path folds parked files into the
  batch so approvals fire once (#914). Coherence + URL-failure heuristics gate
  dispatch.
- **recon/**: scheduled intelligence jobs (release watch, model intelligence
  Sun 8am, models.md synthesis Sun 10am, GitHub discovery, skill-security scan
  via external NVIDIA SkillSpector). Emits findings for triage
  (`recon_findings`/`recon_triage`) — intelligence-only, never auto-acts.
- **web/**: stateless search (SearXNG primary, Brave fallback) + httpx fetch
  (50k-char cap), sanitizer-wrapped; consumed via importers (MCP web tools,
  research, recon, pipeline), not runtime init.
- **research/**: `ResearchOrchestrator` over the provider registry — read-only
  capability, no egress gate needed.
- **pipeline/**: tiered research collection → triage → elevation feeding
  capability modules (crypto/prediction); pause-guarded. It is research
  plumbing, NOT the cognitive pipeline.
- GROUNDWORK: vision-ocr (image processor).

## 6. Channels & interfaces

Every surface a human (or host process) talks to Genesis through.

```yaml subsystem-map
entry: channels-interfaces
modules: [channels, dashboard, mcp, hosting, browser, mail]
verified: 0eb21377 2026-07-10
```

- **channels/**: adapter framework. Telegram (`bridge.py` =
  `genesis-bridge.service`, boots a full runtime — LEGACY FALLBACK ONLY:
  it yields at startup, exit 200, when the genesis-server process lock is
  held, because two runtimes dual-poll getUpdates and both write
  status.json; the server hosts the same adapter via
  `hosting/standalone.py`. The polling stall watchdog records liveness
  from successful EMPTY getUpdates round trips too —
  `LivenessHTTPXRequest` — so idle chat is not a stall); voice (HA,
  OUTBOUND-only — inbound voice arrives via `dashboard/routes/voice_api.py`;
  uses `media_player.play_media`, never `assist_satellite.announce` which
  reopens the mic); Discord webhook; email SMTP. All env-gated. "OpenClaw" here
  is only the MIT origin of the Telegram transport code. Device/edge-side voice
  software (firmware, esphome, S2S/ambient bridges, edge deploy) lives in the
  separate `GENesis-Voice` repo — `channels/voice/` here is only the in-runtime
  channel.
- **dashboard/**: Flask blueprint at `/genesis` (~45 route modules);
  `_async_route` bridges sync Flask onto the runtime event loop; heartbeat
  thread detects degraded-but-alive Flask; web terminal.
- **mcp/**: 5 Genesis MCP servers (health, memory, outreach, recon,
  discord-bot) + external codebase-memory; profile→server allowlist lives in
  `cc/session_config._MCP_PROFILES`. `genesis-health` is the big one (~35 tool
  modules). `standalone_health.py` serves from `~/.genesis/status.json` when no
  live runtime (stale-but-functional).
- **hosting/**: the OUTER layer that calls the runtime. `standalone.py` is the
  default (`python -m genesis serve`; also hosts the OpenClaw
  `/v1/chat/completions` endpoint); Agent Zero adapter optional.
- **browser/**: profile/state layer only (persistent
  `~/.genesis/browser-profile`, `BrowserLayer` enum, pgrep patterns as the
  single source of process detection). The automation TOOLS live in
  `mcp/health/browser.py`.
- **mail/**: Gmail IMAP recon (weekly two-layer monitor: cheap-LLM briefs →
  CC judge, sanitizer-wrapped) + reply poller (4h) + `ReplyHandler` dispatching
  restricted `mail`-profile sessions. Sending is NOT here — all sends go
  through the outreach gate (entry 3). Trap: never default a recipient to the
  agent's own address (self-send loop).
- GROUNDWORK: unified-bridge, outreach-pipeline (channels/base.py),
  guardian-dialogue (dashboard health route).

## 7. Ego & self-model

The two autonomous decision-making egos and the identity documents that shape
them.

```yaml subsystem-map
entry: ego-self-model
modules: [ego, identity, deliberation]
verified: 9037d45b 2026-07-07
```

- **Two egos, both LIVE**: user ego (CEO, Opus, MCP profile `user_reflection`)
  and Genesis ego (COO, Sonnet, profile `reflection`), sharing `EgoSession`
  (~108K). `EgoCadenceManager`: adaptive proactive cycles, morning-report cron,
  30-min mechanical sweep, goal-staleness scans. Review cadence + budget
  controls before adding call sites.
- **`capability_aggregator.py` → `capability_map` table** = per-domain
  self-confidence from up to 6 sources (inverse-confidence weighted; the
  Outcome-Bus feed is flag-gated OFF). This is the naming-trap twin of this
  document — unrelated to the subsystem map.
- Proposal pipeline (`proposals.py`): batch WHAT/WHY/HOW digests to Telegram,
  content firewall via `validate_batch()`, 6h digest rate-limit GROUNDWORK;
  `_NEVER_DISPATCH_ACTION_TYPES` blocklist lives in `session.py`. Dispatches
  record `follow_ups` rows for accountability. `integrity.py` chain-verify is
  GROUNDWORK, explicitly NOT wired.
- **identity/**: SOUL/USER/VOICE/STEERING CAPS-markdown + `IdentityLoader`
  (wired via perception). `cc/session_config` reads SOUL+VOICE directly, not
  via the loader. **USER.md auto-synthesis is PERMANENTLY DISABLED** — the
  evolver writes system-owned `USER_KNOWLEDGE.md` instead, ledger-tracked.
- **deliberation/**: `deliberate()` multi-model panel with explicit dissent —
  reachable ONLY via the `deliberate` MCP tool, recursion-blocked,
  never-raises. On-demand, not a default judgment path.

## 8. Guardian & sentinel — infrastructure self-healing

Two complementary watchdogs: the host-VM Guardian (outside the container blast
radius) and the container-side Sentinel (CC-driven diagnosis/repair).

```yaml subsystem-map
entry: guardian-sentinel
modules: [guardian, sentinel]
verified: 4e483381 2026-07-15
```

- **guardian/** is bidirectional: host side (`python -m genesis.guardian`,
  systemd timer; `check.py` runs 5 parallel probes → 6-state machine → act;
  Proxmox disk/RAM provisioning verbs) and container side (`watchdog.py`
  monitors the host Guardian every awareness tick, incl. git-SHA code-drift
  detection). Config `~/.genesis/guardian_remote.yaml`; missing → silently
  disabled.
- **Merged ≠ deployed**: guardian code reaches the host ONLY via
  `scripts/update.sh` / `guardian-gateway.sh` (the host-deploy gate in the dev
  skill). Known wart: the watchdog's stale-alert wording inverts when the
  deployed script is NEWER than the host checkout.
- Provisioning verbs are EXECUTE-ONLY — approval is the CALLER's
  responsibility (container obtains it via Telegram before invoking).
- Read-only `host-profile` verb (`guardian/host_profile.py`) feeds the
  `infra_profile` host plane; the CC diagnosis prompt inlines the shared-mount
  `INFRASTRUCTURE.md` (truncated) so the diagnostician starts with the body
  schema instead of re-deriving the machine's shape.
- **Out-of-band tiered alerts** run every tick through the guardian's OWN
  Telegram (survives a dead/thrashing container): storage-pool data%/metadata%
  (`pool.py`) and **RAM** (`memory_watch.py`, E-rest) — the latter over two
  axes worst-of, container cgroup (via incus-exec, best-effort) + host-VM
  `/proc/meminfo` (the reliable axis). Both use the shared `_tier_for`/
  `decide_alert` hysteresis. Read-only `disk-status`/`ram-status` verbs expose
  the same measurement to the container.
- **Container-swap invariant reconciler** (`swap_watch.py`) runs every tick:
  re-asserts `limits.memory.swap=true` (incus config) and live-activates the
  cgroup `memory.swap.max` (via `cgroup_ops`) when observed at `0` — the
  self-heal for installs that advance via bare `git pull` and never re-run
  host-setup. Heals page INFO; failures page WARNING (24h throttle); kill
  switch `swap_reconcile_enabled: false`.
- **sentinel/** is LIVE-wired but **shadow-only autonomy**: config mode
  `"live"` is NOT implemented (dispatcher warns + downgrades); every proposed
  action requires human approval. `InfrastructureMonitor` (call site 37, free
  models) observes each awareness tick and wakes the dispatcher; state persists
  to `~/.genesis/sentinel_state.json`.
- GROUNDWORK: guardian-cgroup, guardian-bidirectional, sentinel-live-autonomy.

## 9. Ambient cognition — heartbeat, reflection, attention

The loops that make Genesis think between conversations.

```yaml subsystem-map
entry: ambient-cognition
modules: [awareness, perception, reflection, attention, session_awareness,
          session_charter.py]
verified: 47e7a132 2026-07-15
```

- **awareness/**: the 5-min heartbeat. ~23 signal collectors (the richer
  `learning/signals/*` set REPLACES the bootstrap placeholders in
  `signals.py` — those stubs are GROUNDWORK(signal-bootstrap), not the live
  collectors). Tick → depth classification (MICRO/LIGHT/DEEP/STRATEGIC) →
  reflection dispatch. Also per-tick `_check_*` housekeeping: CC-slot RSS leak
  watch, subscription-cap detection, SQLite WAL hygiene, resilience-axis folds,
  liveness heartbeat, duplicate-CC-executor paging (two live `claude` processes
  on ONE transcript — hook layer `scripts/hooks/duplicate_session_guard.py`
  writes `~/.genesis/session-owners/*.conflict`, this check pages `critical`
  and GCs stale registry files; newest executor wins, older one's repo-mutating
  tools are denied), and (hourly) embedding-backlog degradation — counts
  `memory_metadata.embedding_status='failed'` (permanently keyword-only rows the
  rate alert misses), hybrid `high` (dashboard) / `critical` (Telegram) by band —
  plus (hourly) deploy staleness: merged-vs-deployed drift (update.sh age,
  commits behind from local refs, missing systemd units, host-guardian
  deployed_commit via `~/.genesis/host_gateway_state.json`; collectors in
  `observability/snapshots/deploy_health.py`), `high` on any drift, `critical`
  only sustained (≥7d AND ≥20 commits, or a missing unit alerted >24h).
  Also per-tick (WS-2 M10) the SINGLE designated `alert_events` writer:
  `_persist_health_alerts` recomputes the firing set via the pure
  `mcp/health/errors.py::_compute_alerts()` and reconciles a durable open-set
  (open row per firing alert, `resolved_at` stamped on clear) — replacing the
  in-memory, per-process, one-generation `_alert_history` dict so incident
  history survives restart. It does NOT drive the ego cadence (ego has its own
  scheduler). Trap: PEP 562 lazy `__init__` — don't eager-import `loop.py`.
- **scheduled-job telemetry (WS-2 M9)**: `runtime/_job_health.py` keeps the
  cumulative `job_health` row AND now appends per-run `job_run_events` (era
  attribution the cumulative row can't give). Writes are debounced off the
  persisted `job_health` anchors — a success only when ≥1h since the last, a
  failure on streak onset + hourly heartbeat — so a stuck sub-hourly poll costs
  ~24 rows/day. `duration_ms` is honest-or-NULL (only from an explicit
  `record_job_start` marker; never derived from `last_run`). 90-day prune via
  `_wire_drip_retention_jobs`.
- **perception/**: the real-time reflection engine — MICRO (and LIGHT without
  a CC bridge) run in-process via the router; DEEP/STRATEGIC go to the CC
  reflection bridge. GROUNDWORK: user-model-synthesis, pre-execution-gate
  (template exists, gate not live).
- **reflection/**: the deep/scheduled path (self-assessment, quality
  calibration, learning-stability). **Cadence trap:** jobs FIRE DAILY but an
  idempotency gate holds each to ≤1 SUCCESS per week — a failed day retries
  tomorrow, not next week.
- **attention/**: Track-1 ambient attention — SHADOW, not in runtime init;
  runs via offline CLI over pulled snapshots. The 6-module core is pure and
  edge-portable (no wall clock, no I/O, no genesis deps — test-enforced);
  `sampler.py` (L1.5 judge) is the only LLM caller, outside the core.
  **Firewall: transcript text is never persisted** — only refs + derived
  features reach `attention_events`. Config is versioned DATA
  (`~/.genesis/config/attention_config.json`).
- **session_awareness/**: WS-C ambient session-theme layer — SHADOW.
  The proactive memory hook folds each genuine user prompt's embedding
  into a per-session EMA + entity ledger
  (`~/.genesis/sessions/<id>/session_theme.json`); on a drift-trigger
  fire it spawns the detached worker (2-slot flock semaphore), which
  retrieves+ranks candidates over four lanes — vector, decisions
  (`tags~decision`, the OMI-incident class), entity-keyword drift, and
  the **entity lane** (ledger keywords → entity nodes → ≤2 typed hops →
  mentions; `ranking.ENTITY_LANE_MODE`, SHADOW — entity-only hits ride
  `entity_shadow` telemetry, never candidates, until the OMI-replay-
  gated flip) — all Qdrant lanes EXACT search (filtered HNSW without
  payload indexes drops valid results; found 2026-07-09). Headless-
  Haiku arbiter judges candidates per fire (fail-closed parse, group-
  kill on timeout). Verdicts → `ambient_verdict.json`, tuning →
  size-capped shadow log; each arbiter attempt (incl. pre-spawn
  failures, success=0 with reason) also records a `call_site_last_run`
  row (`ambient_arbiter`, neural monitor) via its own short-lived RW
  connection. **Zero memory-row writes — never bumps
  retrieved_count** (retrieval connection is mode=ro; protects
  MEM-005/H-1 baselines). Fail-open at the hook boundary. Kill switch:
  `GENESIS_SESSION_AWARENESS_DISABLED=1`.
- **Session charter + ledger** (session-manager stages 1-2): the
  `session_charters` + `session_ledger` DB tables (migration 0058) are the
  canonical store; `~/.genesis/sessions/<sid>/charter.md` is the regenerated
  human mirror (pre-0058 `charter.json` files are a legacy read-fallback,
  imported once by `scripts/backfill_session_charters.py`).
  `scripts/genesis_precompact.py` (PreCompact hook, both triggers, 5s
  fail-open timeout, stdlib-only sqlite3, BEGIN IMMEDIATE) persists a
  foreground session's IMMUTABLE origin — the first typed user prompt,
  extracted from the transcript head at the FIRST compaction boundary — and
  bumps `compaction_count` thereafter (+ `waypoints.jsonl` deterministic
  spine). `origin_prompt`/`origin_ts` are write-once (filled only WHERE
  origin_prompt IS NULL); `mission`/`pointers`/ledger rows are living fields
  owned by the `session_charter*`/`session_ledger*` MCP tools on
  genesis-health (`mcp/health/session_charter_tools.py`), which may create a
  stub row before the first compaction. Read paths:
  `genesis_session_context.py` re-injects origin + open ledger on every
  startup/resume/compact (NOT clear), and `genesis_urgent_alerts.py` emits a
  per-turn `[Charter: <mission> | open: N]` drift tag (both mode=ro,
  fail-open). Ledger statuses: open/in_progress/done/absorbed/dropped —
  `absorbed` + `evidence` is the repo-pulse (PR-4) seam. Dispatched sessions
  (GENESIS_CC_SESSION=1) are skipped — task_states is their continuity spine.
- **Ambient ledger extractor** (session-manager stage 3) — **SHADOW**. At
  each PreCompact snapshot the hook fire-and-forgets
  `scripts/ledger_shadow_worker.py` (`--end-byte` stat'd at the boundary);
  the detached worker (`session_awareness/ledger_worker.py`) reads the
  transcript delta since its own cursor
  (`~/.genesis/sessions/<sid>/ledger_shadow_cursor.json`, advanced ONLY on
  recorded ok/empty_delta — failures re-cover their window), extracts
  agreements/pivots via headless Haiku (`ledger_extractor.py`: DATA-framed
  prompt, fail-closed parse, verbatim-quote verification), matches against
  the live ledger (exact hash + SequenceMatcher ≥0.85 — the precision
  signal) and prior shadow events (`duplicate_of`), and records rows to
  `session_ledger_shadow_runs`/`_events` (migration 0059) — **the live
  `session_ledger` is NEVER written until the data-gated flip PR**. Shared
  subprocess core with the arbiter: `session_awareness/headless.py`;
  canonical typed-prompt filter `session_awareness/transcript.py` (the
  PreCompact hook keeps a parity-tested stdlib duplicate; honors
  `promptSource` typed/queued, excludes bare slash-commands + markers).
  Levers: settings domain `session_ledger_shadow` (off|shadow; `live`
  reserved, coerced+warn) read at worker startup;
  `GENESIS_LEDGER_SHADOW_DISABLED=1` hook-level kill. Per-session flock;
  `--backfill` replays historical transcripts in typed-turn windows
  (`trigger='backfill'`, cursor untouched). Measurement:
  `scripts/ledger_shadow_report.py` (recomputed precision, FP adjudication,
  FN windowing, leak invariant); retention 45d via
  `scripts/prune_ledger_shadow.py` (disk-hygiene step 8). Telemetry:
  `call_site_last_run` row `ambient_ledger_extractor` (deliberately not a
  critical site).

## 10. Learning & evaluation

Self-improvement loops and the instrumentation that keeps them honest.

```yaml subsystem-map
entry: learning-evaluation
modules: [learning, eval, experimentation, feedback, calibration]
verified: fe5d0945 2026-07-10
```

- **learning/** is the de-facto cron host: `rt._learning_scheduler` registers
  ~20+ jobs well beyond learning (recovery orchestrator, reapers, email-gate
  drain, retention sweeps, plus the eval/feedback jobs below). CronTrigger
  discipline is load-bearing here. Loops that actually run: triage pipeline,
  procedural extraction (extract → judge → promote hourly, novelty +
  contradiction gates), weekly skill evolution, daily triage calibration.
  `tool_discovery.py` static maps are deprecated (GROUNDWORK
  provider-migration) — use ProviderRegistry.
- **eval/**: J9 = fire-and-forget emit hooks on live cognitive paths (the
  "cannot break production" contract — hooks must never raise) + weekly Sunday
  aggregation (hard 7-day window) + an on-demand batch judge as a surplus
  task. The model gauntlet is weekly but OFF by default (paid inference) and
  NEVER auto-mutates the roster. Ten snapshot dimensions: the original five
  (memory/system/ego/cognitive/procedure) + cognitive_drift + three
  snapshot-only WS-1 A2 series (approvals gate-throughput, goals scaffold,
  noise/passivity) + `dev_quality` (findings-per-PR by severity, code-audit
  gauge, edit-failure flow; fed by the `pr_review_harvest` job — Sun 06:45
  user-tz, 45 min BEFORE the 07:30 aggregation, deliberately a separate job
  so a gh outage degrades to stale rows instead of silently nulling the
  dimension; the harvester reads INLINE PR comments via
  `gh api pulls/N/comments` — `gh pr view --json reviews,comments` misses
  bot findings). `run_weekly_aggregation` swallows per-dimension failures
  silently — the registration test in `tests/test_eval/test_j9_eval.py` is
  the guard; keep it in sync when adding a dimension. Resolver-origin
  classification for the approvals series is canonical in
  `db/crud/approval_requests.py classify_resolver` (free-text convention —
  a new `resolved_by` writer must extend the prefix tuples + drift test).
- **eval/bench/** (`genesis eval bench`, WS-1 A3): Genesis-vs-bare-Claude
  paired A/B on real tasks. FOUR A/B-ish surfaces now exist — don't conflate:
  `eval bench` (CC-arm task A/B), `eval benchmark` (provider×dataset table),
  the gauntlet (roster agentic fix-loop), Crucible/Evo (in-process prompt
  A/B). Bench isolation contract: Genesis arm = fresh no-callback CCInvoker +
  genesis-memory MCP env-redirected to a WAL-safe DB snapshot
  (`GENESIS_DB_PATH` + `GENESIS_MEMORY_WRITEBACKS_OFF` — recall is
  read-MOSTLY: without the seam it bumps retrieved_count in shared prod
  Qdrant); bare arm = `--safe-mode` (the only OAuth-compatible CLAUDE.md
  suppression; `--bare` refuses OAuth) + cleanroom CLAUDE_CONFIG_DIR.
  Task set is PRIVATE (`~/.genesis/eval/bench_tasks_v1.jsonl`; loader
  refuses in-repo paths). Judge = `bench_task_success` rubric, UNCALIBRATED
  v1 (every surface stamps `judge_calibrated: false`). Every new
  genesis-memory tool must be classified in `eval/bench/arms.py`
  (static-AST forcing test). Don't run across Sun 07:30 (J-9 aggregation).
  **A5 read surface** (WS-1 A5): the persisted paired win-rate is readable via
  `/api/genesis/eval/bench` (a compact result card in the dashboard internals
  tab) and the `bench_status` MCP tool — both aggregate-only (never per-task
  private text), shaped by the shared `eval/bench/surface.py`, filtered on
  `model_profile='bench:genesis'` (the genesis row's `metadata_json.stats` is
  self-contained), and stamped with the uncalibrated-judge + `insufficient_data`
  caveat. A stats-less/all-skip run surfaces flagged, never crashes.
- **experimentation/**: Crucible A/B + Evo fan-out — on-demand via MCP tools
  only; **recommend-only is the safety invariant** (no autonomous promotion,
  no live-cognition writes; Bonferroni + held-out re-validation).
  `standalone_router.StandaloneLiteLLMRouter` is the ONE offline Router shim
  (calibration + bench both use it — don't add inline copies).
- **feedback/**: the Outcome Bus (`outcome_events`) — **write-path LIVE
  (harvest 8:45/20:45), read-path DARK** (nothing consumes the ledger yet).
  Tier taxonomy is load-bearing: Tier-1 ground truth outranks user approval.
  `record_outcome` must never raise. Deliberately "observation, not
  reinforcement" — don't rename toward RL.
- **calibration/**: Bayesian prediction-calibration primitives, currently
  wired via outreach (engagement reconciliation). **Four distinct
  "calibration" surfaces exist** (this package, `learning/triage/calibration`,
  `feedback/calibration` ego-ECE, `eval/calibration` golden-set loader) —
  don't conflate.

## 11. Routing & providers

How every LLM call picks a provider, and the registry for non-LLM tools.

```yaml subsystem-map
entry: routing-providers
modules: [routing, providers]
verified: 9037d45b 2026-07-07
```

- **routing/**: `config/model_routing.yaml` defines ~54 numbered call sites,
  each a free-first → paid-last chain; `never_pays` sites are filtered to
  free-only. Per-provider circuit breaker (3 failures, exponential backoff
  capped 30 min — 4h for QUOTA_EXHAUSTED; 429 = backpressure, NOT a breaker
  failure; state persisted cross-process to
  `~/.genesis/circuit_breaker_state.json`). Degradation levels are
  hand-curated: L2 sheds nice-to-haves; **L3 keeps ONLY micro-reflection,
  embeddings, tagging** — changing those sets changes what survives an outage.
  Some call sites alias another site's chain — don't assume 1:1.
- **providers/**: the `ToolProvider` registry for NON-LLM tools (search,
  embeddings, STT/TTS, crawl, probes). Adapters register GATED ON ENV KEYS —
  silent non-registration is by design (absence ≠ bug). LLM breaker/health
  logic lives in routing, not here. No embedding provider registered → memory
  silently degrades to FTS5-only.
- GROUNDWORK: gpt-oss-120b provider defined but unwired into any chain.

## 12. Platform & data

The load-bearing floor: database, runtime bootstrap, resilience, observability,
config resolution, and hygiene utilities.

```yaml subsystem-map
entry: platform-data
modules: [db, runtime, resilience, observability, security, codebase,
          restore, util, infra_profile, env.py, _config_overlay.py]
verified: 95dee055 2026-07-15
```

- **db/**: aiosqlite WAL behind `SerializedConnection` (an asyncio.Lock —
  without it interleaved commits pin `in_transaction` until restart). Two
  schema paths coexist: base DDL (`schema/_tables.py`, ~113 CREATE TABLE; docs
  still say "60+") plus versioned `migrations/` 0001..0060 run ONCE at startup
  before any other init step touches data; a failed migration ABORTS bootstrap.
  EVERY table must be in BOTH paths (fresh-install DDL + its numbered
  migration) — the `test_db/test_schema.py` allow-list enforces it. Migration
  atomicity is hand-rolled (BEGIN IMMEDIATE + a proxy that blocks stray
  commits/DDL autocommit) with a post-commit reconcile and SQLITE_LOCKED
  retry (2026-06-25 incident guard). No TABLES-vs-sqlite_master parity test
  exists.
  **DATA migrations (WS-C, `db/data_migrations/`) are the OPPOSITE contract:**
  non-schema backfills (Qdrant payloads, entity graphs) that run POST-boot as a
  background `tracked_task` (kicked from `runtime/_core`), never abort boot, are
  idempotent, and are claimed atomically via the `data_migrations` ledger (so
  server + bridge-fallback can't double-run). `dNNNN_*.py` modules expose sync
  `migrate()`+`verify()` (runner offloads via `to_thread`); `requires_operator`
  ones sit `operator_pending` and never auto-run. Shared file-discovery with the
  schema runner (`db/_migration_discovery.py`), deliberately NOT the atomic-txn
  proxy. Seed `d0001` mirrors SQLite `origin_class` onto Qdrant — idempotent, so
  a lagging install self-heals on next pull+restart with no control plane.
- **runtime/**: sequential bootstrap (secrets → db → … → sentinel, ~27 steps);
  each step records ok/degraded/failed in the manifest — only db aborts.
  `~/.genesis/capabilities.json` + `bootstrap_manifest.json` are projected at
  bootstrap tail; readonly probes must never clobber the primary's state.
  New capabilities need `_CAPABILITY_DESCRIPTIONS` registration. Autonomy init
  installs a fail-closed `DenyHighRiskSentinel` FIRST so ctor failures degrade
  to blocking. GROUNDWORK: task-verify (constructed, `.verify()` never called
  — dark), web-dd.
- **resilience/**: RecoveryOrchestrator on a 30-min interval (3 confirmation
  probes before draining); `DeferredWorkQueue` priorities + staleness policies;
  dead-letter replay. The `dream_synthesis_slice` worklist is deliberately
  excluded from the backlog alarm (drift-guard test pins it).
- **observability/**: event bus dispatches inline AND logs every event;
  persist-queue overflow drops events but emits a rate-limited "dropped"
  meta-event (WS-17). Two health layers (async probes vs systemd shell-out);
  `/health` is a dashboard route, not an MCP tool; `job_health` state machine
  is runtime-owned. `snapshots/deploy_health.py` = merged-vs-deployed drift
  (never does network I/O; host guardian state comes from
  `~/.genesis/host_gateway_state.json`, written by `cc_align_host_sync` on
  every gateway version probe — update.sh and the nightly cc-align timer);
  its `GUARDIAN_HOST_PATHS` must stay in LOCKSTEP with update.sh
  GUARDIAN_PATHS.
- **security/**: prompt-injection defense + outbound scanning — sanitizer is
  LOG-ONLY for internal sources (perimeter EMAIL/INBOX can block);
  `output_scanner` = deterministic outbound secrets/IP scan; `skill_scan`
  shells to external NVIDIA SkillSpector. NOT auth or secrets storage (that's
  `runtime/init/secrets.py` + `env.py`). **`immunity.py` = the WS-3 kill
  switch + gate policy**: `gate_mode()` re-reads `config/ws3_immunity.yaml` +
  its `.local.yaml` overlay per call (no cache, no restart — the
  `ws3_immunity` settings domain is writable); master `enabled: false`
  short-circuits every gate; `is_blockable()` is the never-block-owner/
  first-party invariant every gate routes through; the gate-time fail-closed
  unknown→external rule lives ONLY in `effective_origin_class()` (store-time
  derivation never fail-closes). Auto-demote state is written INTO the overlay
  so state and behavior share one file. **B1: gate 4 (injection) is LIVE in
  SHADOW** — `immunity_shadow.py` records a would-block into
  `immunity_shadow_events` (migration 0055) at all 8 `wrap_external_recall`
  inject sites + the proactive hook whenever `external_untrusted` content
  reaches an action-capable prompt (observe-only — the item still reaches the
  model; owner/first-party never recorded). The gate set is CI-locked in
  `test_recall_inject_coverage.py` (a new inject site or a removed emit fails).
  **Gate 1 (procedure) is LIVE in SHADOW** — `record_would_block(gate="procedure")`
  fires at the two promotion paths that have a trustworthy SOURCE-origin signal:
  the judge convergence (`judge._store_judged_procedure`, covering BOTH the
  struggle and rebuild callers) classifies by a coarse tool-name ingest scan over
  the real transcript spine (`provenance.origin_from_tool_names` — external-ingest
  tool → `external_untrusted`; over-observes by design since fetched content lives
  in tool RESULTS the spine doesn't carry); the autonomy retrospective
  (`executor/trace.py`) classifies by `initiated_by` (Genesis's own execution =
  first_party/owner; the trace has no source-tool spine). Two promotion paths are
  DEFERRED (classified `deferred-with-reason`, no emit): the deprecated
  auto-extractor (`extractor.py` — its only signals are replay tools or a
  hyphen-truncating prose scrape, both undercount) and `procedure_store` (an MCP
  tool needing the caller's session origin — the session-origin PR's env; it
  wires that emit). CI-locked in `test_procedure_gate_coverage.py`.
  **Gates 2-3 (identity/autonomy) are LIVE in SHADOW.** Gate 2: the steering
  write (`learning/pipeline.py`) emits with a CHANNEL allow-map origin
  (`_CHANNEL_ORIGIN`: terminal/telegram/whatsapp/web = owner; voice + unknown
  channels fail CLOSED to external_untrusted — the polarity fix for the
  fail-open `_AUTONOMOUS_CHANNELS` deny-list, so a deny-list escape is now
  OBSERVED), and the USER_KNOWLEDGE synthesis (`runtime/init/learning.py`)
  emits first_party-by-authorship (FLIP BLOCKER: observations carry no
  origin_class, so externally-planted user-facts remain first_party until
  delta-level provenance lands). Gate 3: the emit lives INSIDE
  `db/crud/capability_grants.py` (record_success/record_correction/apply_event
  — `origin_class` is a REQUIRED kwarg so every future caller must state
  provenance); all six live callers thread owner/first_party → zero rows today
  by construction. Locks: `test_identity_autonomy_gate_coverage.py` pins the
  loader's 4-method write_text surface by set-equality + the dashboard PUT
  writer manually, and discovers grant-mutation callers ALIAS-RESOLVED (bare
  `record_success` name collisions excluded). The legacy `autonomy_state`
  evidence store is a documented out-of-scope exclusion. Auto-demote wired but
  dormant (server + enforce only); retention via
  `scripts/prune_immunity_shadow.py` (disk-hygiene). The shadow log is readable
  via the `immunity_status` health MCP tool (gate-agnostic: per-gate live mode
  + per-site would-block counts — sizes the B4 enforce blast radius).
  **B4: stored-origin recall + enforce for gates 3-4 (shipped shadow; flip is a
  live `settings_update`).** Recall now plumbs the stored `origin_class`
  (migration 0054) end-to-end — `RetrievalResult.origin_class` on both the
  Qdrant and FTS5-only paths (the latter via a `search_ranked` column,
  coalescing SQLite when a pre-backfill payload is None) — so
  `item_is_blockable` is STORED-FIRST (widens to episodic-external rows; fixes
  the first-party-in-KB over-observe). A second CI sweep
  (`KNOWN_QDRANT_READ_SITES`) locks every direct Qdrant `.scroll`/`.retrieve`
  content→prompt surface; it caught `memory_core_facts` (now gated). The
  gate-2 L-tier substrate: `cc_sessions.origin_class` + `observations.origin_class`
  (migration 0057), stamped at registration from the DISPATCH PROFILE (never a
  tool scan); reflection `user_model_delta` writers carry a run-level window
  aggregate (`cc_sessions.reflection_window_origin`), so the identity emit
  derives real provenance instead of hardcoded first_party (gate-2 stays
  shadow). Enforce (gates 3-4 only; procedure/identity rejected by the
  validator honesty guard): gate-4 drops `external_untrusted` from PUSHED feeds
  (`memory_proactive`, `memory_core_facts`; the proactive hook needs no filter —
  dispatched sessions exit it at module import, total absence) ONLY in dispatched
  UNSUPERVISED sessions under enforce — the discriminator is
  `GENESIS_CC_SESSION` present (stamped unconditionally on every CCInvoker
  child) AND `GENESIS_SESSION_SUPERVISED` absent (`CCInvocation.supervised`,
  set only by ConversationManager's owner-attended invocations).
  `GENESIS_SESSION_ID` is attribution only — foreground conversations carry
  one and some autonomy dispatches don't, so it is wrong in both directions
  as a supervision signal. Explicit queries
  (`memory_recall`/`knowledge_recall`/`memory_expand`) and every foreground
  surface keep wrapped external in all modes (`should_enforce_drop`, fail-open);
  gate-3 refuses grant evidence/state writes with a blockable origin — and the
  refusal is read-only (no `ensure_cell` before the guard: external provenance
  can't even seed a NOT_DETERMINED cell). Wrap + provenance labels are
  STORED-FIRST at every inject surface (review round): `wrap_external_recall`
  and `provenance_descriptor(origin_class=…)` key on the stored origin with the
  collection check as fallback, so external EPISODIC rows are delimited/labeled
  external everywhere (MCP recall/expand/proactive, hook `Memory·external` tag,
  context injector, voice, research executor, dashboard) — the wrap is the
  compensating control on the explicit surfaces the enforce cut retains. Every
  drop/refusal still records (the enforce-mode row IS the block ledger).
  Auto-demote now pages a `critical` `infrastructure_alert` when a gate stands
  down, and counts only ENFORCED INTERVENTIONS (`count_enforced_interventions`
  — rows whose detail carries `refused`/`enforced_drops`), never wrap-only
  observation rows, so a normal explicit-recall session can't flip the gate
  back to shadow. Red-team acceptance: `test_redteam_enforce.py` (synthetic).
- **codebase/**: AST indexer (surplus task, set-difference deletes with
  CASCADE) behind the `codebase_navigate` MCP tool.
- **infra_profile/**: the infrastructure body schema — deterministic fact
  collectors (container plane + host plane via the guardian `host-profile`
  gateway verb; a missing guardian or un-redeployed gateway degrades to
  "not visible from this vantage") → per-section hashed `profile.json` +
  rendered `INFRASTRUCTURE.md` under `~/.genesis/infrastructure/`. **The
  facts/metrics split is load-bearing**: only `facts` are hashed; a hash change
  emits a dedup-gated `infrastructure_drift` observation and regenerates that
  section's LLM annotation (call site 46, strong-first — annotations are PINNED
  to source hashes; staleness derived at render, never stored). Consumers: boot
  step (delayed, non-blocking) + daily 06:20 cron + `infrastructure_profile`
  MCP tool (facts-only refresh cross-process, flock-guarded) + sentinel digest
  + the user-CLAUDE.md `container-specs` block (content owner:
  `infra_profile/claude_md.py`; update.sh invokes `--claude-md-block`).
  Distinct from `observability/snapshots/infrastructure.py` (dynamic health) —
  don't merge them. Memory-resilience invariants are first-class facts:
  container `cgroup_memory_swap_max` (tri-state — "0" IS the 2026-07 wedge
  state) + `oomd_user_slice_kill` (config-plane scan of user.slice.d drop-ins,
  laid down by `scripts/lib/memory_resilience.sh` from bootstrap/update) and
  host-plane `swap_total_kb`, so the annotation layer flags unprotected
  installs (see docs/reference/memory-resilience.md). Network-resilience
  invariants are first-class too: container `networkd_keep_configuration` +
  `network_watchdog_installed` (config-plane facts from
  `scripts/lib/network_resilience.sh`) plus a volatile `watchdog` heal-telemetry
  metric from `/run/genesis-network-watchdog.json` (see
  docs/reference/network-resilience.md).
- **restore/**: thin CLI → `scripts/restore.sh` (counterpart of the 6h
  encrypted `scripts/backup.sh` timer).
- **util/**: `atomic_write_text`, `tracked_task` (logs swallowed exceptions),
  `process_lock` (the reason bare `python -m genesis serve` blocks systemd),
  tmp discipline (`~/tmp` for large temp — never override TMPDIR).
- **env.py**: 3-tier resolution (env var → `~/.genesis/config/genesis.yaml` →
  default). **`update_in_progress()` is load-bearing**: the watchdog defers
  restarts during deploys (mid-deploy revival deadlocks bootstrap); fails open
  to "no deploy". `secrets_path()` is repo-relative unless SECRETS_PATH set.
- **_config_overlay.py**: `.local.yaml` deep-merge (user config dir first;
  dicts merge, lists REPLACE wholesale); dependency-free by design to stay
  import-cycle-safe.

## 13. Modules, skills & self-extension

The pluggable edges: capability modules, the skill library, and the pipeline
for contributing code upstream.

```yaml subsystem-map
entry: modules-skills
modules: [modules, skills, contribution, bookmark, workflows]
verified: 9037d45b 2026-07-07
```

- **modules/**: capability modules are "hands, not brain" — a module may
  observe Genesis but never participates in cognition, and MUST NOT set
  `source_subsystem` on memory writes (test-enforced). Two-phase load
  (config/modules/*.yaml + auto-discovery; YAML wins), enabled-state persisted
  in DB. Shipped: content-pipeline (enabled, ALL auto-features OFF),
  crypto-ops, prediction-markets. GROUNDWORK: autonomous-distribution.
- **skills/**: skills are directories with SKILL.md — registration is catalog
  generation (`scripts/generate_skill_catalog.py` scans `.claude/skills/`,
  `src/genesis/skills/`, `~/.genesis/skill-library/` →
  `~/.genesis/skill_catalog.json`, self-heals hourly), consumed by the
  injection hook and by autonomous-session resources. Skill refinement is a
  tracked cognitive-file modification (`learning/skills/applicator.py`).
  Voice-master exemplars are on the contribution FORBIDDEN list.
- **contribution/**: `python -m genesis contribute <sha>` — sanitize-then-PR
  upstream, pseudonymous. `sanitize.scan_diff()` is FAIL-CLOSED (8 scanners;
  any finding stops). Its forbidden-globs floor duplicates
  `config/protected_paths.yaml` — keep in sync.
- **bookmark/**: two-tier session bookmarks stored as episodic memories +
  a lookup table; enrichment runs on surplus compute.
- **workflows/**: YAML DAG executor — GROUNDWORK(workflow-engine), built with
  NO runtime caller. Not live; do not treat as a capability.

---

*Maintenance: run `python scripts/check_subsystem_map.py` from the repo root;
CI runs it on every PR. Entry stamps mark the commit each entry was last
verified against — bump them when you re-verify, not when you merely edit
prose.*
