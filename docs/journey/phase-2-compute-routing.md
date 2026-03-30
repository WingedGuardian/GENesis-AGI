# Phase 2: The Switchboard — Intelligent Routing

*Completed 2026-03-04. 93 tests (380 cumulative).*

---

## What We Built

Phase 2 is Genesis's nervous system for model selection — a complete routing infrastructure that decides which model handles each request, what happens when that model fails, and how the system degrades gracefully when providers go down.

The `genesis.routing` package includes: a 23-call-site routing registry with fallback chains, per-provider circuit breakers (state machine with closed/open/half-open transitions), exponential backoff with jitter, a cost tracker writing to SQLite, dead-letter queuing for failed operations, and a 6-level degradation tracker (L0 normal through L5 local-compute-down).

All of it tested with mock delegates. No real LLM calls needed — this is pure infrastructure, ready to wire into the perception pipeline when Phase 4 arrives.

## Why Intelligent Routing Matters

The naive approach to multi-model systems is a dropdown menu: pick a model, hope it works. The slightly better approach is a single fallback: if model A fails, try model B. Both approaches treat routing as plumbing — invisible, static, boring.

Genesis treats routing as an intelligence problem. Every call site in the system has different requirements. A micro reflection processing background signals needs speed and low cost — a free-tier model is ideal. A deep reflection consolidating a week of observations needs reasoning depth — that demands a stronger model. An anti-sycophancy check on outreach quality needs genuine independence — it should not use the same provider that generated the draft.

The routing registry encodes this knowledge: 23 call sites, each with a primary model and a fallback chain ordered by fitness for that specific task. When a provider goes down, the system doesn't just retry — it walks the chain to find the next model that meets the call site's requirements. When it recovers, the provider becomes eligible for routing again. The system heals itself.

## Key Design Decisions

**Circuit breakers per provider, not per model.** When a cloud provider has an outage, all of its models go down simultaneously. Tracking state per provider (not per model) means a single health signal correctly affects all models on that provider. The circuit breaker state machine — closed (healthy), open (rejecting), half-open (probing with a single request) — follows the standard pattern but with provider-appropriate timing: 60 seconds for local endpoints, 120 seconds for cloud.

**Cost tracking as observability, not control.** Every paid call writes a cost event to SQLite. Budget enforcement limits Genesis's autonomous spending on background work. But user-requested work always proceeds regardless of budget status. Budgets are Genesis's self-constraint — a way for the system to be disciplined about its own background spending — not a cage that blocks the user. The user can override any budget instantly through natural language.

**Dead-letter queuing for failed operations.** When the entire fallback chain is exhausted and a request cannot be served, it doesn't vanish. It goes into a dead-letter queue with full context — what was requested, which providers were tried, what errors occurred. This means failed operations can be replayed when providers recover, and the system maintains a complete record of what it couldn't do and why.

**Degradation as a spectrum, not a binary.** The system tracks six degradation levels across independent axes — cloud providers, memory systems, embedding services, and background compute. One cloud provider going down is L1 (transparent fallback). All cloud providers down is L3 (essential monitoring only). Qdrant down is L4 on an independent axis. This granularity means the system can communicate precisely about its own health rather than just "working" or "broken."

## What We Learned

The key insight from Phase 2 was that **intelligent routing is what makes multi-model systems practical**. Without it, you either pick one model and accept its limitations, or you build fragile if-then logic that breaks when providers change their APIs or pricing.

With a proper routing layer, Genesis can use the best available model for each task, fall back gracefully when providers fail, track what everything costs, and communicate its own health status in precise terms. The intelligence is not in any single model — it is in knowing which model to use, when, and what to do when that model is unavailable.

Cost discipline follows naturally from intelligent routing. When the system knows which tasks genuinely need expensive models and which can be handled by free-tier alternatives, spending becomes a consequence of good judgment rather than a constraint imposed from outside. The system does not auto-throttle or auto-degrade to save money. It makes intelligent selections by design, and the cost savings are a side effect of those selections being appropriate.

The other lesson was about provider diversity as a reliability strategy. A single-provider system has one uptime curve. A multi-provider system with intelligent fallback has the union of all providers' uptime curves. In practice, at least one provider is always available. The complexity cost of managing multiple providers is contained in two modules (registry and fallback chain) and never leaks into the rest of the system. For a system that is intended to run continuously and reliably, provider diversity is not over-engineering — it is the minimum viable reliability architecture.
