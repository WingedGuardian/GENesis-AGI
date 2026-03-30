# Phase 2: Compute Routing — Design Document

**Date:** 2026-03-04
**Status:** Approved
**Dependencies:** Phase 0 (COMPLETE), Architecture Reconciliation (COMPLETE)
**Risk:** LOW — Infrastructure plumbing, no LLM judgment calls

---

## Goal

Build the complete compute routing infrastructure: fallback chain resolution,
circuit breakers, retry with backoff, cost tracking, budget enforcement,
dead-letter staging, and graceful degradation. All tested with mock delegates
— no real LLM calls needed until Phase 4 integrates.

## Architecture

**Approach:** Genesis-side routing module (`genesis.routing` package) that
wraps LLM call sites. The actual LLM call is delegated through a protocol
interface — mocked in tests, wired to `agent.call_chat_model()` /
`agent.call_utility_model()` in Phase 4.

**Why not an AZ extension or MCP server:** Routing decisions must be fast
(in-process lookup, not MCP round-trip) and testable without AZ running.
The routing module is a clean Python library that Phase 4's extensions import.

**Existing infrastructure used:**
- `cost_events` table + CRUD (Phase 0)
- `budgets` table + CRUD (Phase 0)
- `signal_weights` table (budget_pct_consumed signal for awareness loop)

---

## Core Routing Principles

1. **Every call site CAN use free compute. Some SHOULDN'T by default. All
   are overridable.** The `default_paid` flag is a recommendation, not a hard
   block. Anti-sycophancy calls (self-assessment, quality calibration) skip
   free models by default because quality matters more than cost. But the
   user can override any restriction via natural language.

2. **Budgets are Genesis's self-constraint, not the user's cage.** Budget
   enforcement limits Genesis's autonomous spending (background reflection,
   autonomous tasks). User-requested work always proceeds. The user can
   override any budget instantly via natural language ("raise daily limit to
   $5", "unlimited for this task", "go ahead anyway").

3. **No call site is ever permanently blocked.** An exceeded budget + no free
   override = Genesis tells the user and waits for their decision. It never
   silently drops important work forever. Genesis explains the tradeoff
   (quality vs. cost) and lets the user choose.

4. **Free compute is never budget-gated.** Budgets only apply to paid calls.
   Free sources (Ollama, Mistral free, Groq free, etc.) are always available
   regardless of budget status.

5. **Genesis is the interface to its own settings.** Users never touch YAML,
   SQL, or config files. "Switch adversarial to Kimi 2.5", "disable Gemini
   free tier", "show cost today" — all via natural language.

---

## Package Structure

```
src/genesis/routing/
├── __init__.py
├── types.py           # Enums, frozen dataclasses
├── config.py          # Load/validate model_routing.yaml
├── registry.py        # 28-call-site registry, chain lookup
├── circuit_breaker.py # Per-provider state machine + registry
├── retry.py           # Exponential backoff, jitter, error classification
├── router.py          # Main entry: route_call() walks chain, delegates
├── cost_tracker.py    # Write cost_events, check budgets, enforce limits
├── dead_letter.py     # Failed operation staging + replay
└── degradation.py     # Level tracking (L0-L5), sacrifice ordering

config/
└── model_routing.yaml # 28 call sites, provider definitions, retry policies

tests/test_routing/
├── __init__.py
├── conftest.py        # MockDelegate, test config fixtures
├── test_config.py
├── test_circuit_breaker.py
├── test_retry.py
├── test_router.py
├── test_cost_tracker.py
├── test_dead_letter.py
└── test_degradation.py
```

---

## Types

```python
class ProviderState(StrEnum):
    CLOSED = "closed"       # Normal — accepting requests
    OPEN = "open"           # Tripped — rejecting requests
    HALF_OPEN = "half_open" # Probing — single request allowed

class ErrorCategory(StrEnum):
    TRANSIENT = "transient"  # 429, 503, timeout → retry with backoff
    DEGRADED = "degraded"    # Partial/malformed → retry once, then fallback
    PERMANENT = "permanent"  # 401, 404, quota → no retry, fallback immediately

class DegradationLevel(StrEnum):
    NORMAL = "L0"
    FALLBACK = "L1"            # One cloud provider down, transparent
    REDUCED = "L2"             # Multiple down, defer surplus
    ESSENTIAL = "L3"           # All cloud down, monitoring only
    MEMORY_IMPAIRED = "L4"     # Qdrant/SQLite down (independent axis)
    LOCAL_COMPUTE_DOWN = "L5"  # Ollama down (independent axis)

class BudgetStatus(StrEnum):
    UNDER_LIMIT = "under_limit"
    WARNING = "warning"        # Above warning_pct (default 80%)
    EXCEEDED = "exceeded"

@dataclass(frozen=True)
class ProviderConfig:
    name: str              # e.g., "mistral-free"
    provider_type: str     # "ollama", "anthropic", "mistral", etc.
    model_id: str          # Actual model name for the API
    is_free: bool
    rpm_limit: int | None
    open_duration_s: int   # 60 local, 120 cloud

@dataclass(frozen=True)
class CallSiteConfig:
    id: str                    # e.g., "3_micro_reflection"
    chain: list[str]           # Provider names in fallback order
    default_paid: bool = False # Skip free by default (overridable)
    never_pays: bool = False   # Surplus: skip paid entirely
    retry_profile: str = "default"  # default, user_facing, background

@dataclass(frozen=True)
class RoutingDecision:
    call_site_id: str
    provider_used: str
    model_id: str
    attempts: int
    fallback_used: bool

@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 3
    base_delay_ms: int = 500
    max_delay_ms: int = 30000
    backoff_multiplier: float = 2.0
    jitter_pct: float = 0.25
```

---

## Circuit Breaker

Per-provider state machine with persistence to SQLite for health-mcp
visibility and restart recovery.

```python
class CircuitBreaker:
    """Tracks provider health. State machine: CLOSED → OPEN → HALF_OPEN → CLOSED."""

    state: ProviderState
    failure_threshold: int = 3      # Consecutive failures to trip
    open_duration_s: int = 120      # How long to stay open
    success_threshold: int = 2      # Successes in half-open to close

    def is_available(self) -> bool
    def record_success(self) -> None
    def record_failure(self, category: ErrorCategory) -> None

class CircuitBreakerRegistry:
    """Manages all provider circuit breakers."""

    def get(self, provider: str) -> CircuitBreaker
    async def persist_state(self) -> None       # Save to SQLite periodically
    async def load_state(self) -> None          # Restore on startup
    def compute_degradation_level(self) -> DegradationLevel
```

Degradation level is computed from the set of all breaker states, not stored
separately. L4 and L5 are independent axes that coexist with L0-L3.

---

## Router

Main entry point. Walks the fallback chain for a call site.

```python
class CallDelegate(Protocol):
    """Pluggable call backend. Mock in tests, AZ in production."""
    async def call(self, provider: str, model_id: str,
                   messages: list[dict], **kwargs) -> CallResult

class Router:
    def __init__(self, config, breakers, cost_tracker, delegate): ...

    async def route_call(self, call_site_id: str, messages: list[dict],
                         *, budget_override: bool = False, **kwargs) -> RoutingResult:
        """
        1. Look up call site config → get fallback chain
        2. Filter chain by default_paid / never_pays / free-only-mode
        3. For each provider in chain:
           a. Check circuit breaker → skip if OPEN
           b. Check budget (if paid) → skip if exceeded and no override
           c. Try provider with retry policy
           d. On success: record cost event, record success, return
           e. On failure: classify error, record failure, continue
        4. All exhausted → return failure result
        """
```

The `budget_override` flag is set by the agent layer when the user explicitly
approves a budget-exceeding call.

---

## Cost Tracking & Budget Enforcement

Wires together existing Phase 0 CRUD:

```python
class CostTracker:
    async def record(self, call_site_id, provider, result) -> None
        # → cost_events.create()

    async def check_budget(self, budget_type, *, task_id=None) -> BudgetStatus
        # → budgets.list_active() + cost_events.sum_cost()

    async def get_period_cost(self, period: str) -> float
        # period: "today", "this_week", "this_month"
```

Budget enforcement behavior:

| Status | Autonomous Calls | User-Requested Calls | Free Calls |
|--------|-----------------|---------------------|------------|
| UNDER_LIMIT | Proceed | Proceed | Always proceed |
| WARNING | Proceed + signal to awareness loop | Proceed | Always proceed |
| EXCEEDED | Block paid. Ask user if critical. | Proceed (confirm for large calls) | Always proceed |

Budget defaults (conservative, user-adjustable via natural language):

| Type | Default | Rationale |
|------|---------|-----------|
| daily | $2.00 | ~60 paid Sonnet calls/day |
| weekly | $10.00 | Allows some Opus usage |
| monthly | $30.00 | Conservative personal copilot |
| task | $1.00 | Per-task ceiling |
| workaround | $0.20 | 20% of task budget (design doc spec) |

---

## Dead-Letter Staging

New table for failed operations awaiting replay.

```sql
CREATE TABLE IF NOT EXISTS dead_letter (
    id              TEXT PRIMARY KEY,
    operation_type  TEXT NOT NULL,      -- memory_write, embedding, observation, etc.
    payload         TEXT NOT NULL,      -- JSON blob of the full operation
    target_provider TEXT NOT NULL,      -- which provider/system failed
    failure_reason  TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    retry_count     INTEGER DEFAULT 0,
    last_retry_at   TEXT,
    status          TEXT DEFAULT 'pending'  -- pending, replayed, expired
)
```

```python
class DeadLetterQueue:
    async def enqueue(self, operation_type, payload, target_provider, failure_reason) -> str
    async def replay_pending(self, target_provider) -> int  # Returns count replayed
    async def expire_old(self, max_age_hours=72) -> int     # Returns count expired
    async def get_pending_count(self, target_provider=None) -> int
```

What gets dead-lettered: memory writes, embedding writes, observation writes,
execution trace writes, signal weight updates.

What does NOT: outreach delivery (idempotency risk), circuit breaker state
(ephemeral), awareness tick results (next tick recollects).

---

## Graceful Degradation

Levels are computed from circuit breaker states. L4/L5 are independent axes.

| Level | Trigger | Sacrifice Order |
|-------|---------|----------------|
| L0 Normal | All up | — |
| L1 Fallback | One cloud down | Transparent, logged |
| L2 Reduced | Multiple cloud down | Defer: surplus brainstorm → outreach drafts → morning report compilation |
| L3 Essential | All cloud down | Awareness loop + triage + health monitoring continue. Reflections paused, signals queued |
| L4 Memory | Qdrant/SQLite down | Writes → dead-letter. FTS5 as degraded retrieval |
| L5 Local | Ollama down | Triage → programmatic rules. Embeddings → dead-letter |

What NEVER gets skipped: triage, health monitoring, cost accounting, micro
reflection signal capture (queued for later processing if no LLM available).

Recovery is automatic: circuit breaker half-open probes on each awareness
tick detect recovery. Dead-letter replay and catch-up ticks run automatically.

---

## Config File

```yaml
# config/model_routing.yaml
providers:
  ollama-3b:
    type: ollama
    model: qwen2.5:3b
    endpoint: http://${OLLAMA_URL:-localhost:11434}
    free: true
    open_duration_s: 60

  ollama-embedding:
    type: ollama
    model: qwen3-embedding:0.6b
    endpoint: http://${OLLAMA_URL:-localhost:11434}
    free: true
    open_duration_s: 60

  mistral-free:
    type: mistral
    model: mistral-large-latest
    free: true
    rpm_limit: 2
    open_duration_s: 120

  groq-free:
    type: groq
    model: llama-3.3-70b-versatile
    free: true
    rpm_limit: 30
    open_duration_s: 120

  gemini-free:
    type: google
    model: gemini-2.5-flash
    free: true
    rpm_limit: 15
    open_duration_s: 120
    allowed_tasks: [web_search, long_context, code_grunt]

  openrouter-free:
    type: openrouter
    model: best-free
    free: true
    rpm_limit: 20
    open_duration_s: 120

  deepseek-v4:
    type: deepseek
    model: deepseek-chat
    free: false
    open_duration_s: 120

  gpt-5-nano:
    type: openai
    model: gpt-5-nano
    free: false
    open_duration_s: 120

  claude-haiku:
    type: anthropic
    model: claude-haiku-4-5-20251001
    free: false
    open_duration_s: 120

  claude-sonnet:
    type: anthropic
    model: claude-sonnet-4-6-20250514
    free: false
    open_duration_s: 120

  claude-opus:
    type: anthropic
    model: claude-opus-4-6-20250514
    free: false
    open_duration_s: 120

call_sites:
  1_signal_collection:
    chain: [programmatic]
  2_triage:
    chain: [ollama-3b, mistral-free, groq-free, gpt-5-nano]
  3_micro_reflection:
    chain: [groq-free, mistral-free, gpt-5-nano]
    retry_profile: background
  4_light_reflection:
    chain: [claude-haiku, claude-sonnet]
    default_paid: true
  5_deep_reflection:
    chain: [claude-sonnet, claude-opus]
    default_paid: true
    retry_profile: background
  6_strategic_reflection:
    chain: [claude-opus]
    default_paid: true
    retry_profile: background
  7_task_retrospective:
    chain: [deepseek-v4, gpt-5-nano]
    default_paid: true
  8_memory_consolidation:
    chain: [mistral-free, groq-free, gemini-free, openrouter-free, gpt-5-nano]
  9_fact_extraction:
    chain: [mistral-free, groq-free, gemini-free, openrouter-free, gpt-5-nano]
  10_cognitive_state:
    chain: [deepseek-v4, claude-sonnet]
    default_paid: true
  11_user_model_synthesis:
    chain: [claude-sonnet, claude-opus]
    default_paid: true
    retry_profile: background
  12_surplus_brainstorm:
    chain: [mistral-free, groq-free, gemini-free, openrouter-free]
    never_pays: true
  13_morning_report:
    chain: [mistral-free, groq-free, gemini-free, gpt-5-nano]
  14_weekly_self_assessment:
    chain: [claude-opus]
    default_paid: true
  15_triage_calibration:
    chain: [deepseek-v4, gpt-5-nano]
    default_paid: true
  16_quality_calibration:
    chain: [claude-opus]
    default_paid: true
  17_fresh_eyes_review:
    chain: [gpt-5-nano, claude-sonnet]
    default_paid: true
  18_meta_prompting:
    chain: [deepseek-v4, gpt-5-nano]
    default_paid: true
  19_outreach_draft:
    chain: [mistral-free, groq-free, gemini-free, openrouter-free, gpt-5-nano]
  20_adversarial_counterargument:
    chain: [gpt-5-nano, claude-sonnet]
    default_paid: true
  21_embeddings:
    chain: [ollama-embedding]
  22_tagging:
    chain: [ollama-3b, mistral-free, groq-free]
  27_pre_execution_assessment:
    chain: [claude-sonnet, claude-haiku]
    default_paid: true
    retry_profile: user_facing
  28_observation_sweep:
    chain: [deepseek-v4, claude-sonnet]
    default_paid: true

retry:
  default:
    max_retries: 3
    base_delay_ms: 500
    max_delay_ms: 30000
  user_facing:
    max_retries: 2
    base_delay_ms: 300
    max_delay_ms: 5000
  background:
    max_retries: 4
    base_delay_ms: 1000
    max_delay_ms: 60000
```

---

## Testing Strategy

All components tested with `MockDelegate` — no real LLM calls:

- **Config tests:** Load/validate YAML, reject malformed configs, verify all
  28 call sites present
- **Circuit breaker tests:** State transitions (CLOSED→OPEN→HALF_OPEN→CLOSED),
  timing, failure counting, persistence round-trip
- **Retry tests:** Backoff timing, jitter bounds, error classification,
  Retry-After header respect, retry budget exhaustion
- **Router tests:** Full chain walk, default_paid filtering, never_pays
  filtering, budget enforcement, budget override, all-providers-failed
- **Cost tracker tests:** Record events, period aggregation, budget checking
  (under/warning/exceeded), budget override logging
- **Dead-letter tests:** Enqueue, replay, expiry, idempotent replay
- **Degradation tests:** Level computation from breaker states, independent
  L4/L5 axes, sacrifice ordering

---

## Schema Changes

New table: `dead_letter` (see DDL above)
New indexes: `idx_dead_letter_status`, `idx_dead_letter_provider`
New seed data: default budget entries (daily $2, weekly $10, monthly $30)

---

## What Phase 4 Needs to Do

When Phase 4 (Perception) integrates the routing layer:

1. Implement `CallDelegate` protocol wrapping `agent.call_chat_model()` and
   `agent.call_utility_model()`
2. Wire `Router` into Genesis extension initialization
3. Replace direct model calls in reflection extensions with `router.route_call()`
4. Connect budget warnings to awareness loop signals
5. Connect degradation level to health-mcp observable state

---

## Cross-References

- Model Routing Registry: `docs/architecture/genesis-v3-model-routing-registry.md`
- Resilience Patterns: `docs/architecture/genesis-v3-resilience-patterns.md`
- Operations Design: `docs/plans/2026-03-03-model-routing-operations-design.md`
- Build Phases: `docs/architecture/genesis-v3-build-phases.md` → Phase 2
- AZ Integration: `docs/architecture/genesis-agent-zero-integration.md` → unified_call()
