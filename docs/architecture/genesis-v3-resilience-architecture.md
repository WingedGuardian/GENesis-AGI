# Genesis v3 — Resilience Architecture

**Status:** Active | **Last updated:** 2026-03-11


> System-level resilience design. Supersedes the tactical patterns in
> `genesis-v3-resilience-patterns.md` for architectural decisions; that document
> remains the building-blocks reference.

## 1. Scope & Constraints

Genesis runs in a container where **only Python + SQLite are guaranteed local**.
Everything else — LLM providers, Qdrant, Ollama, external APIs — may be cloud
or network-adjacent and therefore subject to outage, latency spikes, rate
limits, or complete unavailability.

The resilience layer must:
- Detect partial and total failures across independent axes.
- Defer work that cannot be completed now without losing it.
- Queue embedding operations for replay on recovery.
- Track Claude Code session budgets and throttle proactively.
- Orchestrate recovery in the correct order when services return.
- Notify the user through whatever channel remains available.

## 2. Composite Resilience State Machine

The legacy `DegradationLevel` enum (L0–L5) collapses multiple failure dimensions
into a single ordinal. This makes it impossible to express "cloud LLMs are fine
but embeddings are down" without overloading semantics.

### 2.1 Four Independent Axes

| Axis | Enum | States |
|------|------|--------|
| **Cloud LLM** | `CloudStatus` | `NORMAL` → `FALLBACK` → `REDUCED` → `ESSENTIAL` → `OFFLINE` |
| **Memory** | `MemoryStatus` | `NORMAL` → `FTS_ONLY` → `WRITE_QUEUED` → `DOWN` |
| **Embedding** | `EmbeddingStatus` | `NORMAL` → `QUEUED` → `UNAVAILABLE` |
| **Claude Code** | `CCStatus` | `NORMAL` → `THROTTLED` → `RATE_LIMITED` → `UNAVAILABLE` |

Each axis is updated independently. The composite `ResilienceState` is the
product of all four axes plus a timestamp and transition history.

### 2.2 Flapping Protection

If the same axis transitions **more than 3 times within a 15-minute window**,
the state machine holds the **worse state** for a 10-minute stabilization
period. During stabilization, transitions to better states on that axis are
suppressed; transitions to worse states are always accepted.

### 2.3 Legacy Mapping

For backward compatibility, `ResilienceState` maps to a single
`DegradationLevel` via `to_legacy_degradation_level()`:

- `cloud=NORMAL/FALLBACK` → `NORMAL`/`FALLBACK`
- `cloud=REDUCED` → `REDUCED`
- `cloud=ESSENTIAL/OFFLINE` → `ESSENTIAL`
- `memory=FTS_ONLY/WRITE_QUEUED/DOWN` → `MEMORY_IMPAIRED` (overrides if worse)
- `embedding=QUEUED/UNAVAILABLE` → `LOCAL_COMPUTE_DOWN` (overrides if worse)

Implementation: `src/genesis/resilience/state.py`

## 3. Failure Mode Responses

| Failure Mode | Detection | Response |
|---|---|---|
| Single provider failure | Circuit breaker opens | Route to next in chain; no user impact |
| Multiple provider failure | ≥2 breakers open | `cloud=REDUCED`; defer non-essential work |
| All cloud down | All providers open | `cloud=OFFLINE`; defer all LLM work; notify user |
| CC rate limits | 429 / budget exhaustion | `cc=THROTTLED→RATE_LIMITED`; defer background sessions |
| Full internet outage | All external calls fail | All axes degrade; local-only operation |
| SLM/embeddings down, heavy LLMs fine | Ollama unreachable, cloud OK | `embedding=UNAVAILABLE`; queue embeddings; awareness ticks skip SLM |
| Full chain exhaustion | Dead letter overflow | `cloud=ESSENTIAL`; critical-only operation |

## 4. Deferred Work Queue

A SQLite table (`deferred_work_queue`) stores Genesis-level work items that
cannot be completed due to degraded state.

### 4.1 Schema

See `src/genesis/db/schema.py` for DDL. Key fields:
- `work_type`: categorizes the work (e.g., `reflection`, `outreach_draft`, `morning_report`)
- `priority`: integer 10–100, lower = higher priority
- `staleness_policy`: one of `drain`, `refresh`, `discard`, `ttl`
- `staleness_ttl_s`: seconds before TTL-policy items expire

### 4.2 Staleness Policies

| Policy | Behavior |
|--------|----------|
| `drain` | Always process on recovery, regardless of age |
| `refresh` | Discard stale item; re-derive from current state on recovery |
| `discard` | Drop if not processed within a reasonable window |
| `ttl` | Drop if `staleness_ttl_s` has elapsed since `deferred_at` |

### 4.3 Priority Constants

| Name | Value | Use Case |
|------|-------|----------|
| `FOREGROUND` | 10 | User-initiated work |
| `URGENT_OUTREACH` | 20 | Blocker/alert outreach |
| `REFLECTION` | 30 | Reflection sessions |
| `SCHEDULED` | 40 | Scheduled tasks |
| `MEMORY_OPS` | 50 | Memory maintenance |
| `OUTREACH_DRAFT` | 60 | Non-urgent outreach |
| `MORNING_REPORT` | 70 | Morning report generation |
| `SURPLUS` | 80 | Surplus/brainstorm work |

Implementation: `src/genesis/resilience/deferred_work.py`

## 5. Embedding Backlog

When embedding providers (Ollama, Mistral) are unavailable, content that needs
embedding is written to `pending_embeddings` rather than dropped.

### 5.1 Schema

See `src/genesis/db/schema.py`. Each row captures the raw content, memory type,
target collection, and tags needed to embed and upsert on recovery.

### 5.2 Recovery Worker

`EmbeddingRecoveryWorker` drains the pending queue at a configurable pace
(default 10/min) to avoid overwhelming a freshly-recovered embedding provider.
On individual item failure, the worker marks that item failed and continues.

Implementation: `src/genesis/resilience/embedding_recovery.py`

## 6. CC Session Budget Management

Claude Code sessions are rate-limited by Anthropic. Genesis must:

1. **Track** active sessions, completions, and rate-limit responses.
2. **Prioritize** foreground > background reflection > background tasks.
3. **Throttle** by deferring low-priority background sessions when approaching
   limits.
4. **Back off** on 429s with exponential delay.

The `CCStatus` axis reflects current CC availability. When `RATE_LIMITED`,
only foreground sessions proceed; all background work is deferred.

## 7. Recovery Orchestration

When a degraded axis returns to a better state, recovery follows this sequence:

1. **Health confirmation** — Verify the service is genuinely back (not just one
   successful call). Require 3 consecutive successes before upgrading state.
2. **Staleness expiry** — Run `expire_stale()` on the deferred work queue to
   drop items whose policies say they're no longer relevant.
3. **Embedding drain** — `EmbeddingRecoveryWorker.drain_pending()` processes
   queued embeddings at controlled pace.
4. **Deferred work drain** — Process remaining deferred work items in priority
   order.
5. **Dead-letter replay** — Replay items from `dead_letter` table (existing
   infrastructure in `genesis.routing.dead_letter`).
6. **Catch-up tick** — Fire an out-of-cycle awareness tick to re-assess signals
   with restored capabilities.

## 8. Out-of-Band Notification

When Genesis enters a degraded state, it needs to notify the user even if the
primary conversation channel is unavailable.

### 8.1 Local Status File

Write a JSON status file to `~/.genesis/status.json` on every state transition.
Any local tool can read this. Fields: current state, transition history,
deferred work count, pending embeddings count, timestamp.

### 8.2 Webhook (Optional)

If configured, POST state transitions to a webhook URL. Fire-and-forget with
3 retries. Configuration via `GENESIS_STATUS_WEBHOOK_URL` env var.

### 8.3 Dashboard (Phase 8)

The health-mcp server (Phase 8) reads resilience state and exposes it via
MCP tools for the Agent Zero dashboard.

## 9. Edge Cases

### 9.1 Flapping

Handled by the stabilization mechanism in §2.2. The 10-minute hold prevents
rapid state oscillation from triggering repeated recovery cycles.

### 9.2 Partial Recovery

One axis recovers while others remain degraded. Recovery orchestration runs
only for the recovered axis's dependent work. Other deferred work stays queued.

### 9.3 Huge Queue

If `deferred_work_queue` exceeds 1000 pending items, begin aggressive staleness
expiry: treat `refresh` and `discard` policies as immediate-expire. Log a
warning. This prevents unbounded queue growth during extended outages.

### 9.4 Outreach Idempotency

Deferred outreach items include a content hash. On drain, check
`outreach_history` for a matching hash before sending. Skip duplicates.

### 9.5 Stale Morning Report

Morning reports have `refresh` staleness policy. If the outage spans the
morning window, the deferred report is discarded and a fresh one is generated
from current state on recovery.

### 9.6 SQLite Disk Full

If SQLite writes fail with `SQLITE_FULL`, Genesis enters a hard-degraded state.
Log the error, attempt to clear WAL checkpoint, and notify via stderr. No
deferred work can be queued — this is a last-resort scenario.

## 10. Implementation Mapping

### 10.1 New Files

| File | Purpose |
|------|---------|
| `src/genesis/resilience/__init__.py` | Package init, public re-exports |
| `src/genesis/resilience/state.py` | Composite state machine |
| `src/genesis/resilience/deferred_work.py` | `DeferredWorkQueue` class |
| `src/genesis/resilience/embedding_recovery.py` | `EmbeddingRecoveryWorker` |
| `src/genesis/db/crud/deferred_work.py` | Deferred work CRUD |
| `src/genesis/db/crud/pending_embeddings.py` | Pending embeddings CRUD |

### 10.2 Modified Files

| File | Change |
|------|--------|
| `src/genesis/db/schema.py` | Add `deferred_work_queue` and `pending_embeddings` tables + indexes |
| `src/genesis/observability/types.py` | Add `RESILIENCE` to `Subsystem` enum (if needed) |

### 10.3 Existing Infrastructure

| File | Relationship |
|------|-------------|
| `src/genesis/routing/types.py` | `DegradationLevel` — legacy mapping target |
| `src/genesis/routing/dead_letter.py` | Dead letter queue — replayed during recovery |
| `src/genesis/memory/embeddings.py` | `EmbeddingProvider` — used by recovery worker |
| `src/genesis/memory/linker.py` | `MemoryLinker` — optional auto-linking on embed recovery |
| `src/genesis/qdrant/collections.py` | `upsert_point()` — vector storage target |
| `src/genesis/observability/events.py` | `GenesisEventBus` — emits resilience events |

## 11. Relationship to health-mcp

The resilience logic lives entirely in `src/genesis/resilience/`. The health-mcp
server (built in Phase 8) is a **consumer** of resilience state:

- Reads `ResilienceStateMachine.current` for dashboard display.
- Exposes `get_resilience_state` and `get_deferred_work_stats` as MCP tools.
- Does NOT modify resilience state — that's the responsibility of the routing
  layer and awareness loop.

This separation ensures resilience logic is testable without MCP infrastructure
and that health-mcp remains a thin read-only view.

---

*Cross-reference: `genesis-v3-resilience-patterns.md` for tactical patterns
(circuit breakers, retry policies, fallback chains) that this architecture
builds upon.*

---

## Related Documents

- [genesis-v3-resilience-patterns.md](genesis-v3-resilience-patterns.md) — Tactical resilience patterns
- [genesis-v3-self-healing-design.md](genesis-v3-self-healing-design.md) — Self-healing server extension
- [genesis-v3-survivable-architecture.md](genesis-v3-survivable-architecture.md) — Survivability under degradation
