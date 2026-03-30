# Deferred Health Signal Gaps & Follow-Up Items

**Date**: 2026-03-13
**Context**: Health MCP signal audit identified ~30 medium/high-effort signals
not implemented in the quick-wins pass. Also captures related deferred decisions.

---

## 1. Deferred Health Signals (Post-V4)

### Routing
- **Model-specific cost tracking** — cost per model per call site (requires cost_events JOIN)
- **Budget burn rate** — projected daily spend extrapolated from recent window
- **Circuit breaker history** — trip/recovery timeline per provider (requires new table)
- **Latency percentiles** — p50/p95/p99 per provider per call site

### CC Sessions
- **Cost per model breakdown** — aggregate cost_usd by model_used for last 24h
- **Budget trend** — hourly budget usage trajectory (rising/stable/declining)
- **Background job type breakdown** — count by session purpose (reflection, triage, etc.)
- **Session error categorization** — group failures by error type

### Learning
- **Prediction calibration** — triage classifier accuracy vs actual learning outcomes
- **Signal attribution** — which signals most frequently trigger higher triage depths
- **Procedure health metrics** — procedure success rates, confidence distribution
- **Outcome correlation** — link interaction patterns to learning quality

### Memory
- **Retrieval success rates** — hit/miss ratio for memory activation queries
- **Auto-link success** — how often auto-linked memories prove relevant
- **Qdrant fallback frequency** — how often embedding-less fallback activates
- **Memory staleness** — age distribution of most-activated memories

### Resilience
- **Deferred work staleness effectiveness** — are staleness policies catching the right items
- **CC budget projection** — will budget last the hour at current rate
- **Recovery orchestrator metrics** — how often recovery runs, what it fixes

### Event Gaps (events that should be emitted but aren't)
- `memory.consolidation.completed` — when memory consolidation finishes
- `learning.procedure.created` / `updated` — procedure lifecycle
- `routing.provider.recovered` — when a circuit breaker closes after recovery
- `cc.session.budget_warning` — when approaching budget limit
- `outreach.delivery.failed` — when outreach delivery fails
- `awareness.ceiling.blocked` — when ceiling prevents deeper tick
- `resilience.deferred.expired` — when a deferred item hits staleness
- `surplus.task.dispatched` / `completed` — surplus task lifecycle
- `memory.embedding.batch_completed` — embedding backlog progress
- `routing.degradation.changed` — when degradation level changes

---

## 2. Tagging (#22)

**Decision**: Deferred to V4.

Infrastructure is ready (call site `22_tagging` exists in `model_routing.yaml`,
`ollama-3b` chain configured). Zero consumers — no code currently calls the
tagging call site. Wire when memory consolidation pipeline (V4) activates and
needs tag-based retrieval.

---

## 3. AZ Chat → CC Routing

**Decision**: Deferred. Separate design session required.

**Recommended approach**: Approach 3 — parallel `/ws/genesis` websocket endpoint.
Genesis dashboard gets its own chat input, AZ's chat becomes debug-only. Zero AZ
chat path changes.

**Why not Approach 1** (extension hook): `user_message_ui` extension hook fires
inside AZ's agent execution loop. It can inject context but cannot redirect the
message flow to a completely different execution engine (ConversationLoop → CCInvoker)
without blocking the agent loop.

---

## 4. Awareness `classify_depth` — Threshold Math

**Status**: NOT technical debt. Deliberate design.

The awareness loop's `classify_depth()` uses threshold math (signal scores vs
configured depth thresholds) rather than an LLM call. This is intentional:

- Awareness loop = pure perception layer. Zero LLM calls by design.
- Fires every 5 minutes. LLM calls would add cost and latency.
- Threshold math is deterministic and auditable.
- The LLM-backed classification is in retrospective triage (#29), which runs
  AFTER the tick and uses the router's fallback chain.

The build phases doc reference to "3B SLM classification" refers to call site
#29 (retrospective triage), not the awareness classifier itself.
