# Genesis Codebase Map

Quick reference for how Genesis is structurally organized, where to find
things, and what trips developers up repeatedly.

## Package Map (`src/genesis/`)

| Package | Purpose | Status |
|---------|---------|--------|
| `runtime/` | Core orchestrator — GenesisRuntime singleton, bootstrap, 20+ init modules, capabilities | ACTIVE |
| `db/` | SQLite schema (60+ tables), CRUD modules, migrations, WAL mode | ACTIVE |
| `awareness/` | 5-minute ticker loop, signal collection, depth classification, urgency | ACTIVE |
| `memory/` | Hybrid store (Qdrant vector + SQLite FTS5), ACT-R activation, retrieval | ACTIVE |
| `learning/` | Triage pipeline, procedural memory harvest, signal collectors, skill evolution | ACTIVE |
| `reflection/` | Deep/strategic reflection scheduler, observation creation | ACTIVE |
| `perception/` | Reflection engine — context assembling, LLM calling, output parsing | ACTIVE |
| `routing/` | LLM provider fallback chains, circuit breakers, cost tracking, dead-letter queue | ACTIVE |
| `surplus/` | Idle detector, compute scheduler, brainstorm tasks | ACTIVE (executor is stub) |
| `cc/` | Claude Code integration — invoker, session manager, checkpoints | ACTIVE |
| `observability/` | Event bus, structured logging, health probes, neural monitor UI | ACTIVE |
| `outreach/` | Governance gate, draft/deliver pipeline, engagement tracker, scheduler | ACTIVE |
| `autonomy/` | Levels L1-L4, action classifier, approval flow, task verifier | ACTIVE |
| `skills/` | 20+ internal skills (evaluate, research, osint, forecasting, etc.) | ACTIVE |
| `ego/` | Ego sessions, adaptive backoff, circuit breaker | **INERT** |
| `providers/` | Provider registry (web search, STT, TTS, embeddings, health) | ACTIVE |
| `research/` | Web search + URL fetch via SearXNG+Brave, Gemini YouTube routing | ACTIVE |
| `mail/` | Gmail monitor (weekly poll), two-layer triage (Gemini + CC) | ACTIVE |
| `inbox/` | Inbox scanner (~inbox/), markdown evaluation, CC dispatch | ACTIVE |
| `channels/` | Message delivery adapters (Telegram, OpenClaw) | ACTIVE |
| `contribution/` | User contribution pipeline (`genesis contribute <sha>`) | Phase 6 |
| `resilience/` | CC budget tracking, circuit breakers, fault recovery | ACTIVE |
| `qdrant/` | Vector DB wrapper (1024-dim, hybrid search) | ACTIVE |
| `mcp/` | MCP servers: health, memory, recon, outreach | ACTIVE |
| `guardian/` | External host VM monitoring | ACTIVE |
| `sentinel/` | Container-side autonomous diagnosis | ACTIVE |
| `modules/` | Capability module registry (domain add-ons) | Skeleton |
| `content/` | Content formatter and drafter | Partial |
| `util/` | Process locks, common utilities | ACTIVE |

## Bootstrap Init Order (`runtime/init/`)

The order matters — reordering breaks initialization. Defined by the
export order in `runtime/init/__init__.py`.

1. **secrets** — Load API keys from `secrets.env`
2. **db** — Initialize SQLite, create tables (WAL, foreign keys ON)
3. **observability** — Event bus, logging, health probes
4. **providers** — Provider registry (embeddings, health, research)
5. **modules** — Module registry
6. **awareness** — 5-min ticker loop, signal collectors
7. **router** — LLM routing with fallback chains, circuit breakers
8. **perception** — Reflection engine
9. **cc_relay** — Claude Code invoker, session manager
10. **memory** — Hybrid store (Qdrant + SQLite + FTS5)
11. **pipeline** — Signal collection + triage assembly
12. **surplus** — Idle detector, compute scheduler
13. **learning** — Triage, procedural memory, calibration
14. **reflection** — Reflection scheduler (deep/light/strategic)
15. **inbox** — Inbox monitor (APScheduler)
16. **mail** — Gmail monitor
17. **health_data** — Aggregate subsystem health
18. **outreach** — Governance + scheduler
19. **autonomy** — Levels, classifier, approval gates
20. **tasks** — Task executor (autonomous multi-step)

## Key Entry Points

| Entry Point | Location | Role |
|---|---|---|
| `python -m genesis serve` | `__main__.py` | Standalone server startup |
| `StandaloneAdapter.bootstrap()` | `hosting/standalone.py` | Calls `GenesisRuntime.bootstrap()` |
| `GenesisRuntime.bootstrap()` | `runtime/_core.py` | Init all 20+ subsystems in order |
| `AwarenessLoop.perform_tick()` | `awareness/loop.py` | 5-min heartbeat triggering reflection |
| `ReflectionEngine.run()` | `perception/engine.py` | Context -> LLM -> output |
| `CCInvoker.invoke()` | `cc/invoker.py` | Dispatch to Claude Code |
| `OutreachScheduler.run()` | `outreach/scheduler.py` | 4 APScheduler jobs |
| `TaskExecutor.execute()` | `autonomy/executor.py` | Multi-step with approval gates |

## Gotchas (Things That Trip Developers Up)

### 1. Database Path

`genesis.db` is at `~/genesis/data/genesis.db`, NOT `~/genesis/genesis.db`.
Always resolve via `genesis.env.genesis_db_path()`. The DB has 60+ tables —
use `db_schema` MCP before assuming column names.

### 2. Ego Sessions Are Inert

`src/genesis/ego/` has real data structures (session.py, cadence.py,
proposals.py, compaction.py, dispatch.py) but ZERO production callers.
Don't wire them, don't treat them as broken. Waiting for beta.

### 3. Capabilities Manifest Is Write-Once

`~/.genesis/capabilities.json` is written once at bootstrap by
`_capabilities.write_capabilities_file()`. It's NOT dynamic or queryable
during runtime. New capabilities need registration in
`_CAPABILITY_DESCRIPTIONS` + a bootstrap init step.

### 4. Some Signal Collectors Are Stubs

Some signal collectors have real implementations (BudgetCollector,
ConversationCollector, etc.) while others are still stubs with interfaces
but no real data production. Code that looks complete may not actually
produce useful signals — check for actual data flow before assuming.

### 5. Embedding Fallback Is Silent

Embedding provider: cloud-primary (Mistral, DeepInfra). Local Ollama is
optional and install-specific. If the configured provider is down, falls
back silently to next in chain. If all providers fail, vector search
fails silently. FTS5-only fallback exists but degrades recall.

### 6. CircuitBreaker State Persistence Gap

State is persisted in SQLite but the in-memory CircuitBreakerRegistry
doesn't auto-reload on bootstrap. Can lead to stale state if Genesis
restarts mid-fault.

### 7. Auth-Exempt Paths Are Easy to Miss

New health/monitoring endpoints must be added to the auth-exempt list.
Incident 2026-04-08: `/api/genesis/heartbeat` wasn't exempt, causing
Guardian heartbeat 401s, cascade restart, memory pressure.

### 8. Dead Code in Autonomy

`AutonomyManager.record_success()` and `record_correction()` have Bayesian
regression methods built. Zero production callers — wired but not called.
Same with enforcement spectrum: RuleEngine + SteerMessage exist but need
integration.

### 9. USER.md Evolution Bug

Successful `user.md` updates are sometimes recorded as failed in
`cognitive_state`. The model IS actually updated — the logging is wrong.

### 10. Guardian Checks Deprecated Service

Guardian checks for `genesis-bridge` (deprecated) instead of
`genesis-server` (active). Affects host-side monitoring; container-side
Sentinel is correct.

### 11. Procedural Memory Not Wired to Debrief

`genesis.learning.procedural` has full harvest pipeline but it's not called
in `cc_relay` debrief. Only basic memory writing happens. Decision ->
outcome -> rule extraction not yet flowing.

### 12. Surplus Executor Is a Stub

`SurplusScheduler` fires on 12h cadence but `StubExecutor` does nothing.
Queue fills up; tasks don't execute.

## Debugging Ladder

When Genesis breaks, check in this order:

1. **Systemd status**: `systemctl --user status genesis-server`
2. **Logs**: `journalctl --user -u genesis-server --since '1h ago'`
3. **Observations**: Query `genesis.db` observations table for recent
   errors
4. **Capabilities**: Check `~/.genesis/capabilities.json` — is the
   subsystem registered? Did bootstrap succeed?
5. **Bootstrap trace**: Read `runtime/_core.py` bootstrap method — did
   init steps run in order?
6. **Hooks**: Check `.claude/settings.json` — is the hook registered?
   Does the script exist and have correct permissions?
7. **Auth**: Is the endpoint in the auth-exempt list if it's a health
   or API endpoint?
8. **Circuit breakers**: Check breaker state in DB — stuck in OPEN
   from a previous fault?

## Non-Obvious Dependencies

- **Awareness -> Perception**: Tick triggers reflection engine
- **Memory -> CC Relay**: Checkpoint manager uses MemoryStore for context
- **Learning -> Outreach**: Procedural harvest feeds skill evolution,
  which gates outreach
- **Guardian <-> Sentinel**: Bidirectional — external host monitoring +
  container-side diagnosis
- **Autonomy -> Approval Inbox**: Requests live in message_queue, gate
  polls inbox

## Surprisingly Important Small Files

| File | Why |
|------|-----|
| `src/genesis/env.py` | All path resolution — changing one breaks many subsystems |
| `src/genesis/runtime/_capabilities.py` | Subsystem manifest CC sessions read at startup |
| `config/model_routing.yaml` | Router fallback chains + cost budgets for every LLM call |
| `config/autonomy_rules.yaml` | Enforcement spectrum (W1-W7 behavioral correction) |
| `src/genesis/db/schema/_tables.py` | 60+ table definitions — every query depends on this |
| `src/genesis/runtime/init/__init__.py` | Export order = bootstrap sequence |
| `secrets.env` | API keys for 10+ services — missing one = silent degraded mode |
