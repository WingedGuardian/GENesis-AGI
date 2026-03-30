# Genesis v3: Infrastructure Resilience Patterns

**Date:** 2026-03-01
**Status:** Active — cross-cutting design concern for V3 implementation
**Applies to:** Phase 2 (Compute Routing), all 4 MCP servers, Awareness Loop, Reflection Engine
**Source:** Microsoft Azure transient fault handling best practices, Confluent agentic AI
reliability analysis, operational experience from v1/v2

---

## Why This Document Exists

The Genesis v3 architecture describes rich cognitive behavior — what happens when
things go right. But every LLM API call, every Qdrant query, every MCP server
operation can fail transiently. Rate limits, timeouts, dropped connections, 503s,
partial responses, and provider outages are routine in production LLM systems.

Without explicit resilience patterns, the Awareness Loop's 5-minute tick becomes
the system's single point of failure. If the tick's API calls fail and aren't
handled, the entire cognitive layer goes blind.

**Core principle:** The cognitive architecture (design doc) describes WHAT Genesis
thinks. This document describes HOW Genesis survives infrastructure failures while
thinking. These patterns are implementation-level but architecturally significant
because they affect every layer.

---

## Error Categorization

Before retrying, classify the error. This parallels the Self-Learning Loop's
root-cause classification (`approach_failure` / `capability_gap` / `external_blocker`)
but operates at the infrastructure level:

| Error Type | Examples | Action |
|-----------|---------|--------|
| **Transient** | HTTP 429, 503, connection timeout, socket reset | Retry with backoff |
| **Degraded** | Partial response, malformed JSON, truncated output | Retry once; if repeated, route to fallback provider |
| **Permanent** | HTTP 401 (auth), 404, invalid model name, quota exhausted | Do NOT retry. Log error, route to fallback provider or surface to user |
| **Provider down** | Consecutive transient failures from same provider | Open circuit breaker, route all traffic to fallback |

**Why this matters:** Retrying a permanent error wastes budget and delays fallback.
Not retrying a transient error causes unnecessary failures. The classification
determines the response.

---

## Pattern 1: Exponential Backoff with Jitter

**Applies to:** Every outbound API call (LLM inference, embedding, Qdrant, external APIs).

**Default retry policy:**

```
max_retries: 3
base_delay_ms: 500
max_delay_ms: 30000
backoff_multiplier: 2
jitter: ±25% of computed delay (random uniform)
```

**Retry sequence example:**
```
Attempt 1: immediate
Attempt 2: ~500ms  (500 × 1 × jitter)
Attempt 3: ~1000ms (500 × 2 × jitter)
Attempt 4: ~2000ms (500 × 4 × jitter)
→ All attempts exhausted → fallback or surface error
```

**Why jitter is mandatory:** The Awareness Loop fires every 5 minutes. If it makes
3 API calls and all fail simultaneously, without jitter they all retry at the same
instant — hitting the same rate limit again. Jitter desynchronizes retries.

**Override per context:**
- **User-facing task execution:** Lower max_retries (2), shorter delays. User is waiting.
- **Background reflection:** Higher max_retries (4), longer delays. No urgency.
- **Embedding calls:** Shorter base_delay (200ms). Embeddings are fast operations that usually succeed quickly or not at all.

**Respect `Retry-After` headers:** If a provider returns a `Retry-After` header or
similar signal, use that value as the minimum delay instead of the computed backoff.
The provider knows its recovery timeline better than our algorithm.

---

## Pattern 2: Circuit Breaker

**Applies to:** Each provider endpoint in the compute hierarchy (Ollama, Gemini free,
paid API providers, Qdrant).

A circuit breaker prevents hammering a provider that's already down. Three states:

```
CLOSED (normal)  →  failures exceed threshold  →  OPEN (blocking)
                                                      │
                                                      │ after cooldown period
                                                      ▼
                                                  HALF-OPEN (probing)
                                                      │
                                               ┌──────┴──────┐
                                               │             │
                                          probe succeeds  probe fails
                                               │             │
                                               ▼             ▼
                                            CLOSED         OPEN
                                          (recovered)   (still down)
```

**Default thresholds:**

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Failure threshold | 3 consecutive failures OR 5 failures in 5 minutes | Distinguish "one bad request" from "provider is down" |
| Base open duration | 60 seconds (Ollama), 120 seconds (cloud APIs) | Local services recover faster than cloud outages |
| Half-open probe | 1 lightweight request (health check or tiny inference) | Don't probe with expensive calls |
| Success threshold to close | 2 consecutive successes in half-open | One success could be a fluke |

**Escalating backoff:** The open duration doubles with each consecutive trip
without recovery: `base * 2^(trip_count-1)`, capped at 30 minutes. First trip
uses the base duration. A provider that's been down for hours gets probed
every 30 min instead of every 2 min. Trip count resets to 0 when the provider
recovers (HALF_OPEN → CLOSED). State is persisted to disk.

**Integration with Compute Hierarchy:**

When a circuit opens, the compute routing layer (Phase 2) automatically routes to
the next provider in the fallback chain:

```
Ollama (local) [OPEN] → skip → Gemini Flash free → success → use
                              ↓ [OPEN]
                        GLM5/cheap paid → success → use
                              ↓ [OPEN]
                        Sonnet-class → success → use (notify user of cost upgrade)
                              ↓ [OPEN]
                        ALL CIRCUITS OPEN → degrade gracefully (see Pattern 5)
```

**Circuit state is observable:** The health-mcp server should expose circuit breaker
states so the Awareness Loop can include "provider X has been down for Y minutes"
as a signal. This feeds naturally into the existing health alert system.

---

## Pattern 3: Retry Budget

**Applies to:** Per-provider, per time window. Prevents retry storms.

Individual request retry limits (Pattern 1) are necessary but insufficient. If the
Awareness Loop tick fires and makes 10 API calls, each retrying 3 times, that's 30
retry attempts hitting a struggling provider. Multiple concurrent reflections make
this worse.

**Budget parameters:**

| Scope | Budget | Window | Action when exhausted |
|-------|--------|--------|----------------------|
| Per provider | 30 retries | 1 minute | Open circuit breaker immediately |
| Per tick cycle | 10 retries | Per Awareness Loop tick | Skip remaining calls, log, try next tick |
| Global | 100 retries | 5 minutes | Reduce all activity to essential-only (health checks) |

**Why global budget matters:** If every provider is struggling simultaneously
(network issue, DNS problem), the system should reduce its own load rather than
amplifying the problem. The global budget triggers a "conservation mode" that
limits activity to health monitoring until connectivity recovers.

---

## Pattern 4: Idempotent Write Operations

**Applies to:** All MCP server write operations (memory_store, observation_write,
outreach_send, health_report_metric, etc.).

When a write operation times out, did it succeed on the server before the timeout
reached the client? If yes, retrying creates a duplicate. All write paths must
handle this:

**Memory writes:**
- Every memory item has a deterministic ID derived from content hash + timestamp
- `memory_store` uses upsert semantics: if ID exists, update metadata, don't create duplicate
- Extraction writes (`memory_extract`) deduplicate by content similarity threshold (embedding distance < 0.05 = same fact)

**Observation writes:**
- Observations have source + timestamp + content hash as composite key
- Duplicate writes update `last_seen` timestamp instead of creating new records

**Outreach sends:**
- Outreach items have a unique `outreach_id` assigned at queue time (before delivery attempt)
- Delivery retry uses the same `outreach_id` — delivery adapters must be idempotent on ID
- **Critical:** A user receiving the same WhatsApp message twice is worse than not sending it. When in doubt, don't retry outreach delivery — log the failure for manual review.

**Execution traces:**
- Trace writes use `task_id` + `sub_agent_id` as composite key
- Partial trace updates (adding sub-agent results) use atomic append, not replace

---

## Pattern 5: Graceful Degradation

**Applies to:** The cognitive layer when infrastructure is impaired.

When providers fail and circuit breakers open, the system should degrade gracefully
rather than crash or stall. Six degradation levels, ordered by severity. Levels
0-3 cover cloud provider availability. Levels 4-5 cover local infrastructure and
can coexist independently with Levels 0-3.

> For the full operational design (notifications, queuing, self-troubleshooting,
> infrastructure baseline detection), see
> `docs/plans/2026-03-03-model-routing-operations-design.md`.

**Level 0: Normal operation**
All providers available. Full cognitive layer active.

**Level 1: Provider fallback (transparent)**
One cloud provider down, fallback active. No user-visible impact. Logged silently
— visible via `show health status` chat command and morning report.

**Level 2: Reduced cognitive depth**
Multiple cloud providers down. Available compute is limited.
- Defer surplus brainstorm (#12) — pure bonus, skip entirely
- Queue outreach drafts (#19) — save intent, draft later
- Consolidate Bucket 2 calls to surviving providers
- Deep/Strategic reflections still fire on schedule
- **Micro reflections always queue — never skip.** Signal data from the tick is
  saved to a staging table and batch-processed on the next available tick.
- **Notification:** Push via outreach channels.

**Level 3: Essential-only mode**
All or most cloud LLM providers down. The system can't reason.
- Awareness Loop continues (it's programmatic, no LLM needed)
- Triage continues (3B Ollama if alive, programmatic rules otherwise)
- Health monitoring continues
- All reflections paused, signals queued for catch-up
- Outreach limited to pre-queued items
- User conversations route to whatever provider responds
- **Notification:** Immediate escalation via all channels.

**Level 4: Infrastructure failure — memory**
Qdrant or SQLite down. Memory system impaired.
- User conversations continue without memory injection (degraded but functional)
- All writes queued to dead-letter staging (see Pattern 6)
- FTS5 full-text search in SQLite serves as degraded retrieval if Qdrant is down
- **Notification:** Immediate escalation.

**Level 5: Infrastructure failure — local compute**
Ollama container down. Triage, embeddings, and tagging impaired.
- Triage (#2) falls back to programmatic rules (cruder but functional)
- Tagging (#22) falls back to regex/heuristic extraction
- Embeddings (#21) dead-lettered; new memories still written to SQLite (FTS5
  searchable) but vector similarity search goes blind for new content
- Genesis attempts self-troubleshooting: ping endpoint, check model availability,
  attempt model pull if accessible. Reports diagnostics if unresolvable.
- **Notification:** Immediate escalation with diagnostics.

Levels 4 and 5 are independent of each other and of Levels 0-3. "Cloud is fine
but Ollama is down" is a distinct state from "cloud is down but Ollama is fine."

**Recovery:** Degradation is automatic and reversible. When providers recover
(detected via half-open probes on 5-min ticks), the system steps down. Process
the dead-letter queue and run a catch-up tick to clear accumulated signal
backlog. Recovery from Level 2→1→0 is logged but does not push-notify — the
morning report covers it.

---

## Pattern 6: Dead-Letter Staging

**Applies to:** Any operation that produces outputs that fail to persist.

When a reflection produces observations but the memory write fails after all retries,
those observations must not be lost. Dead-letter staging captures failed operations
for later replay:

**Implementation:**
- Local file-based queue (SQLite table or JSON-lines file in workspace)
- Each entry: `{operation, payload, original_timestamp, failure_reason, retry_count}`
- Processed on next successful health check of the target system
- Entries older than 72 hours are logged as permanently failed and archived

**What gets dead-lettered:**
- Memory writes (observations, episodic records, procedural extractions)
- Outreach queue additions (not delivery — see Pattern 4 outreach note)
- Execution trace writes
- Signal weight updates

**What does NOT get dead-lettered:**
- Outreach delivery (idempotency risk too high — see Pattern 4)
- Circuit breaker state (ephemeral, reconstruct from current conditions)
- Awareness Loop tick results (next tick will recollect signals)

---

## Implementation Priority

These patterns are ordered by implementation priority within Phase 2:

1. **Exponential backoff + jitter** — Wrap every LLM API call. Prerequisite for everything else.
2. **Error categorization** — Distinguish transient from permanent before retrying.
3. **Circuit breaker** — Prevents cascading failures when providers go down.
4. **Idempotent writes** — Design MCP server write operations correctly from the start.
   (This is Phase 0 design work, not Phase 2, but must be decided before Phase 0 code is final.)
5. **Retry budget** — Prevents retry storms under load.
6. **Graceful degradation** — Define the degradation levels and wire them into the Awareness Loop.
7. **Dead-letter staging** — Safety net for failed writes. Can be a simple file initially.

**Phase 0 implication:** The MCP server stubs built in Phase 0 should already use
upsert/idempotent semantics in their CRUD operations. Retrofitting idempotency is
harder than building it in.

---

## Relationship to Existing Architecture

| This Document | Related Architecture |
|--------------|---------------------|
| Error categorization | Parallels Self-Learning Loop's root-cause classification (§Layer 3) |
| Circuit breaker + fallback | Extends Compute Hierarchy (§LLM Weakness Pattern 1) |
| Graceful degradation | Extends health-mcp's role (§4 MCP Servers → health-mcp) |
| Retry budget | New concept — feeds into cost accounting (§Build Phases Phase 2) |
| Dead-letter staging | Extends surplus staging concept (§Build Phases Phase 3) |
| Idempotent writes | Constraint on Phase 0 MCP server stubs |

---

## Sources

- [Microsoft Azure — Transient Fault Handling Best Practices](https://learn.microsoft.com/en-us/azure/architecture/best-practices/transient-faults)
- [Confluent — Agentic AI Top 5 Challenges](https://www.confluent.io/blog/agentic-ai-the-top-5-challenges-and-how-to-overcome-them/)
- [Microsoft Azure — Circuit Breaker Pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker)
- [Microsoft Azure — Retry Pattern](https://learn.microsoft.com/en-us/azure/architecture/patterns/retry)

---

## Related Documents

- [genesis-v3-resilience-architecture.md](genesis-v3-resilience-architecture.md) — System-level resilience design
- [genesis-v3-self-healing-design.md](genesis-v3-self-healing-design.md) — Self-healing server extension
