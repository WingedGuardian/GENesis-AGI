# Design: Model Routing Operations Layer

**Date:** 2026-03-03
**Status:** Approved
**Scope:** V3 runtime operations for the 28-call-site model routing registry
**Dependencies:** Model Routing Registry, Resilience Patterns, health-mcp, Awareness Loop

---

## Summary

This document defines how the model routing registry operates at runtime: how
users configure it, how the system handles failures, how it degrades gracefully,
how it discovers and adapts to available infrastructure, and how it communicates
status to the user.

---

## 1. V3 Configuration Surface

### Config File as Source of Truth

A YAML config file (`config/model_routing.yaml`) defines all 28 call sites with
full fallback chains. This is the single source of truth for model assignments.

```yaml
call_sites:
  2_triage:
    chain: [ollama-3b, mistral-free, groq-free, gpt-5-nano]

  7_task_retrospective:  # ⚡ outsized impact — always paid
    chain: [deepseek-v4, qwen-3.5-plus, gpt-5-nano]
    always_paid: true

  12_surplus_brainstorm:
    chain: [local-30b, mistral-free, groq-free, gemini-free, openrouter-free]
    never_pays: true

  17_fresh_eyes_review:
    chain: [gpt-5.2, kimi-2.5, sonnet]
    rotation_pool: [gpt-5.2, kimi-2.5]  # user-switchable

  21_embeddings:
    chain: [ollama-embedding, sentence-transformers-cpu, openai-embedding]

free_compute:
  mistral_free:
    enabled: true
    rpm_limit: 2
  groq_free:
    enabled: true
    rpm_limit: 30
  gemini_free:
    enabled: true
    allowed_tasks: [web_search, long_context, code_grunt]
  openrouter_free:
    enabled: true
```

Every call site has a full chain from local to free cloud to paid cloud. The
system walks the chain at runtime, skipping anything unreachable.

### Chat Commands

Genesis modifies the config file via natural language commands:

- `show routing table` — formatted view of current assignments and active model
- `switch adversarial to Kimi 2.5` — updates config, confirms change
- `show health status` — circuit breaker states, degradation level, baseline
- `show cost today` — cost_events summary for current day
- `disable Gemini free tier` — updates free_compute config
- `rescan infrastructure` — triggers full infrastructure re-probe
- `remove Ollama from baseline` — stops treating Ollama absence as Level 5

### V4 Upgrade Path

Separate dashboard page within Agent Zero's Flask app (`/genesis/dashboard`).
Visualizes the same data the chat commands expose: routing table, circuit
breaker states, cost graph, degradation level, dead-letter queue depth,
notification history. All data is collected in V3; the dashboard renders it.

---

## 2. Infrastructure Baseline Detection

### The Adaptive Config Model

One config works for all deployments. No profiles, no manual environment
selection. The system learns what infrastructure is available and adapts.

- **User with Ollama + GPU:** Chain starts at local models
- **Cloud-only user:** Chain starts at Mistral free (Ollama not in baseline)
- **User who adds Ollama later:** Discovered, user approves, baseline updated

### Baseline Lifecycle

```
NOT IN BASELINE ──discover──→ DETECTED ──user approves──→ IN BASELINE
                                  │                            │
                                  └──user declines──→ KNOWN,   │
                                       NOT ADOPTED             │
                                                               │
IN BASELINE ──disappears──→ LEVEL 5 ALERT ──user removes──→ NOT IN BASELINE
                                │
                                └──recovers──→ IN BASELINE (automatic)
```

### Probe Mechanisms

**1. health-mcp process start (full probe)**

When the MCP server initializes, it probes all known endpoints: Ollama, GPU
machine, cloud APIs (lightweight ping). Establishes or refreshes the baseline.
Triggers: AZ process restart, MCP server restart, manual `rescan infrastructure`.

**2. Periodic re-probe via Awareness Loop (discovery)**

Every 288 ticks (24 hours), the Awareness Loop triggers a full re-probe through
health-mcp. Catches new infrastructure that appeared since last probe, expired
free tiers, changed endpoints.

**3. Lightweight health pings (failure detection)**

Every 5-min tick pings Ollama and GPU machine as part of signal collection.
Detects outages within 5 minutes. Cloud provider health is inferred from call
success/failure (no separate ping).

**4. Opportunistic discovery**

If a fallback chain walks past an endpoint marked "not in baseline" and it
unexpectedly responds, Genesis notices and initiates the discovery flow.

### New Infrastructure Discovery Flow

```
Probe detects new endpoint (e.g., Ollama at ${OLLAMA_URL:-localhost:11434})
  │
  ├─ 1. VERIFY — probe 3 times over 2 ticks (10 min) to confirm stability
  │     Check available models (GET /api/tags for Ollama)
  │
  ├─ 2. NOTIFY USER
  │     "New infrastructure detected: Ollama at ${OLLAMA_URL:-localhost:11434}
  │      Available models: qwen2.5:3b, qwen3-embedding:0.6b
  │      This enables local triage, tagging, and embeddings.
  │      Add to baseline and route eligible tasks to Ollama?"
  │
  ├─ 3. USER APPROVES → update baseline, update routing config, persist
  │
  └─ 4. USER DECLINES → mark as "known, not adopted," don't prompt again
```

Discovery is automatic. Adoption requires user approval.

---

## 3. Failure Handling

### Per-Call Retry Flow

```
Call site fires → try primary model
  ├─ Success → done
  └─ Fail → classify error
      ├─ Transient (408, 429, 5xx) → retry with exponential backoff
      │   3 attempts: 500ms → 1s → 2s (~4 seconds total)
      │   ├─ Retry succeeds → done
      │   └─ All retries fail → open circuit breaker, try next in chain
      └─ Permanent (401, 404, quota) → open circuit immediately, next in chain
```

Walk the fallback chain until something works. If every option in the chain
fails, the call is skipped and any write payload goes to dead-letter staging.

### Circuit Breaker (per provider)

| State | Behavior |
|-------|----------|
| **CLOSED** | Normal — send requests |
| **OPEN** | Stop sending. Duration: 60s local, 120s cloud |
| **HALF-OPEN** | After open duration, send 1 lightweight probe. 2 consecutive successes → CLOSED. Any failure → OPEN |

Recovery happens naturally via the Awareness Loop's 5-min tick. The tick IS
the health check — no dedicated probing thread needed.

### Ollama Self-Troubleshooting

When Ollama was in baseline but stops responding:

```
Ollama unreachable
  │
  ├─ 1. DIAGNOSE: ping endpoint, timeout vs connection refused
  │
  ├─ 2. ATTEMPT RESOLUTION (if accessible):
  │     Check model availability (GET /api/tags)
  │     Try pulling model if missing (POST /api/pull)
  │
  ├─ 3. IF CAN'T RESOLVE → Level 5 notification with diagnostics:
  │     "Ollama at ${OLLAMA_URL:-localhost:11434} unreachable (connection refused).
  │      Last successful contact: 5 min ago.
  │      Triage falling back to Mistral free.
  │      Embeddings queued to dead-letter.
  │      Possible causes: container stopped, network issue, host down."
  │
  └─ 4. CONTINUE MONITORING: half-open probe every tick
        On recovery: "Ollama recovered. Replaying N dead-letter entries."
```

---

## 4. Graceful Degradation

### Six Levels

| Level | Condition | Behavior | User Notification |
|-------|-----------|----------|-------------------|
| **0 — Normal** | All providers available | Full cognitive layer | None |
| **1 — Fallback** | One cloud provider down | Transparent fallback, same output contracts | Logged silently. Visible via `show health status`. Morning report mentions it |
| **2 — Reduced depth** | Multiple cloud providers down | Defer surplus (#12). Consolidate Bucket 2 to surviving providers. Deep/Strategic still fire | Push notification: "Reduced cognitive depth — [providers] unavailable" |
| **3 — Essential only** | All/most cloud LLM providers down | Health monitoring + triage continue (programmatic/Ollama). No reflections, no extraction. Queue signals for later | Immediate escalation: "Cognitive processing suspended" |
| **4 — Infrastructure: memory** | Qdrant or SQLite down | Memory impaired. Writes → dead-letter. FTS5 as degraded retrieval | Immediate escalation: "Memory infrastructure down" |
| **5 — Infrastructure: compute** | Ollama down | Triage → programmatic rules. Embeddings → dead-letter. Vector search blind for new content | Immediate escalation: "Ollama unavailable — triage degraded, embeddings queued" |

Levels 4 and 5 are independent of each other and of Levels 0-3. They can
coexist (e.g., Level 2 on cloud + Level 5 on Ollama simultaneously).

### Sacrifice Order

What gets deferred first when compute is scarce (micro reflections always
queue — they are never skipped):

1. Surplus brainstorm (#12) — skip entirely, pure bonus
2. Outreach drafts (#19) — queue intent, draft later
3. Morning report compilation (#13) — skip compilation, not health check
4. Memory consolidation (#8) — dead-letter, replay when available

### What Never Gets Skipped

- Triage (#2) — 3B Ollama or programmatic fallback. Always runs
- Health monitoring (#1) — programmatic, no LLM needed
- Cost accounting — programmatic
- Deep/Strategic reflection — high-value, worth waiting for
- Micro reflections — always queued, never skipped

### Recovery

Degradation is automatic and reversible. When providers recover (detected via
half-open probes on 5-min ticks), the system steps back down. Level 2→1→0
transitions are logged but don't push-notify — the morning report covers
recovery. Dead-letter replay and catch-up ticks run automatically.

---

## 5. Tiered Notifications

| Level | Channel | Example |
|-------|---------|---------|
| **Level 1** | Log only. Visible via `show health status` and morning report | "Mistral free rate-limited, using Groq for 2 calls" |
| **Level 2** | Push notification via outreach channels | "Reduced cognitive depth — Mistral and Groq unavailable. DeepSeek handling Bucket 2 tasks." |
| **Level 3** | Immediate escalation via all channels | "Genesis in essential-only mode. Cloud providers down. Monitoring continues." |
| **Level 4** | Immediate escalation | "Memory infrastructure down. Dead-letter staging active." |
| **Level 5** | Immediate escalation with diagnostics | "Ollama unreachable. Triage on programmatic rules. Embeddings queued." |
| **Recovery** | Log + morning report (no push) | Morning report: "Ollama recovered at 03:14 after 23 min. 5 dead-letter entries replayed." |

---

## 6. Queuing and Dead-Letter Behavior

| Situation | Behavior | Replay |
|-----------|----------|--------|
| Micro reflection queued | Signal data saved to staging table. Next available tick processes current + queued in batch | Auto-replay. Batch up to 6 ticks (30 min). If >30 min queued, summarize oldest |
| Memory write failed | Full payload → dead-letter table | Auto-replay when provider recovers. 72h retention |
| Embedding write failed | Text + metadata → SQLite (FTS5 searchable). Embedding → dead-letter | Auto-replay when Ollama recovers. Backfill vectors for records missing Qdrant entries |
| Outreach draft deferred | Intent record saved (recipient, topic, priority, source) | Draft generated when provider available. Intent doesn't expire |
| Surplus brainstorm skipped | Nothing saved | No replay |
| Reflection deferred | Marked "deferred" with reason. Signals accumulate | Runs next available tick with richer context. Max defer: 24h deep, 48h strategic → escalate |

### Dead-Letter Table Schema

```
id              TEXT PRIMARY KEY
operation_type  TEXT NOT NULL   -- memory_write, embedding, observation, etc.
payload         TEXT NOT NULL   -- JSON blob of the full operation
target_provider TEXT NOT NULL   -- which provider/system failed
failure_reason  TEXT NOT NULL   -- error message / classification
created_at      TEXT NOT NULL   -- when the failure occurred
retry_count     INTEGER DEFAULT 0
last_retry_at   TEXT
status          TEXT DEFAULT 'pending'  -- pending, replayed, expired
```

Replay is automatic and idempotent. Every write uses deterministic IDs
(content hash + timestamp) and upsert semantics. Replaying a dead-letter
entry already persisted through another path is a no-op.

Entries older than 72 hours are logged as permanently failed and archived.

---

## 7. Relationship to Existing Documents

| This Design | Updates/Extends |
|-------------|----------------|
| Config file + chat commands | New — V3 configuration surface for routing registry |
| Infrastructure baseline | New — extends health-mcp's role |
| Degradation levels 4 & 5 | Updates `genesis-v3-resilience-patterns.md` Pattern 5 (was 4 levels, now 6) |
| Micro reflections never skip | Updates resilience patterns (was "skip micro first") |
| Ollama self-troubleshooting | New — extends health-mcp diagnostic capability |
| Tiered notifications | New — extends outreach-mcp's role |
| Probe lifecycle | New — extends health-mcp startup behavior |
| Dead-letter schema | Refines resilience patterns Pattern 6 |
