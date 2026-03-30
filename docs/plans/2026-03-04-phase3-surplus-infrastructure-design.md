# Phase 3: Surplus Infrastructure — Design

**Date:** 2026-03-04
**Phase:** V3 Phase 3
**Risk:** LOW-MODERATE
**Dependencies:** Phase 0 (data foundation), Phase 2 (compute routing)

---

## Summary

Phase 3 builds the infrastructure for Genesis to use free compute during idle
cycles. V3 ships conservative (2 brainstorm sessions/day with stub executors).
The architecture is designed to support V4's aggressive surplus mode: multi-model
sweeps, self-unblocking, systematic memory auditing, and anticipatory research.

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Idle detection | Timer-based | No AZ runtime hooks needed. Track last user interaction, idle after 15 min configurable. |
| GPU availability | Active ping (LM Studio) | GPU machine is a "spot instance" with no predictable schedule. Ping `http://${LM_STUDIO_HOST:-localhost:1234}/v1/models` to check. |
| Brainstorm execution | Stub in V3 | Phase 3 is infrastructure only. Stub executor generates structured placeholders. Real LLM calls come in Phase 4. |
| Scheduler | Own APScheduler instance | Independent lifecycle from Awareness Loop. Foundation for V4 aggressive mode with parallel dispatch. |
| Surplus scale | Conservative V3, aggressive V4 | V3: 2 brainstorms/day, single task dispatch. V4: continuous processing, multi-model diversity, two-stage filtering. |

## Compute Landscape

| Endpoint | Location | Hardware | Role | Surplus? |
|----------|----------|----------|------|----------|
| Ollama 3B | `${OLLAMA_URL:-localhost:11434}` (container) | CPU | Embeddings + extraction for active ops | **NO** — not a surplus target |
| Ollama embedding | `${OLLAMA_URL:-localhost:11434}` (container) | CPU | Embedding model | **NO** |
| LM Studio 30B | `${LM_STUDIO_HOST:-localhost:1234}` (separate machine) | Consumer GPU | 20-30B inference | Yes — secondary, spot availability |
| Gemini free | Cloud | — | Free API tier (~10-30 calls/day) | Yes — primary surplus workhorse |
| Groq free | Cloud | — | Free API (30 rpm) | Yes — primary |
| Mistral free | Cloud | — | Free API (2 rpm) | Yes — primary |
| OpenRouter free | Cloud | — | Free API (20 rpm) | Yes — primary |
| Sonnet+ | Cloud | — | Paid models | **NEVER** for surplus |

**Key insight:** Surplus primarily targets free cloud APIs, not local models.
Ollama is reserved for active operations. LM Studio is secondary (spot).

## Component Architecture

```
genesis/surplus/
├── __init__.py
├── types.py                # SurplusTask, TaskType, ComputeTier, ExecutorResult
├── idle_detector.py        # IdleDetector — timer-based
├── compute_availability.py # ComputeAvailability — LM Studio ping, tier tracking
├── queue.py                # SurplusQueue — priority queue backed by surplus_tasks table
├── executor.py             # SurplusExecutor protocol + StubExecutor (V3)
├── brainstorm.py           # BrainstormRunner — schedules 2/day sessions
└── scheduler.py            # SurplusScheduler — own APScheduler, orchestrates dispatch
```

### Types (`types.py`)

```python
class TaskType(StrEnum):
    BRAINSTORM_USER = "brainstorm_user"
    BRAINSTORM_SELF = "brainstorm_self"
    META_BRAINSTORM = "meta_brainstorm"
    # GROUNDWORK(v4-surplus-tasks):
    MEMORY_AUDIT = "memory_audit"
    PROCEDURE_AUDIT = "procedure_audit"
    GAP_CLUSTERING = "gap_clustering"
    SELF_UNBLOCK = "self_unblock"
    ANTICIPATORY_RESEARCH = "anticipatory_research"
    PROMPT_EFFECTIVENESS_REVIEW = "prompt_effectiveness_review"

class ComputeTier(StrEnum):
    LOCAL_30B = "local_30b"       # LM Studio, spot
    FREE_API = "free_api"         # Gemini, Groq, Mistral, OpenRouter
    CHEAP_PAID = "cheap_paid"     # Overflow only (V4)
    NEVER = "never"               # Sonnet+ — rejected at enqueue

class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

`SurplusTask` dataclass: task_type, compute_tier, priority (0.0-1.0),
drive_alignment, status, payload (JSON), created_at, attempt_count.

`ExecutorResult` dataclass: success (bool), content (str | None),
insights (list of staging entries), error (str | None).

`SurplusExecutor` protocol: `async def execute(task: SurplusTask) -> ExecutorResult`

### Idle Detection (`idle_detector.py`)

- `mark_active()` — called when user interaction occurs, resets timer
- `is_idle(threshold_minutes=15) -> bool` — threshold configurable
- `idle_since() -> datetime | None` — how long idle has lasted
- No AZ hooks — purely timer-based. AZ message handler calls `mark_active()`
  when Genesis integrates.

### Compute Availability (`compute_availability.py`)

- `check_lmstudio() -> bool` — HTTP GET to LM Studio endpoint, timeout 3s
- `get_available_tiers() -> list[ComputeTier]` — `FREE_API` always included
  (failures handled by Router retries); `LOCAL_30B` included if LM Studio alive
- LM Studio check cached for configurable TTL (default 60s)
- No Ollama tracking — Ollama is not a surplus target
- GROUNDWORK(v4-rate-tracking): slot for per-provider rate limit tracking

### Surplus Queue (`queue.py`)

New DB table — `surplus_tasks`:

```sql
CREATE TABLE IF NOT EXISTS surplus_tasks (
    id                TEXT PRIMARY KEY,
    task_type         TEXT NOT NULL,
    compute_tier      TEXT NOT NULL,
    priority          REAL NOT NULL DEFAULT 0.5,
    drive_alignment   TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',
    payload           TEXT,
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    completed_at      TEXT,
    result_staging_id TEXT,
    failure_reason    TEXT,
    attempt_count     INTEGER NOT NULL DEFAULT 0
)
```

Queue operations:
- `enqueue(task_type, compute_tier, priority, drive_alignment, payload) -> id`
- `next_task(available_tiers) -> SurplusTask | None` — highest priority pending
  task whose tier is in available_tiers
- `mark_running(id)` / `mark_completed(id, staging_id)` / `mark_failed(id, reason)`
- `drain_expired(max_age_hours=72)` — clean stale pending tasks
- Priority = drive_weight (from `drive_weights` table) × base_priority

Task lifecycle: `pending → running → completed | failed | cancelled`

### Brainstorm Runner (`brainstorm.py`)

- `schedule_daily_brainstorms()` — enqueues `BRAINSTORM_USER` + `BRAINSTORM_SELF`
- Idempotent: checks `brainstorm_log` for today's sessions, skips if exist
- On execution: writes placeholder to `surplus_insights` (pending promotion),
  logs to `brainstorm_log`
- V4: real executor calls Router with call site `12_surplus_brainstorm`

### Surplus Scheduler (`scheduler.py`)

Owns its own `AsyncIOScheduler` instance. Two recurring jobs:

1. **Brainstorm check** (startup + every 12 hours):
   `BrainstormRunner.schedule_daily_brainstorms()`

2. **Dispatch loop** (every 5 minutes):
   ```
   if not idle → return
   available_tiers = compute_availability.get_available_tiers()
   task = queue.next_task(available_tiers)
   if no task → return
   mark_running → execute → mark_completed/failed
   write result to surplus_insights staging
   ```

- `start()` / `stop()` for lifecycle management
- GROUNDWORK(v4-parallel-dispatch): V4 can dispatch multiple tasks concurrently
  across different providers
- Integration: starts via `DeferredTask` alongside `AwarenessLoop` when
  running inside Agent Zero

### Cost-Frequency Enforcement

| Tier | When to run surplus | Enforced by |
|------|-------------------|-------------|
| `FREE_API` | Always when idle | Queue returns these tasks first |
| `LOCAL_30B` | When idle AND LM Studio available | `get_available_tiers()` excludes when offline |
| `CHEAP_PAID` | Only for backed-up high-priority items | V4 only |
| `NEVER` (Sonnet+) | Never for surplus | `never_pays: true` on call site; queue rejects at enqueue |

## Configuration Changes

**`config/model_routing.yaml` additions:**

```yaml
providers:
  lmstudio-30b:
    type: lmstudio
    model: "TBD"
    base_url: "http://${LM_STUDIO_HOST:-localhost:1234}/v1"
    free: true
    open_duration_s: 120

surplus:
  idle_threshold_minutes: 15
  dispatch_interval_minutes: 5
  brainstorm_check_interval_hours: 12
  task_expiry_hours: 72
  max_attempts: 2
  tier_policy:
    brainstorm_user: [free_api, local_30b]
    brainstorm_self: [free_api, local_30b]
    meta_brainstorm: [free_api, local_30b]
  health_checks:
    lmstudio:
      url: "http://${LM_STUDIO_HOST:-localhost:1234}/v1/models"
      timeout_s: 3
      cache_ttl_s: 60
```

## Schema Changes

- One new table: `surplus_tasks` (added to `schema.py`)
- New indexes: `idx_surplus_tasks_status`, `idx_surplus_tasks_priority`,
  `idx_surplus_tasks_tier`
- No new seed data — queue starts empty, `BrainstormRunner` populates on first tick

## Testing Strategy (~45-50 tests)

**Unit tests:**
- IdleDetector (~5): threshold behavior, mark_active reset, idle_since
- ComputeAvailability (~6): ping success/failure, cache TTL, tier list, timeouts
- SurplusQueue (~12): enqueue/dequeue, priority ordering, tier filtering, state
  transitions, drain_expired, drive weight multiplication
- BrainstormRunner (~6): schedules both types, idempotent, writes logs, links staging
- StubExecutor (~3): returns placeholder, populates staging fields
- SurplusScheduler (~10): idle gating, dispatch flow, empty queue, error handling,
  brainstorm check on startup, graceful stop

**Integration tests (~5):**
- Full pipeline: enqueue → idle → dispatch → staging entry
- Brainstorm → queue → executor → surplus_insights + brainstorm_log
- Compute availability gating (mock LM Studio down)
- Priority ordering across multiple tasks
- Failed task respects max_attempts

**All tests run with no LLM calls** — StubExecutor handles everything.

## Verification (from build phases doc)

- [ ] Surplus tasks only execute on free/cheap compute
- [ ] Cost-frequency rule enforced (free=always, threshold=never)
- [ ] Staging area stores without promoting to production
- [ ] Daily brainstorm sessions fire reliably (exactly 2/day minimum)
- [ ] Brainstorm sessions write to brainstorm_log (memory-mcp stubbed in V3)
- [ ] Idle detection identifies available compute windows

## V4 Expansion Points

The following are documented GROUNDWORK slots, not V3 scope:

- **Multi-model sweep:** Same prompt dispatched to multiple free providers for
  perspective diversity
- **Two-stage filtering:** Free model generates → different free model filters →
  only top candidates reach Deep reflection
- **Self-unblocking:** When task system hits a blocker, surplus brainstorms
  solutions on free compute
- **Systematic memory auditing:** Cross-reference existing memories for
  contradictions, staleness, confirmation
- **Anticipatory research:** Based on user activity patterns, pre-research
  likely future needs
- **Internet scouring:** Search for solutions to known capability gaps
- **Parallel dispatch:** Multiple surplus tasks across different providers
  concurrently
- **Rate limit tracking:** Per-provider rate limit awareness for smarter
  dispatching
