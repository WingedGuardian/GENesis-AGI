# LLM Routing: How It Works

Multi-provider routing with circuit breakers, rate gates, and dead-letter recovery.
20+ providers. Zero single-provider dependency.

---

## Why this subsystem exists

Any system that depends on one LLM provider is one outage away from total failure.
Genesis makes 100+ LLM calls per day across triage, reflection, memory extraction,
surplus compute, and outreach. A provider going down can't cascade into system-wide
failure — which means routing can't be an afterthought.

---

## Architecture

```
route_call(call_site_id, messages)
          │
          ▼
  [Degradation check] — skip non-critical call sites if system is stressed
          │
          ▼
  [Budget check] — skip paid providers if daily/weekly/monthly limit hit
          │
          ▼
  [Fallback chain: provider_1, provider_2, ...]
          │
    ┌─────┴────────────────────────────────────┐
    ▼                  ▼                        ▼
[Circuit breaker] [Circuit breaker]    [Circuit breaker]
    │                  │                        │
[Rate gate]        [Rate gate]            [Rate gate]
    │                  │                        │
[Retry loop]       [Retry loop]          [Retry loop]
    │                  │                        │
    └─────────┬─────────┘────────────────────────┘
              │
    Success → return result
    All fail → dead-letter queue
```

---

## Circuit Breaker

Three states. Transitions are automatic, state persists to disk across restarts.

```
CLOSED ──[3 consecutive failures]──► OPEN
   ▲                                    │
   │                              [wait duration]
   │                                    │
   │                                    ▼
   └──[2 consecutive successes]──── HALF_OPEN
                                        │
                              [1 failure in HALF_OPEN]
                                        ▼
                                      OPEN (duration doubles)
```

**Escalating backoff on repeated trips:**

| Trip | Open Duration |
|------|---------------|
| 1 | 120s |
| 2 | 240s |
| 3 | 480s |
| 4 | 960s |
| cap | 1800s (30 min) |
| quota exhaustion | 14400s (4 hours) |

On restart, trip count caps at 3 — prevents week-spanning lockouts from an
extended outage.

---

## Error Classification

Not all failures are the same. `classify_error()` in `retry.py` determines
whether to retry and how:

```python
_TRANSIENT_CODES = {408, 429, 500, 502, 503, 504}  # retry with backoff
_PERMANENT_CODES = {401, 404}                       # stop immediately
_QUOTA_CODES = {402}                                # 4h circuit breaker
_QUOTA_KEYWORDS = {"quota", "exceeded", "billing", "limit", "exhausted"}
```

| Category | What triggers it | What happens |
|----------|-----------------|--------------|
| TRANSIENT | 429, 5xx, timeout | Retry with exponential backoff |
| PERMANENT | 401, 404 | Skip provider, move to next in chain |
| QUOTA_EXHAUSTED | 402, quota keywords in 403 | Trip breaker with 4h duration |
| DEGRADED | Malformed/partial response | Retry |

---

## Rate Gate: Thundering Herd Prevention

When a primary provider fails, every call site simultaneously falls back to the
same alternatives. Without per-provider rate limiting, the fallback gets hit with
10x traffic and rate-limits immediately — cascading the failure forward.

```python
class ProviderRateGate:
    def __init__(self, provider: str, rpm: int):
        self._interval = 60.0 / rpm  # 30 RPM → 2s between requests
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        async with self._lock:
            elapsed = time.monotonic() - self._last_call
            wait = max(0, self._interval - elapsed)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()
            return wait
```

The asyncio lock naturally serializes requests. No distributed coordination needed.

**Configured limits:**

| Provider | RPM | Interval |
|----------|-----|----------|
| Mistral Large | 4 | 15.0s |
| Groq | 30 | 2.0s |
| OpenRouter | 20 | 3.0s |
| Cerebras | 10 | 6.0s |

---

## Retry Policy

Exponential backoff with jitter prevents synchronized retry storms across callers.

| Attempt | Base Delay | With ±25% Jitter |
|---------|------------|------------------|
| 0 | 500ms | 375–625ms |
| 1 | 1000ms | 750–1250ms |
| 2 | 2000ms | 1500–2500ms |
| 3 (max) | 4000ms | 3000–5000ms |

Hard cap at 30s delay. Max 3 retries per provider — 4 total attempts. Combined
with the fallback chain: worst case is `3 providers × 4 attempts = 12` attempts
before dead-lettering.

---

## Dead-Letter Queue

When every provider in a chain fails, the request is queued for later redispatch
rather than dropped silently.

```
All providers exhausted
    │
    ▼
[Enqueue] — operation_type, payload, failure_reason, status: "pending"
    │
    ▼ (periodic redispatch)
    ├── Success → status: "replayed"
    ├── call_site_id no longer in config → status: "expired" (orphan cleanup)
    └── Still failing → retry_count++, stay "pending"

After 72h → auto-expire (unbounded growth prevention)
```

**Orphan cleanup** runs after `router.reload_config()`. If a call_site_id was
removed from `model_routing.yaml`, its pending DLQ items expire immediately.

---

## Budget Tracking

Three independent periods checked before every paid provider attempt:

```python
async def check_budget(self) -> BudgetStatus:
    daily = await self._sum_period(start_of_day, now)
    weekly = await self._sum_period(start_of_week, now)
    monthly = await self._sum_period(start_of_month, now)
    # returns worst status across all three
```

When `EXCEEDED`: paid providers are skipped (free tier always available).
`budget_override=True` exists for critical operations.

Why three periods: a single monthly limit can be exhausted on day one.
Three periods prevent daily spikes while preserving monthly capacity.

---

## Degradation Levels

When multiple providers are down, the system adapts rather than grinding:

| Level | Condition | Behavior |
|-------|-----------|----------|
| L0 NORMAL | All healthy | Full operation |
| L1 FALLBACK | 1 paid provider down | Use fallback chains |
| L2 REDUCED | 2+ paid down | Skip surplus, morning reports |
| L3 ESSENTIAL | All paid down | Only triage, embeddings, tagging |

The L2/L3 skip logic prevents burning free-tier rate limits on non-critical
work when the system is already under stress.

---

## Key Files

| File | Purpose |
|------|---------|
| `src/genesis/routing/router.py` | Main orchestrator, fallback chain iteration |
| `src/genesis/routing/circuit_breaker.py` | State machine, escalating backoff, persistence |
| `src/genesis/routing/retry.py` | Error classification, backoff computation |
| `src/genesis/routing/rate_gate.py` | Per-provider RPM enforcement |
| `src/genesis/routing/cost_tracker.py` | Budget periods, cost recording |
| `src/genesis/routing/dead_letter.py` | DLQ, redispatch, orphan cleanup |
| `src/genesis/routing/degradation.py` | Degradation levels, call-site filtering |
| `config/model_routing.yaml` | Provider config, call site chains, RPM limits |

---

## Design Decisions

**Why fallback chains over a single smart router?**
Any single aggregator (even OpenRouter) has its own outages. The router treats
every provider, including aggregators, as unreliable.

**Why circuit breakers over simple retry?**
Retrying a dead provider wastes time and burns rate budget. Circuit breakers skip
known-broken providers immediately, falling through to healthy alternatives.

**Why per-provider rate gates?**
Asyncio serialization is cheap. Without it, thundering herd after a primary failure
cascades to every fallback simultaneously. One failing provider becomes three.

**Why DLQ over drop?**
Non-critical operations (reflection, surplus) tolerate delayed execution. DLQ ensures
nothing is permanently lost without explicit expiration policy.

---

## V4 Targets

- **Latency-aware routing**: dynamic reordering based on recent p95, not static chain position
- **Output quality scoring**: track not just success/failure but instruction-following quality;
  route complex tasks toward higher-quality providers
- **Cross-call-site coordination**: shared awareness of "Groq is slow right now"
  across all call sites simultaneously
