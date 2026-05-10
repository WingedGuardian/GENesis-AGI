# Changelog

All notable changes to Genesis are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows Genesis release stages (v3.0a → v3.0b → v3.1 → v4.0a…).

---

## [Unreleased]

### Changed

- **MCP code-intelligence tools auto-upgrade on install/bootstrap**
  --- `scripts/bootstrap.sh` and `scripts/install.sh` now re-run the
  codebase-memory-mcp installer unconditionally (it is idempotent and
  pulls the latest release) and call `uv tool upgrade serena-agent`
  when Serena is already present. Existing installs get the latest
  versions on the next bootstrap; fresh installs are unchanged.
  GitNexus is unaffected (deliberately pinned to a prerelease
  channel).

---

## [v3.0b7] - 2026-05-09

Ego gets two new self-awareness features, references move into the
episodic graph, and Opus 4.7's xhigh effort tier becomes a first-class
option.

### Added

- **Ego causal intervention journal** (#284) --- every proposal now
  tracks its lifecycle (proposed → approved/rejected → executed →
  outcome) in a queryable journal. Ego can correlate decisions with
  outcomes to learn from past judgments.
- **Ego self-model capability map** (#288) --- Genesis maintains a
  live capability inventory aggregated from MCP tools, channels,
  modules, and memory wings. Ego references this when proposing
  actions to avoid suggesting things it can't do.
- **Email outbound channel** (#289) --- Genesis can now send email
  via the configured outbound provider. Third outreach lane alongside
  Telegram and dashboard.
- **GitHub star tracking** (#289) --- recon source captures
  GENesis-AGI repo stargazer activity. Surfaces in morning reports.
- **xhigh effort tier** (#297) --- Claude Code 2.1.111's xhigh tier
  for Opus 4.7 is now recognized everywhere Genesis hands off effort
  level (CC invoker, Telegram `/effort`, `session_set_effort` MCP,
  dashboard). Defaults remain at `high`; xhigh is opt-in.
- **Morning report observations** (#285) --- recent unresolved
  observations are surfaced alongside the usual morning digest, so
  operators see what Genesis is paying attention to.
- **Follow-up retention cleanup** (#293) --- completed and failed
  follow-ups older than 30 days are now purged daily at 02:30 UTC.
  Pinned items are preserved.

### Changed

- **Reference storage migrates to episodic memory** (#296) --- 52
  reference vectors move from `knowledge_base` to `episodic_memory`
  via SQLite migration 0013 + Qdrant init-time migration (idempotent).
  References now surface naturally via all memory recall paths.
  `reference_lookup` continues to work; only the storage collection
  changed.
- **Disk alert threshold** (#295) --- `health_alerts` now fires
  WARNING at <15% free disk (was CRITICAL-only at <10%). The 10–15%
  gap is no longer a blind spot.

### Fixed

- **Ego self-reinforcing holdback loop** (#283) --- ego could spiral
  into withdrawing its own proposals based on its own prior
  decisions. The holdback heuristic now considers proposal age and
  user signal correctly.
- **Heartbeat cleanup not wired** (#281) --- subsystem heartbeats
  weren't being aged out, leaving stale records in the dashboard.
- **Surplus task double-enqueue** (#281) --- `active_by_type` check
  now matches the dispatch loop's filter, so scheduled surplus jobs
  don't double-enqueue.
- **Outreach metric mislabels** (#289) --- corrected mislabeled
  outreach counters in the dashboard.

---

## [v3.0b6] - 2026-05-09

Memory retrieval gets faster graph traversal, explicit drift control,
and better observability.

### Added

- **NetworkX graph engine** (#279) --- in-memory graph over 43K+ memory
  links replaces recursive SQL queries. Enables centrality scoring and
  shortest-path queries. Falls back to SQL if NetworkX is unavailable.
- **DRIFT retrieval mode** (#279) --- `memory_recall` gains a `mode`
  parameter: `"auto"` (default, unchanged behavior), `"standard"`
  (no drift fallback), `"drift"` (direct 3-phase retrieval).
- **Recall instrumentation** (#279) --- every `memory_recall` call now
  logs which pipeline was used (standard, drift, auto→drift) for
  retrieval quality analysis.

### Fixed

- **Knowledge re-ingestion creates duplicates** (#279) --- the
  orchestrator now uses idempotent upsert with stale Qdrant cleanup
  instead of raw insert. Re-ingesting a URL no longer creates orphaned
  vectors.
- **DB resilience** (#273) --- awareness tick survives transient SQLite
  connection failures with automatic recovery and alert deduplication.

### Changed

- **Dashboard call site badges** (#278) --- parallelization indicator
  shows which call sites run concurrently.
- **Routing updates** (#274, #277) --- DeepSeek V4 Flash added, GLM 5.1
  renamed, call site descriptions added to routing config.

---

## [v3.0b5] - 2026-05-07

Sentinel gets smarter, ego learns its boundaries, and a cascade of
observation spam gets silenced at the source.

### Changed

- **Sentinel upgraded to Opus** (#245) --- the container-side health
  guardian now runs on the strongest available model. Both Sentinel and
  Guardian prompts gain planning directives, tenacity rules, known
  pitfalls from production incidents, and live operational context
  injection from essential knowledge.
- **Ego domain boundaries** (#248) --- User Ego no longer tracks
  operational costs or opines on config values. Genesis Ego stays in its
  infrastructure lane. Both egos receive explicit rules separating user
  career goals from Genesis marketing goals.

### Fixed

- **Observation spam eliminated** (#248) --- micro-reflection dedup
  was hashing LLM-generated summary text, which varies each tick. Now
  hashes structural properties (tags, anomaly flag, signal names).
  Stops the 21+ duplicate `user_goal_staleness` observations per day.
- **Approval gate restored** (#245) --- PR #240 accidentally set the
  live config to `manual_approval_required: false`. Fixed with
  three-layer config separation: code default (True, safe fallback),
  repo YAML (false, friction-free installs), local overlay (user
  preference, gitignored).
- **Telegram polling reconnected** (#245) --- adapter_v2 was stuck in
  a stall loop (26 consecutive 900s stalls). Server restart
  reinitialized the connection cleanly.
- **Files tab fills viewport** (#245) --- the 1400px max-width
  constraint lifts when the Files tab is active. File content viewer
  now resizable in both directions (#247).
- **Download button visible** (#245) --- enlarged with text label.

### Removed

- **CC version watcher deactivated** (#248) --- the automatic Claude
  Code update signal was generating noise. Genesis version watcher
  (upstream update detection) stays active.

### Infrastructure

- **Ubuntu/noble portability** (#248) --- `scripts/host-setup.sh` now
  accepts `GENESIS_CONTAINER_IMAGE` env var override instead of
  hardcoding `images:ubuntu/noble`.

---

## [v3.0b4] - 2026-05-06

Settings get a proper overhaul, ego recovers from a multi-day deadlock,
and memory recall learns to try harder when results are thin.

### Added

- **Dashboard PWA support** (#242) --- manifest + service worker make
  the dashboard installable as a standalone mobile app. Memory tab gains
  a 30-day growth sparkline and wing distribution badges.
- **File download** (#232) --- Files tab gets a download button with
  50MB cap, path traversal protection, and symlink-aware security.
- **Drift recall fallback** (#233) --- when `memory_recall` returns
  sparse results (<3), the 3-phase drift retrieval algorithm
  (global scan → cluster drill-down → weighted RRF) fires automatically.
  Silent degradation on failure.
- **Query term expansion** (#234) --- `expand_query_terms` parameter
  exposed on the `memory_recall` MCP tool, enabling tag co-occurrence
  query expansion for ambiguous searches.

### Changed

- **Settings consolidation** (#240) --- all per-subsystem timezone
  fields replaced by `genesis.env.user_timezone()`. Dashboard settings
  tab gets domain ordering, expanded form domains, and descriptions
  for all 18 settings groups.
- **Inbox retry dedup** (#243) --- scanner reuses existing failed rows
  instead of creating duplicates on retry. CC invoker captures stderr
  on timeout for diagnostics. Evaluation timeout raised to 900s.

### Fixed

- **Ego deadlock** (#241) --- approval blocks no longer trip the circuit
  breaker (new `CycleBlockedError` exception). Approval requests get
  timeouts (1h CLI, 2h sentinel). Telegram proposals split at 4096 chars
  instead of failing silently. Proposal field truncation limits raised
  4–5x.

---

## [v3.0b3] - 2026-05-05

Web tools get MCP exposure so background sessions and subagents can
actually use them. Ego proposals flow through approval correctly.
SSH dispatch enables cross-machine module communication.

### Added

- **SSH IPC adapter** (#225) --- external modules can now dispatch
  prompts to remote Claude Code instances over SSH. Two modes: CC
  (structured JSON) and SHELL (raw commands). Enables module
  communication without standing up HTTP services.
- **Protected paths guard** (#226) --- PreToolUse hook blocks accidental
  deletion of session transcripts, backups, snapshots, browser profiles,
  and the production database.

### Changed

- **Web tools exposed via MCP** (#229) --- `web_fetch` and `web_search`
  are now MCP tools on genesis-health, making Scrapling, Crawl4AI,
  SearXNG, and the paid search backends accessible to background
  sessions, ego, and subagents (previously required Bash/Python imports).
  Behavioral nudges steer sessions toward these over CC's built-in
  WebFetch/WebSearch.
- **Ego proposal flow** (#228) --- proposals now route through the
  approval gate correctly. Auto-promote removed; all proposals require
  explicit approval before execution.
- **Sentinel alarm clearing** (#227) --- auto-clear fires only when the
  specific pending alarm resolves, not all alarms indiscriminately.
- **Temp file conventions** (#226) --- `~/tmp/` documented as the
  standard transient path. `/tmp/` (512MB tmpfs) is off-limits.

### Fixed

- **Migration runner compatibility** (#230) --- migration 0010 handles
  databases that lack the `memory_metadata` table (test fixtures, fresh
  installs before DDL runs).
- **Dashboard memory bar** --- uses correct anonymization percentage for
  status assessment.
- **Drift recall and step dispatcher** --- critical bugs in recall
  query, bi-temporal column migration, and dispatcher routing.

---

## [v3.0b2] - 2026-05-03

Ego becomes perceptive, task execution gets smarter about blockers, and
Genesis can now bootstrap code intelligence tools on fresh machines.
Seventeen PRs landed — a mix of new capabilities, reliability fixes, and
documentation that reflects what the system actually is.

### Added

- **Ego memory surfacing** (#207) --- the ego now pulls relevant memories
  before proposing actions, grounds proposals in evidence, and flags
  recurring observation patterns (Hapax-style proactive discovery).
- **Planning-first direct sessions** (#207) --- background CC sessions
  receive a planning instruction so they structure work before executing.
- **Voice identity layer** (#207) --- `VOICE.md` defines output taste
  (tone, rhythm, vocabulary) injected into content generation and ego
  sessions.
- **Deep research for task blockers** (#216) --- when the task executor
  hits an unresolvable blocker, it spawns a deep-research session and
  uses the findings to construct an exit gate, rather than spinning.
- **Architecture Decision Records** (#217) --- seven ADRs documenting
  load-bearing choices (ego ephemeral sessions, surplus routing, memory
  wings, no silent timeouts, router dead-letter, LLM-first judgment).
- **Memory DRIFT recall** (#217) --- bi-temporal columns on memory
  metadata enable time-aware retrieval and staleness detection.
- **Medium distribution** (#210) --- publish to Medium via Camoufox
  browser automation with voice-calibrated formatting.
- **Code intelligence bootstrap** (#222) --- `bootstrap.sh` and
  `install.sh` now install and configure codebase-memory-mcp, GitNexus,
  and Serena automatically on fresh machines. Includes MCP registration
  and initial indexing.
- **Architecture deep-dives and case studies** (#213) --- three
  subsystem deep-dives (routing, memory, autonomy) and four case studies
  showing Genesis in practice.
- **Positioning and taxonomy docs** (#217) --- "Genesis vs. CLAUDE.md"
  differentiator and the Four C's external vocabulary.

### Changed

- **Approval staleness guard** (#208) --- stale approval records are now
  pruned on each cycle. Infrastructure monitor respects disable flag.
- **Ego interact profile expanded** (#215) --- the interact safety
  profile now permits content publishing dispatch.
- **README primitives section** (#223) --- updated to reflect
  genesis-router and genesis-memory as the two extractable libraries.

### Fixed

- **Surplus scoring collapse** (#209) --- scoring function no longer
  collapses to zero when all candidates tie. Watchgod /tmp protection
  and surplus routing corrected.
- **Telegram polling** (#211) --- retry logic on polling timeout,
  reduced alert noise from transient failures, morning report
  completeness improved.
- **Knowledge source pipeline default** (#206) --- new knowledge sources
  default to `knowledge_ingest` pipeline instead of `recon`.
- **Browser keystroke typing** (#221) --- CDP remote sessions now type
  per-keystroke instead of bulk-setting input values, fixing sites that
  validate on keypress.
- **CI stability** (#219) --- fixed lint errors (unused imports,
  f-string prefixes), duplicate migration prefix detection, and test
  isolation for migration runner.
- **STEERING.md write protection** (#214) --- autonomous learning
  pipelines can no longer modify steering rules without user approval.

---

## [v3.0b1] - 2026-05-01

First beta. The ego subsystem---Genesis's autonomous decision-making
layer---is stable and public. Two egos (User Ego and Genesis Ego) run on
adaptive cadence, propose actions via Telegram, and execute approved work
autonomously. The reflection pipeline now feeds both egos balanced
context instead of flooding one with infrastructure noise.

### Added

- **Ego module** (#26, #27) --- two autonomous egos with ephemeral
  sessions, model selection, proposal board, and tiered execution.
  User Ego (CEO, Opus) focuses on user goals; Genesis Ego (COO, Sonnet)
  handles system health. Both dispatch CC sessions with approval gates.
- **Reflection rebalancing** (#196) --- observations now carry relevance
  tags (`:user`, `:genesis`, `:both`). Each ego sees what it needs
  instead of everything. Two new signal collectors track user goal
  staleness and session activity patterns.
- **Ego context enrichment** (#205) --- User Ego now sees an activity
  pulse (goal staleness, session rhythm, conversation count), model
  freshness warnings, and backlog depth (inbox, recon, follow-ups).
  Genesis Ego gets signal trend arrows across ticks. Both egos see
  recent proposal outcomes for self-calibration.
- **Sequential task execution** (#193) --- tasks execute one at a time
  with per-step approval skipping for trusted subsystems.
- **Task intake gate** (#199) --- SQLite trigger rejects malformed task
  submissions before they reach the executor.
- **Pinned follow-ups** (#185) --- follow-up items can be pinned so they
  survive batch resolution.

### Changed

- **Approval gate redesign** (#198) --- stable approval keys for
  recurring dispatches (ego cycles, inbox evaluation). One approval per
  request, no reuse of stale approvals. Pass 3 content-blind matching
  removed entirely.
- **Repetitive micro reflections reduced** (#195) --- consecutive
  identical micro observations are suppressed.

### Fixed

- **Genesis Ego crash** (#198) --- `signals_json` stored as a list, not
  a dict. Every genesis ego cycle hit `AttributeError` on `.items()`.
- **Approval notifications** (#29) --- per-tick notifications are now
  idempotent; duplicate approvals filtered (#33).
- **Executor worktree persistence** (#188) --- worktree paths survive
  server restarts.
- **Dashboard memory gauge** (#202) --- displays anonymous memory
  percentage instead of used percentage.
- **Resilience metrics** (#201) --- correct memory metric source, /tmp
  pressure axis, phantom L2 autonomy level.
- **Ego dashboard controls** (#192) --- column names, model override,
  budget cap fixes.

---

## [v3.0a11] - 2026-04-28

Guardian auto-sync, task executor maturity, ego module. Themes:
**autonomous execution**, **adversarial verification**, **cognitive
architecture**, and **host VM self-maintenance**.

### Added

- **Guardian auto-sync** (#168, #169, #170, #171) — host VM Guardian now
  stays automatically in sync with container updates. When you update
  Genesis, changed Guardian-relevant code is pushed to the host via SSH.
  Drift detection alerts within 15 minutes if sync fails silently.
  No more manual SSH to update Guardian code.
- **Ego module** (#182) — autonomous decision-making cycle with cadence
  management, proposal board, context assembly (user + Genesis + system),
  and session dispatch. Dashboard route for ego status.
- **LinkedIn distribution** (#182) — content delivery via Composio SDK
  with OAuth2. Graceful degradation when unconfigured. Optional
  `[distribution]` dependency.
- **Typed module config schema** (#167) — `ConfigField` dataclass with
  type/min/max/required/sensitive metadata. `ModuleBase` mixin for
  zero-boilerplate config. Auto-discovery for new modules without YAML.
  Dashboard widget fix: correct input types for all field kinds.
- **Session intent trail** (#179) — detects topic pivots via keyword
  similarity, injects `[Session trail] topic → topic → ...` into every
  prompt so conversation flow survives compaction.
- **Task executor pipeline** (#177) — tool-capable adversarial
  verification with Codex, recovery resume for interrupted tasks.
- **Sentinel rejection test coverage** (#166) — 6 tests verifying the
  24-hour dispatch suppression window after user rejection.

### Changed

- **Decomposer uses CC invoker** (#181) — task decomposition now uses
  CC invoker (Sonnet) instead of route_call. Falls back to route_call
  if invoker unavailable.
- **Adversarial review runs in worktree** (#183) — Codex and CC invoker
  verification now execute in the task's worktree directory, not the
  repo root. Fixup steps receive the original plan content and longer
  feedback (2000 chars, up from 500).
- **Browser concurrency safety** (#166) — all 7 interaction tools now
  acquire a lock before accessing shared page state.

### Fixed

- **Blocked tasks resume on approval** (#178) — dispatcher polls for
  approved-but-unconsumed approvals on blocked tasks, re-dispatching
  without requiring a server restart.
- **Dispatcher dedup guard** (#181) — tasks reset to PENDING are
  re-dispatchable without server restart.
- **Plan path tilde expansion** (#178) — `expanduser()` on plan paths.
- **PENDING→FAILED transition** (#178) — tasks that fail before REVIEWING
  no longer get stuck in PENDING forever.
- **Concurrent session contamination** (#173) — raw user messages from
  other sessions no longer appear in concurrent session tags.
- **Observability gaps** (#165) — `exc_info=True` on timeout-path log
  calls; replaced `contextlib.suppress(Exception)` with logged warnings.
- **Update subprocess logging** (#165) — direct update and CC tier
  spawning now log to `~/.genesis/` instead of /dev/null.

### Upgrade notes

**Existing users with Guardian on a host VM:** One-time bootstrap required
to enable auto-sync. Run on your **host VM** (not the container):

```bash
cd ~/.local/share/genesis-guardian
incus exec genesis -- tar -cf - -C /home/ubuntu/genesis \
    src/ scripts/ pyproject.toml config/guardian-claude.md | tar -xf -
cp scripts/guardian-gateway.sh ~/.local/bin/guardian-gateway.sh
chmod +x ~/.local/bin/guardian-gateway.sh
systemctl --user restart genesis-guardian.timer
```

Or: `bash scripts/install_guardian.sh --non-interactive`

After this one-time step, all future updates are automatic.

---

## [v3.0a10] - 2026-04-24

31-commit release. Themes: **multi-step surplus pipelines**, **browser
stealth**, and **reflection quality**.

### Added

- **Surplus pipeline engine** (#147, #149) — deterministic multi-step
  task chains for analytical work. Each step runs on free-tier models;
  the pipeline mechanically advances between steps. First pipeline:
  prompt effectiveness review (catalog call sites, sample outputs,
  evaluate and recommend improvements).
- **Follow-up management** (#146) — `follow_up_update` MCP tool for
  modifying tracked follow-up items.
- **Browser stealth layer 2** (#128) — humanized mouse movements, typing
  cadence, click randomization, and CAPTCHA escalation for automated
  browser sessions.
- **CDP remote backend** (#135) — drive a real Chrome browser over
  Tailscale instead of running headless locally.

### Changed

- **Reflection quality improvements** (#123, #127, #139) — identity
  context for API reflection path, surplus decoupled from reflection
  engine, sentinel recovery wiring, light cognitive state, frequency
  tuning, and NOMINAL quality gate for infrastructure monitoring.
- **Browser reliability** (#126, #133, #134) — always-headed mode, hard
  timeouts, keyboard fallback, ambiguous selector guard, noVNC scaling
  fix.

### Fixed

- **Database write serialization** (#141) — prevents permanent connection
  lock when concurrent writes collide on aiosqlite.
- **Dashboard scroll restoration** (#124) — mouse wheel scrolling works
  on all pages again.
- **Sentinel dashboard indicator** (#130) — yellow indicator for approval
  states plus CI skip markers.
- **Safety fixes** (#136) — surplus test hardening, morning report idle
  filter, sentinel re-verify.

### Removed

- **Infrastructure monitor schedule** — removed from surplus cron.
  Produced noise (459 insights, 1 promotion). Returns as a focused
  "monitor the monitors" pipeline in a future release.

---

## [v3.0a9] - 2026-04-22

7-commit release. Themes: **background session spawner**, **content
pipeline**, **browser reliability**, and **outreach fixes**.

### Added

- **Direct session spawner** (#118, #121) — spawn profile-constrained
  background CC sessions via `direct_session_run` MCP tool. Three safety
  profiles (observe, interact, research) control what each session can do.
  DB-backed dispatch queue ensures sessions outlive the calling session.
- **Content pipeline activation** (#117) — content module wired into
  outreach system with CONTENT category for multi-platform publishing.
- **Browser process hygiene** (#115) — idle timeout (1h auto-cleanup),
  orphan process detection, background reaper for stuck browser processes.

### Changed

- **Browser stale context recovery** (#116) — detects dead browser pages
  and transparently reconnects. Session history tracking and VNC
  environment improvements.

### Fixed

- **Outreach pipeline** (#122) — approval reuse, alert routing, surplus
  topic handling, staleness decay. Fixes pre-existing test failures in
  cognitive state rendering.

---

## [v3.0a8] - 2026-04-21

21-commit release. Themes: **knowledge dashboard UX**, **browser
automation upgrade**, **cross-session awareness**, and **CI/security
hardening**.

### Added

- **Knowledge dashboard overhaul** (#104) — in-page confirm modals
  (immune to browser dialog blocking), drag-drop file upload, processing
  mode toggle (extract vs store-as-is), parallel distillation pipeline
  (4x concurrent), and crash recovery for stuck uploads.
- **File modification audit trail** (#109) — PostToolUse hook records all
  Write/Edit operations with session ID, file path, and file hash. Query
  "what session modified this file?" in one SQL call.
- **Browser collaborative mode** (#107) — side-panel extension for
  real-time observation of automated browser sessions.
- **Cross-session awareness** (#97) — awareness loop now tracks
  observations across sessions with TTL-based hygiene.
- **Output safety convention** (#112) — pre-commit hook warns when
  non-code files are staged, directing to `~/.genesis/output/`.

### Changed

- **Camoufox as primary browser** (#108) — anti-fingerprint browser now
  default for all automation. Chromium available as fallback.
- **Neural monitor grid redesign** (#106) — reorganized dashboard grid
  layout for better information density.
- **Proactive memory enrichment** (#95, #96) — hook results now include
  age, wing, and ID for expand-without-re-search. Limits bumped to
  300/200 chars with smart sentence truncation.
- **Cerebras-Qwen routing** (#104) — promoted to 6 call site chains
  (3 primary, 3 fallback) for surplus and knowledge workloads.
- **Sweep infrastructure** (#98, #102) — provider registry cleanup, MCP
  audit, CLAUDE.md compression.

### Fixed

- **CI test suite** (#110) — resolved 30 pre-existing failures. Skip
  guards for optional dependencies, mock fixes, routing assertion updates.
- **Security hardening** (#111, #113) — prevent stack trace exposure in
  file API responses, clear-text logging of sensitive reference data.
- **Surplus Telegram delivery** (#105) — surplus-originated reflections
  now reach Telegram instead of silently completing.
- **Approval system** (#101) — micro-reflection salience gate removed
  (user sees everything), approval_request_id now populated on
  cli_approved.
- **Stale update banner** (#103) — dashboard auto-resolves the
  update-available banner after successful update.
- **Process reaper** (#10aa9edc) — extended to kill stale Claude sessions
  older than 7 days.

---

## [v3.0a7] - 2026-04-19

25-commit release. Themes: **dashboard and settings overhaul**, **web
fetching upgrade**, **timezone correctness**, and **operational
documentation**.

### Added

- **Scrapling TLS fingerprinting** (#75) — web fetcher upgraded with
  anti-bot bypass via `curl_cffi` TLS impersonation. Cloudflare Quick
  Actions (`/markdown`, `/json`) for JS-rendered content extraction.
- **Observation surfacing + output verification** (#77) — autonomous
  task executor now verifies its own output against success criteria.
  Observations surface in dashboard and outreach.
- **Surplus config wiring + DB-backed approvals** (#84) — surplus
  compute settings configurable via dashboard. Sentinel approvals
  persisted to database (survive restarts).
- **MCP module config overlay** (#94) — MCP tools now discover modules
  from both repo and local config directories, matching runtime behavior.
- **Contribution sanitizer** — auto-blocks gitignored paths from upstream
  PRs.

### Changed

- **Identity file deduplication** (#93) — consolidated overlapping
  content across CLAUDE.md, SOUL.md, STEERING.md, and CONVERSATION.md.
  Each file now has a distinct scope with no redundancy.
- **Settings panel functional** (#82, #89) — settings viewer, routing
  panel consolidation, environment variable expansion fix. Previously
  read-only, now editable.
- **Approval queue** — moved from dedicated page to dashboard overview
  with inline resume mechanism.
- **Knowledge and Memory UI** (#86) — resizable file browser, improved
  layout, tmux compatibility fix.
- **Process management docs** — CLAUDE.md documents systemd units, MCP
  server lifecycle, and the nohup prohibition.

### Fixed

- **Timezone across the board** (#79) — outreach scheduling, alert
  timestamps, and follow-up due dates now respect the configured user
  timezone instead of defaulting to UTC.
- **Neural monitor accuracy** (#92) — disabled providers excluded from
  health display and dropdown. Accuracy metrics cleaned up.
- **Anthropic provider regression** (#88) — providers restored after
  routing config change accidentally dropped them. False queue-empty
  alerts eliminated.
- **SSH PATH** (#87) — Claude CLI now found in SSH RemoteCommand context
  (Guardian diagnosis sessions).
- **Knowledge tab** (#82, #83) — stats endpoint AttributeError fixed,
  CSS corrected, tab fully functional.
- **Strategic reflection routing** (#81) — reflection sessions now route
  to correct providers. Essential knowledge noise reduced.
- **Morning report** (#85) — formatting, missing data handling, and
  observation inclusion fixes.
- **Extraction quality** (#76) — dashboard thresholds tuned, code index
  priority corrected.

---

## [v3.0a6] - 2026-04-17

137-commit release. Major themes: **knowledge ingestion pipeline**,
**embedding storm fix** (Ollama CPU spikes eliminated), **awareness
scoring overhaul**, and **persistent reference store**.

### Added

- **Knowledge ingestion pipeline** (#67, #68) — `knowledge_ingest` MCP
  tool for ingesting files and URLs as authoritative knowledge units.
  Dashboard file upload UX with drag-drop support and ingestion worker.
- **Awareness scoring overhaul** (#65) — signal redistribution across
  subsystems, subsystem-level signals, citation tracking for score
  attribution.
- **Persistent reference store** (#58) — unified store for credentials,
  URLs, IPs, and account handles learned across sessions. Auto-capture
  from conversations, `reference_lookup` retrieval, read-only mirror at
  `~/.genesis/known-to-genesis.md`.
- **Merge/push safety hooks** (#60) — PreToolUse hooks block `git merge`
  on main and `git push origin main` to enforce PR workflow.
- **Session observer** — real-time tool activity capture for foreground
  CC sessions, feeding memory extraction.
- **Codebase navigation MCP tool** — progressive drill-down code
  exploration (`codebase_navigate`).

### Changed

- **Queue-first extraction** (#66) — memory extraction no longer
  hammers the embedding backend with hundreds of sequential calls.
  Stores FTS5-only, queues embeddings for the recovery worker's paced
  drain (10/min). Reduces Ollama embed calls from ~562/hr to ~10/min.
- **Ollama health cache** (#66) — `is_available()` results cached for
  120s, eliminating ~818 uncached `/api/tags` polls per hour.
- **Budget event emission** (#71) — `budget.exceeded` events fire once
  per budget period (daily/weekly/monthly) instead of on every routing
  call. Reduces log entries from ~2,857/7hr to 1 per period crossing.
- **Embedding recovery drain limit** — increased 100 → 500 to handle
  full extraction cycle output in a single recovery pass.
- **CLAUDE.md scope split** — extracted Serena guide, moved dev rules
  to genesis-development skill, compressed main CLAUDE.md.

### Fixed

- **Systemd PATH** (#66) — service templates now include Claude CLI bin
  dir (`__CC_BIN_DIR__`), fixing "Claude CLI not found" errors in
  Telegram bridge sessions. Detected at install time, falls back to
  `~/.npm-global/bin`.
- **Embedding recovery status** (#66) — recovery worker now updates
  `memory_metadata.embedding_status` from "pending" to "embedded"
  after successful recovery (was stale on the queue-first path).
- **Security**: redact identifier in migration dry-run log (#70).
- **Cognitive state catch-22** (#64) — dashboard quality issues and
  circular dependency in state initialization.
- **Backup passphrase, cost attribution, dashboard UX** (#63) — four
  fixes from post-Codex audit.
- **OpenCode wrapper** (#61) — silent exit when no stale sessions exist.

---

## [v3.0a5] - 2026-04-17

120-commit batch release — memory v4, surplus compute, eval framework,
follow-ups, new providers, and update-system improvements.

### Added

- **4-layer memory redesign** (#37) — hybrid retrieval (vector + FTS5 +
  RRF fusion), wing/room taxonomy, essential knowledge layer, activation
  scoring, graph traversal.
- **Skill validator + evolution pipeline** (#34) — validation framework
  for skills with evolution tracking.
- **Encrypted backups** (#53) — Qdrant snapshot encryption, backup
  history migration script.
- **"Your Genesis"** (#48) — encrypted backups, `restore.sh`, unified
  docs for the dual-repo model.
- **Outreach recovery worker** — retries failed deliveries with backoff.
- **Approval staleness + session timezone** — stale approvals
  auto-expire, timezone-aware session tracking.
- **Dashboard timezone endpoint** — configurable timezone via settings.

### Fixed (install hardening, PRs #46-52)

13 install fixes from fresh-VM testing:
- Auto-scale container resources to host capacity (#46).
- Five bugs from fresh VM install test (#47).
- Single incus exec smoke test + timezone seed (#50).
- Unbound `UBUNTU_UID` in timezone seed (#51).
- Remove `secrets.env` seed that broke `git clone` (#52).
- TTY detection, timezone persistence across `apt-get`, `read` EOF.

### Fixed (other)

- **Security**: CodeQL findings — stack-trace exposure, workflow
  permissions (#39).
- **Reflection**: post-Codex audit Phase 1+2 — stop silent failures,
  influence timing, surplus count, scheduler timezone, `parse_failed`.
- **Routing/Sentinel**: `cb.is_available()` fix + `watchdog_failing`
  Tier 2.
- **Guardian**: SSH test uses gateway-compatible ping; `cp -rT` for
  update path.
- **Telegram**: offset persist suppressed on fresh processes (#43).
- **Dashboard**: portability — genericize tz examples (#45).
- **Outreach**: remove dead dedup code.
- **CI**: detect-secrets false positive allowlist (#41).
- README updated — Genesis in 30 seconds, quickstart first, 100k+ LOC.
- Stale branch auto-cleanup after public releases (#44).

---

## [v3.0a4] - 2026-04-13

### Changed

- **Merge-based update system** — Genesis updates via `git merge` instead
  of rebase, compatible with the dual-repo model. Three-tier CC
  escalation for conflict resolution: Haiku (watch), Sonnet (resolve
  trivial), Opus (deep incompatibilities). Crash recovery via
  `update_state.json` phase tracking with automatic rollback.
- Tag-based version comparison (robust against squash-merge divergence).
- Dashboard poll timeout extended to 10 minutes.
- Service management without systemd D-Bus session bus (reads PID from
  lock file).

### Fixed

- PID file cleanup moved to Python `finally` blocks.
- `proc.wait(timeout=3600)` prevents hung CC session wedging background
  thread.
- Escalation recovery in `update_progress()` auto-spawns Tier 2 after
  Flask restart.
- JSON heredoc injection fixed (`FAILEOF`/`CEOF` replaced with
  `json.dumps` via env vars).
- Removed nohup fallback from service management; systemd only.
- `_orchestrator_alive` set inside lock before `thread.start()` (TOCTOU).

---

## [v3.0a3-hf3] - 2026-04-12

Public-primary repo overhaul — Genesis now defaults to install-agnostic
configuration. Machine-specific values (IPs, timezone, GitHub identity)
move to `~/.genesis/config/genesis.yaml` instead of being hardcoded in
the repo. Sets up the public repo (`GENesis-AGI`) as the primary
development target going forward.

### Added

- **Local config overlay** (`~/.genesis/config/genesis.yaml`). Three-tier
  precedence: env var > local config > safe default. Covers Ollama/LM
  Studio URLs, timezone, GitHub identity. Generate with
  `./scripts/setup-local-config.sh`.
- **`setup-local-config.sh`** — Interactive setup script for new installs.
  Auto-detects system timezone, migrates `career-agent.yaml` to local
  overlay on first run.
- **Local module overlay** (`~/.genesis/config/modules/`). User-specific
  module configs (e.g. career-agent) live outside the repo; local files
  take precedence over repo files on same filename.
- **Local research-profile overlay** (`~/.genesis/config/research-profiles/`).
  `ProfileLoader.merge_overlay()` loads user-specific profiles not
  committed to the repo.
- **CI leak detector** — `leak-detector` job in `.github/workflows/ci.yml`
  blocks PRs with hardcoded timezones, personal paths, private repo refs,
  secrets (`detect-secrets`), and personal email addresses.
- **`config/genesis.yaml.example`** — Template for local config.

### Changed

- Config YAMLs (`ego`, `outreach`, `inbox_monitor`, `mail_monitor`):
  timezone defaults changed from `America/New_York` to `UTC`. Existing
  installs set timezone in `~/.genesis/config/genesis.yaml`.
- `tz.py`, dataclass defaults, and config loaders now resolve timezone
  via `user_timezone()` from `env.py` instead of hardcoded string.
- CLAUDE.md: hardcoded IPs and GitHub usernames removed; network config
  points to local config file.
- `.claude/docs/dual-repo.md` rewritten for three-repo model.

### Fixed

- `prepare-public-release.sh` portability scan now excludes `ci.yml`
  (the leak-detector job contains timezone patterns as scanner definitions,
  not config leaks). Removed stale `Build Order` CLAUDE.md regex.

---

## [v3.0a3-hf1] - 2026-04-11

Hotfix immediately after v3.0a3 to restore Phase 6 functionality in the
public release and clear a caplog-flakiness regression. Also rides along
a small community security fix.

### Fixed

- **Release-pipeline templating was too broad.** `prepare-public-release.sh`'s
  step 5b passes (`find + grep + sed -i`) rewrote the contribution
  sanitizer's own regex patterns, the `tz.py` default timezone, and a
  couple of test fixtures that legitimately hold these literals as data.
  In the v3.0a3 public release this shipped a broken Phase 6 sanitizer —
  patterns like `${HOME}/genesis` didn't parse as intended, `\bUTC\b` was
  flagging the opposite of user-specific timezones, and the `tz.py` default
  `_DEFAULT_TZ` was clobbered. Added inline `-not -path` exclusions to
  every 5b templating pass for `src/genesis/contribution/sanitize.py`,
  `tests/test_contribution/test_sanitize.py`, `src/genesis/util/tz.py`,
  `tests/test_util/test_tz.py`, `tests/test_autonomy/test_protection.py`,
  `tests/test_hooks/test_inline_hooks.py`, and `tests/conftest.py`.
  Restores Phase 6 sanitizer correctness and clears 11 public CI failures.
- **Flaky `caplog` assertion in `test_dispatch_unknown_falls_back_to_dual`.**
  Commit `0ad9567` had previously removed the exact same assertion
  because caplog's logger-name filter interacts with other tests' logger
  configuration under the full suite; commit `3bbae15` re-introduced it
  in the F1 dispatch routing wiring. Dropped the log-message sniff again;
  kept the behavioural fallback assertion.
- **Telegram adapter refuses to start with empty / invalid
  `TELEGRAM_ALLOWED_USERS`.** Cherry-picked from community PR
  `WingedGuardian/GENesis-AGI#29`. Previously the bot would start silently
  and allow messages from **all** users when `allowed_users` was empty or
  contained only invalid UIDs (e.g. someone pasting a bot token into the
  wrong field). Dashboard `PUT /api/genesis/secrets` now rejects values
  containing `:` (looks like a bot token) or non-numeric IDs with a clear
  error pointing to `@userinfobot`. `secrets.env.example` documents the
  expected format for each Telegram field.

### Known Issues (tracked as follow-ups, not blocking this hotfix)

- `tests/test_runtime/test_runtime_retriever.py::test_retriever_created_after_bootstrap`
  fails only in GH Actions CI (passes locally on 2026-04-11) — suspected
  test isolation / mock state pollution under the full suite. Filed as a
  follow-up investigation; does not affect runtime behaviour.
- `tests/test_qdrant/test_collections.py` has no `skipif` fixture and
  errors (not fails) when Qdrant isn't running on `localhost:6333`.
  Separate hotfix will add a module-level fixture that pings the port and
  skips the suite with a clear message on `ConnectionError`.

---

## [v3.0a3] - 2026-04-11

Large release. Major new features: **community contribution pipeline**
(Phase 6), **Sentinel** container-side guardian, **self-update infrastructure**,
and a top-to-bottom overhaul of the install experience, Guardian recovery,
approval UX, and the neural monitor dashboard. Also clears a long tail of
runtime, routing, and observability issues accumulated since v3.0a2-hf5.

### Added

**Community contribution pipeline (Phase 6)**

- **`genesis contribute <sha>` CLI** — one-shot pipeline that converts a
  `fix:` commit into a draft PR against the public Genesis repo. Flow:
  divergence check → version gate → sanitizer → adversarial review →
  consent prompt → draft PR via `gh`. Pseudonymous by default
  (`contributor-<id>@genesis.local`); `--identify` uses the user's real
  git identity. MVP scope: bug fixes only (`--allow-non-fix` to override).
- **Post-commit offer hook** — committing a `fix:` commit drops a marker
  in `~/.genesis/pending-offers/`; the `contribution_offer_hook.py`
  UserPromptSubmit hook injects a `[Contribution]` system-reminder on
  the next prompt so Genesis can proactively offer to upstream the fix.
  `fix(local):` scope opts out of the offer entirely.
- **Fail-closed sanitizer** — refuses any diff containing secrets, personal
  email addresses, hardcoded IPs, `/home/ubuntu` paths, or files on the
  `contribution_forbidden` tier of `config/protected_paths.yaml`. Runs
  `detect-secrets`, portability, and path-tier scanners.
- **Adversarial review chain** — Codex CLI first, Claude Code subagent
  fallback, Genesis-native reviewer last. First-success wins; result is
  embedded in the PR body.
- **PR body metadata** — every generated PR includes contributor install
  version (`<version>@<short-sha>`), version drift status, pseudonymous
  install ID, sanitizer finding count + scanners run, and review result.
- **Branch-push flow** — contributions land on a fresh branch named by
  commit sha, pushed to the contributor's fork. E2E CLI test covers the
  full hook → sanitizer → review → branch-push path.

**Sentinel (container-side guardian)**

- **New package `src/genesis/sentinel/`** — container-side complement to
  the host-side Guardian. Runs inside the container, monitors Genesis
  infrastructure with the fire alarm taxonomy (WARN / DEGRADED / DOWN),
  and triggers dormant remediations via the registry.
- **Trigger sources + infrastructure monitor** — wires Qdrant, database,
  memory, and process health into the Sentinel trigger pipeline.
- **Runtime wiring + capability registration** — Sentinel registers as a
  first-class capability, surfaces state in the dashboard Services card,
  and its awareness is folded into Guardian briefings + diagnosis.
- **V4 architecture §8.1/8.2 updated** with implementation status.

**Self-update infrastructure**

- **`GenesisVersionCollector`** — awareness-loop collector checks for
  upstream updates every 6h (configurable), stores observations, sends
  Telegram alerts, surfaces "update available" in the dashboard health
  panel, and detects update failures.
- **Update settings domain** — new `config/updates.yaml` with check
  interval, notification channel, and auto-apply policy (opt-in only).
  Configurable via `settings_update("updates", ...)` MCP tool.
- **Schema migration framework** — `src/genesis/db/migrations/` with
  `MigrationRunner`, CLI (`python -m genesis.db.migrations`), and
  versioned migration files. Tracking table `schema_migrations` records
  applied migrations. First migration: `update_history` table.
- **Public release CI/CD** — `.github/workflows/public-release.yaml`
  triggered on version tags. Runs `prepare-public-release.sh`, secret
  scan, portability scan, and uploads sanitized artifact for maintainer
  review.
- **`detect-secrets` dependency** — added to `[release]` optional deps in
  `pyproject.toml`, unblocking the secret scan step that was previously
  silently skipping.

**Install & host setup**

- **13 resilience fixes from failure-mode audit** — hardens `install.sh`,
  `bootstrap.sh`, and `update.sh` against partial failures, rerun damage,
  missing preconditions, and silently-skipped steps.
- **Container smoke test + damage detection on re-run** — re-running the
  installer now detects a damaged previous run and either repairs or
  fails loudly instead of silently producing a broken state.
- **Tailscale in host setup** — `host-setup.sh` installs Tailscale and
  prompts for authentication during setup (supports `TAILSCALE_AUTH_KEY`
  for unattended installs).
- **Node.js + Claude Code on host VM** — `host-setup.sh` installs Node.js
  20.x and Claude Code on the host (not just the container), enabling
  Guardian CC diagnosis sessions.
- **Node.js ≥ 20** required (was 18); Guardian state reset on container
  recreate.

**Approval UX & autonomy**

- **Approval UX redesign** — dedicated Telegram topic, inline buttons,
  call-site gating so approvals are attributed to the caller, not the
  model. Batch CLI approval flow.
- **Autonomous CLI approval gate wired into standalone server** — gate +
  `approvals` topic registered during standalone startup (previously
  only wired in the AZ hosting mode, silently disabled standalone).
- **Inbox approval-pending resume flow** — stable approval key + resume
  path so restarts don't orphan in-flight approvals.

**Dashboard & observability**

- **Neural monitor visual overhaul** — glowing dots, cleaner layout,
  proportional radial placement, constellation map layout option,
  provider chain fixes. Dispatch mode toggle wired to runtime routing
  with save-verify feedback.
- **Sentinel state in Services card.**
- **Config tab UX overhaul** — visibility, dropdowns, tooltips, health
  indicators. Secret values gated behind auth.
- **Dropped-tick events surfaced** from the awareness loop.
- **Container memory decomposition** into anon/file/kernel components.
- **`runtime.peek()`** — read-only runtime snapshot used by observability
  callers that previously forced full runtime access.

**Docs & conventions**

- **No-silent-timeouts rule** added to `CLAUDE.md` — new timeouts on
  reflections, CC calls, and long-thinking paths require explicit user
  approval with evidence of a real failure mode.
- **Never ignore a bug** rule — bugs encountered in any work must be
  fixed inline or tracked as follow-ups; "out of scope" is not an option.
- **V4 ego / infra self-monitor design** + incident report.

### Changed

- **`update.sh` overhaul** — pre-update backup via `backup.sh`, rollback
  tags (`pre-update-{timestamp}`), idempotent `bootstrap.sh` post-pull
  (replaces manual pip install), health verification with 3× retry,
  automatic rollback on failure, CC-assisted recovery context file
  (`~/.genesis/last_update_failure.json`).
- **Mistral routing rationalization** — consolidated Mistral providers
  and call sites, raised `mistral-large-free` rpm 2 → 4 and
  `mistral-small-free` rpm 2 → 30 based on observed usage (previous
  limits were ~5× over-conservative).
- **Routing tail fallback** added for sites 29 and 35, stopping the
  sentinel DOWN alarm from chain exhaustion.
- **Proactive DLQ orphan scan** on routing config reload — expires DLQ
  items whose `call_site_id` no longer exists instead of leaving them
  stranded.
- **Misinterpreted memory-backlog signal removed end-to-end** from the
  awareness loop (was firing on normal state).
- **Watchdog staleness threshold** 300s → 900s to stop false positives
  during legitimate long-running ticks.
- **`runtime/_core.py` split** under the 600 LOC soft target — converted
  to `runtime/` package with 20 init modules. Extracted mixins:
  `_properties.py`, `_pause_state.py`, `_init_delegates.py`,
  `_degradation.py`, `_capabilities.py`, `_job_health.py`. Re-exported
  from `__init__.py` for backward compatibility.
- **`ashutdown`** async shutdown path + `job_health` envelope for MCP
  health surfaces.
- **`8_memory_consolidation`** call site renamed to `8_ego_compaction`
  for clarity.
- **Ego sessions** remain inert until beta — built but not registered
  in bootstrap.

### Fixed

- **Guardian recovery hardening** — auth middleware was blocking
  Guardian's own health probes, contributing to the 2026-04-08 memory
  exhaustion incident. Auth now gates browser pages only; `/api/` and
  `/v1/` routes are exempt. See `docs/incidents/2026-04-08-memory-exhaustion.md`.
- **Broken page cache reclaim** — watchdog/Guardian collector service
  name fix plus explicit reclaim trigger; container no longer drifts
  toward OOM under sustained read load.
- **Guardian heartbeat decoupled from HEALTHY state** — previously,
  Guardian only emitted heartbeats while reporting HEALTHY, so DEGRADED
  or DOWN states silently stopped the heartbeat stream.
- **Guardian ICMP probe** retries once to absorb bridge ARP races that
  were producing spurious DOWN readings on container recreate.
- **Runtime status writer decoupled from awareness tick** — a slow tick
  no longer blocks status writes, and a slow status write no longer
  delays the next tick.
- **`surplus.py` zombie runtime singleton** — the surplus worker was
  spawning a parallel Genesis runtime in-process when the primary
  runtime's observability snapshot asked for state. Fixed by routing
  through `runtime.peek()`.
- **Circular import crashing `genesis-memory` MCP** — resolved, with
  loud failure reporting instead of the previous silent-skip behavior.
- **Browser tools converted from Playwright sync → async API** —
  sync-in-async-context was deadlocking the MCP server.
- **`TopicManager` wired into standalone startup path** (was only
  wired in AZ mode, silently missing in standalone).
- **IPC non-dict response wrapping** — `module_call` no longer returns
  a bare list when a module returns one; wrapped consistently so
  callers don't need to handle both shapes.
- **Inbox routing** — removed free-SLM routing path, kept approval gate,
  fixed empty-content bug that was dropping messages.
- **Autonomous DM silent fallback surfaced** — fallback path used to
  silently succeed with no user visibility; now surfaces the fallback
  and doesn't stall reask on fail.
- **`update.sh` rollback correctness** — rollback used `git checkout <tag>`
  which left the repo in detached HEAD. Now `git checkout main &&
  git reset --hard <tag>` preserves the branch. Silent failure paths
  (`|| true`) removed, ERR trap covers all mutating steps, health
  endpoint + migration failures now trigger rollback. Added worktree
  guard (refuses to run from `.claude/worktrees/`). `update_history`
  rows written on both success and failure.
- **Migration runner atomicity** — body + tracking row were committed
  separately (risk of "applied but unrecorded"), and Python sqlite3
  auto-commits before DDL when using `db.commit()`/`db.rollback()`.
  Fixed with explicit `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK` SQL
  including DDL in the transaction. Regression test added.
- **Public release CI secret scan** — `detect-secrets` failures were
  silenced by `|| true`, bare `except: print(0)`, and `2>/dev/null`,
  converting scanner crashes into "0 findings" (false PASS). Now fails
  loudly.
- **`GenesisVersionCollector`** — `_check_upstream` silently returned
  `(0, "")` on git fetch failure. Now raises with stderr context.
  Local update resolves prior `genesis_update_available` observations
  so dashboard alert clears immediately. Failure file archived to
  `.processed.json` after processing instead of being re-read every
  awareness tick.
- **Updates settings validator** — non-dict sections silently passed;
  `auto_apply.allowed_impacts` accepted `action_needed` and `breaking`
  despite config comment saying those always require manual approval.
  Both now rejected.
- **Observability** — `errors.py` data-returning paths now log at ERROR
  with `exc_info=True` (dead letter query, circuit breaker check, event
  log query, genesis update alert query). One wrong log message fixed.
- **Health MCP** — hermetic cleanup rounds 2 + 3, transport smoke
  canary expanded to full read-only matrix, heartbeat query error
  raised DEBUG → ERROR, narrow error handling, tighter bootstrap
  manifest messages, worktree test isolation fix in `conftest.py`.
- **Test suite** — cleared 26 pre-existing test failures; root-caused
  test pollution; added 31 new tests for version collector, migration
  runner atomicity, and settings validator edge cases.
- **`SMOKE_FAIL` unbound var** in install scripts.
- **Integer pixel margins** for neural monitor periphery dots (were
  rendering blurry on fractional values).

### Known Limitations

- **Phase 6 MVP is bug-fixes-only.** Feature contributions are blocked
  by the version gate unless `--allow-non-fix` is passed explicitly.
- **Ego sessions remain inert.** Built but not registered in bootstrap;
  will be wired when the autonomous proposal pipeline is ready for
  live use.

---

## [v3.0a2-hf5] - 2026-04-07

### Added

- **Tailscale in host setup** — `host-setup.sh` now installs Tailscale and
  prompts for authentication during setup. Headless server users get an
  immediately usable dashboard URL on their tailnet without SSH tunneling.
  Supports `TAILSCALE_AUTH_KEY` env var for CI/unattended installs.
- **Node.js + Claude Code on host VM** — `host-setup.sh` now installs
  Node.js 20.x and Claude Code on the host VM (not just inside the
  container), enabling Guardian CC diagnosis sessions and direct host
  interaction from day one.

### Changed

- **Guardian framing** — Guardian is no longer framed as optional. Install
  failures now show a prominent box identifying Guardian as a core subsystem
  (health monitoring, diagnosis, recovery) that must be fixed. Final setup
  report reworded: Guardian is "always running"; Claude Code auth enables
  agentic diagnosis as an add-on, not as the thing that "enables" Guardian.

---

## [3.0a2-hf4]

### Fixed

- **GCP split-disk install** — on cloud VMs where `/home` is a separate
  larger disk than `/`, Incus now stores container data under
  `/home/incus-data` instead of the root partition. Disk check validates
  the actual Incus storage location and requires 15GB free.
- **Guardian pip bootstrap** — Debian creates venvs without pip even when
  `ensurepip` imports successfully (module is present but non-functional).
  Guardian now detects missing pip post-venv and bootstraps via
  `ensurepip --upgrade` or `get-pip.py`.

---

## [3.0a2-hf3]

### Added

- **Provider Keys panel** — write-only secrets management in Settings tab.
  Shows configured/not_set status for all 39 API keys across 7 groups parsed
  from `secrets.env.example`. Values are never returned by the API. Atomic
  file writes (tempfile + os.replace), chmod 600, immediate env reload.
- **Config tab UX** — human-readable labels, tooltips, dropdowns for enum
  settings (provider, model, effort, channels), proper domain name display.
  Replaced all underscore identifiers and free-text fields that need exact values.

### Fixed

- **Install portability** — `install_guardian.sh` now auto-detects host Python
  version and installs the matching `python3.X-venv` package if missing.
  Supports Debian 12 (Python 3.11) — Guardian only needs pyyaml, no 3.12
  requirement on the host VM.
- **Container venv** — `host-setup.sh` tries `python3.12-venv` first, falls
  back to `python3-venv` for distros that don't package them separately.
- **Network identity in CLAUDE.md** — `update.sh` now detects and rewrites
  unresolved template variables (`${CONTAINER_IP:-localhost}` etc.) with
  real IPs from the running container and guardian_remote.yaml.
- **Pre-commit hook** — `secrets.env.example` was blocked by the secrets
  file filter (regex matched `secrets\.env` before the `.example` suffix).
  Now explicitly allows `.example` files through.

---

## [3.0a2-hf2]

### Added

- **Dashboard authentication** — optional password-based access control for the
  dashboard. Set `DASHBOARD_PASSWORD` in secrets.env to enable. Cookie-based
  30-day sessions, rate-limited login (5 attempts/5-min lockout), logout button.
  When no password is set, dashboard works as before (backward compatible).
- **Install UX overhaul** — welcome/recovery banners, contextual CC login
  prompts (explains Genesis vs Guardian purpose), `genesis` shell alias for
  convenient container access from host
- **Dashboard accessibility** — Incus proxy device forwarding host:5000 →
  container:5000, network topology detection (IPv4/IPv6/Tailscale), SSH
  tunnel and Tailscale guidance in post-install report
- **Network identity** — container and host IPs (v4 + v6) persisted in
  CLAUDE.md for both Genesis and Guardian; guardian-gateway appends network
  section on code updates
- **Guardian onboarding** — interactive CC login prompt during install,
  network section in Guardian CLAUDE.md
- **Uninstall script** — `scripts/uninstall.sh` for clean removal

### Fixed

- **Services not starting after install** — `genesis-server` was enabled but
  never started; service gate blocked enable/start on re-runs. Now
  unconditionally enables and starts both services
- **Dashboard unreachable from browser** — container IP not routable from
  external network; proxy device now forwards host port
- **`/setup` not found on new installs** — CC discovers slash commands from
  project root; users landing in `~` couldn't find `.claude/commands/`.
  Auto-cd to `~/genesis` on login fixes this
- **Install final output** — removed stale "start services manually" step
  (services auto-start now), shows actual service status, simplified guidance
- **Guardian stuck in CONFIRMED_DEAD** — state machine never checked if
  signals recovered; container could be perfectly healthy while Guardian
  reported it as dead indefinitely. Now auto-recovers when all signals
  return to healthy
- **Neural monitor false green for unconfigured providers** — health probe
  hit unauthenticated `/models` endpoint for providers with `base_url` but
  no API key (e.g., GLM5/Zenmux), getting HTTP 200 and reporting "reachable"
- **CC auto-updater nag** — disabled for pinned versions via
  `DISABLE_AUTOUPDATER` in project settings

---

## [3.0a2-hf1]

### Added

- **User model enrichment** — three-tier user model (identity, preferences,
  knowledge) with unified knowledge pipeline feeding reflection and conversation
- **CI workflow** — ruff lint + pytest with advisory test gate

### Fixed

- **Terminal**: WebSocket compatibility with simple_websocket >=1.0 (returns
  None on timeout instead of raising TimeoutError)
- **CC invoker**: Handle missing claude CLI gracefully (FileNotFoundError)
- **Dependencies**: Pin wsproto>=1.2 (flask-sock transitive dep)
- **Dashboard**: Stale CC status display, degradation calculation, circuit
  breaker backoff timing
- **CI**: Scope lint to src/tests/scripts, ignore preserved AZ-era test files,
  make test job non-blocking while stabilizing
- **Lint**: Resolve all ruff errors (unused vars, unsorted imports, SIM105)

---

## [3.0a2]

### Changed

- **Standalone-only architecture** — Agent Zero fully removed. Genesis runs as
  a standalone server (`python -m genesis serve`) with its own dashboard,
  terminal, and API. AZ can still be used as an optional external agent
  framework via the adapter interface, but is no longer required or bundled.
- **OpenClaw gateway** — Genesis exposes `POST /v1/chat/completions` so OpenClaw
  (or any OpenAI-compatible router) can route channels through it
- **SDK-primary engine routing** — Claude SDK API is the primary execution path;
  Claude Code subprocess is optional based on operator preference

### Added

- **Neural monitor overhaul** — provider probes, subsystem grouping, circuit
  breaker wiring, detail panel with live backend data, warning severity color,
  subsystem sector clustering, visual redesign (larger diagram, refined colors),
  call site triage with naming consistency
- **Settings UX** — human-readable labels, tooltips, channel dropdown
- **Chain editor** — CC entries editable, repositionable, and removable
- **Autonomy enforcement** — data-driven RuleEngine with graduated enforcement
  spectrum (inform → guide → guard → block), SteerMessage abstraction
- **Anti-vision identity boundaries** — selective MCP loading, executor plan
  directive for content evaluation
- **User-evaluate skill** — evaluate content through Genesis's user model
- **update.sh** — pull, sync dependencies, restart services in one command

### Fixed

- **host-setup.sh**: Fix container networking on cloud VMs (GCP, AWS, Azure) —
  UFW `deny (routed)` default policy was blocking all forwarded container traffic
  (DNS, HTTPS). Script now adds `ufw route allow` rules for the Incus bridge.
  Also adds nftables accept rules as defense-in-depth for non-UFW distros.
- **host-setup.sh**: Auto-activate `incus-admin` group after Incus install —
  script previously exited with a permission error, requiring manual
  `newgrp incus-admin` to recover
- **host-setup.sh**: Fail fast on prerequisite install or git clone errors
  instead of continuing to "Genesis is ready" with a broken container
- **host-setup.sh**: Add ERR trap with line number, command, and exit code on
  any failure; `DEBUG=1` enables full `set -x` tracing
- **host-setup.sh**: Enable IP forwarding and bridge NAT before container
  creation; show progress during package installation
- **Dashboard**: uptime counter timezone bug, restart button self-restart,
  post-AZ-removal regressions, probe override guard, detail panel staleness,
  degraded status color visibility
- **Routing**: CC-only model saves silently dropped + input validation missing
- **update.sh**: Use `--rebase` to avoid divergent-branch errors on pull
- **Terminal**: Prefill CC command without auto-executing (user chooses when)
- **push-public-release.sh**: Create tag and GitHub Release even when content
  was already pushed (previously exited early, skipping the release step)
- **install.sh**: Add `cd ~/genesis &&` to headless login instructions so
  first-time users run `claude login` from the correct directory

---

## [v3.0a] - 2026-04-03

Genesis v3 — complete autonomous agent system. First public release.
All Phase 0–9 subsystems built, wired, and tested.

### Added

- **Memory system** — hybrid Qdrant vector + SQLite FTS5 search, episodic memory
  with session provenance, proactive memory injection at session start
- **Telegram integration** — resilient polling adapter with text, voice, photo,
  and document support; supergroup/forum topic routing; streaming responses
  via edit-based drafts; voice transcription via Whisper
- **Morning reports** — daily system state digest via Telegram with configurable
  structure and LLM-generated synthesis
- **Guardian** — host-VM watchdog with agentic Claude Opus diagnosis, briefing
  bridge, credential bridge, and shared filesystem mount
- **MCP servers** — memory recall, outreach queue, health status, and recon
  tools exposed as MCP endpoints for foreground Claude Code sessions
- **Outreach pipeline** — category-based message routing (alerts, digests,
  surplus, recon), engagement tracking, morning report scheduler
- **Reflection system** — background micro/light/deep/strategic reflection
  sessions with consolidation into episodic memory
- **Dual-repo distribution** — private working repo + public GENesis-AGI release
  with automated stripping of user-specific content
- **Dashboard** — web UI with system health, session management, built-in
  terminal, settings hub
- **Standalone server** — `python -m genesis serve` runs dashboard, API, and
  all subsystems; adapter protocol for provider-agnostic operation
- **Model routing** — configurable per-call-site routing with fallback chains,
  cost tracking, and provider health monitoring
- **Inbox monitor** — filesystem inbox for asynchronous task ingestion
- **Knowledge graph** — observation/finding/pattern storage with deduplication
- **Ego session framework** — autonomous proposal pipeline (inert until beta)
- **Hooks system** — PreToolUse/PostToolUse guards for behavioral enforcement
  (blocking pip editable installs to worktrees, validating kill signals, etc.)
- **Bootstrap script** — idempotent machine setup: venv, secrets, systemd
  services, Claude Code config generation

### Breaking

- Requires Python 3.12 and Ubuntu 22.04+
- `secrets.env` must be populated with API keys before first run
- Telegram bot token required for channel features
- Qdrant must be running locally (`localhost:6333`)

---

<!-- Template for future releases:

## [vX.Y] - YYYY-MM-DD

### Added
### Changed
### Fixed
### Breaking

-->
