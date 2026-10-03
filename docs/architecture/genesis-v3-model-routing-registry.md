# Model Routing Registry

**Status:** Active | **Last updated:** 2026-03-22


> **What this is:** The authoritative assignment of specific models to every
> Genesis background LLM call site. If `models.md` is the menu, this document
> is the orders.
>
> **How it's used:** Read by Genesis during compute routing. Reviewed during
> Strategic reflection. Updated via the model review lifecycle (below).
>
> **Last reviewed:** 2026-03-04 — added routing implementation section, anti-sycophancy clarification

---

## Design Principles

1. **Free compute is opportunity capture, not load-bearing infrastructure.**
   The system MUST work entirely on paid models. Free compute reduces cost —
   but if every free source vanished tomorrow, Genesis still functions.

2. **Anti-sycophancy tasks require Anthropic models.** Quality calibration,
   self-assessment, strategic reflection — anything requiring honest pushback
   uses Claude (Opus or Sonnet). No Gemini. No model that validates weak
   reasoning.

3. **Cross-vendor diversity for adversarial/review tasks.** Fresh-eyes review
   and adversarial counterargument MUST use a different vendor than the primary
   model that produced the work.

4. **Output validation contracts over prompt-specific tuning.** Each call site
   defines what valid output looks like (schema, fields, value ranges). Model
   changes that break the contract are caught immediately. Contracts decouple
   "what we need" from "how we ask for it."

5. **Outsized-impact calls (⚡) always use paid models.** Call sites that
   steer expensive downstream processing — triage calibration, meta-prompting,
   task retrospective — never fall back to free compute.

6. **Privacy-first free compute.** Prefer providers whose free tier data is
   NOT used for model training. Mistral > Gemini for general background work.
   Gemini is reserved for its unique strengths (web search, long context).

7. **Default: escalate when uncertain.** A wasted Sonnet call costs cents. A
   bad judgment from a cheap model that gets stored in memory costs far more
   to fix downstream.

---

## How Routing Is Implemented

Genesis does NOT build a separate LLM call path. Agent Zero already has
`unified_call()` in `models.py` which wraps LiteLLM with rate limiting,
streaming, and cost tracking. Genesis layers on top:

```
Genesis call site (e.g., micro_reflection)
  → Look up call site in routing registry → get primary + fallback chain
  → Check provider health (health-mcp circuit breaker state)
  → Select first healthy provider in chain
  → Call agent.call_utility_model() or agent.call_chat_model()
    → AZ unified_call() → LiteLLM → provider API
  → On success: record cost event to SQLite, return result
  → On failure: try next provider in fallback chain
```

Each call site has an **output validation contract** — required fields,
value ranges, structural constraints. When evaluating new models for a call
site, run against the contract. If output passes validation, the model is
compatible. This decouples model selection from prompt engineering.

### Anti-Sycophancy vs. DeepSeek Clarification

Design Principle #2 states: "Anti-sycophancy tasks require Anthropic models."
However, the registry assigns DeepSeek V4 to outsized-impact call sites (#7
Task Retrospective, #15 Triage Calibration, #18 Meta-Prompting).

These are NOT contradictory. The distinction is:

- **Self-assessment** (am I doing well? am I honest? am I improving?) →
  requires anti-sycophancy → Anthropic only (Opus/Sonnet)
- **Analytical tasks** (what caused this failure? how should I calibrate
  triage?) → requires accuracy and reasoning → DeepSeek V4 is appropriate

DeepSeek is used for analysis where the risk is wrong conclusions, not
flattering ones. Anthropic is reserved for tasks where the risk is the model
telling Genesis what it wants to hear instead of the truth.

---

## The Registry

### Bucket 1: Classification — 3B Ollama / Programmatic

No serious LLM needed. Pattern matching, tagging, embedding.

| # | Call Site | Primary | Fallback | Freq | Context | Notes |
|---|----------|---------|----------|------|---------|-------|
| 1 | Signal collection | Programmatic (no LLM) | — | Every 5-min tick | N/A | Pure computation: rates, trends, moving averages |
| 2 | Triage classification | 3B Ollama | Programmatic rules | Every 5-min tick | ~2K | Few-shot classify signal → {ignore, log, micro, light, deep, critical} |
| 21 | Embeddings | qwen3-embedding:0.6b (Ollama) | — | On write | N/A | 1024-dim vectors for Qdrant |
| 22 | Tagging / parsing | 3B Ollama | Programmatic rules | Per input | ~2K | Entity extraction, metadata tagging on structured inputs |

### Bucket 2: Informed Extraction — Free compute primary, paid fallbacks

Background work that benefits from moderate intelligence. Free compute is the
default; paid models are the fallback when free sources are unavailable.

| # | Call Site | Primary | Free Alternatives | Paid Alt | Paid Fallback | Freq | Context | Notes |
|---|----------|---------|-------------------|----------|---------------|------|---------|-------|
| 3 | Micro reflection | Local 30B GPU | Groq Llama 70B, Mistral Large 3 free | — | GPT-5 Nano (~$0.05) | Every 5-min tick | ~4-8K | Quick pattern check on recent signals. Local 30B preferred |
| 8 | Memory consolidation | Mistral Large 3 free | Groq Llama 70B, Gemini Flash, OpenRouter | — | GPT-5 Nano (~$0.05) | Daily | ~8K | Deduplicate, compress, merge related memories |
| 9 | Fact / entity extraction | Mistral Large 3 free | Groq Llama 70B, Gemini Flash, OpenRouter | — | GPT-5 Nano (~$0.05) | Per ingestion | ~4K | Pull structured facts from unstructured input |
| 12 | Surplus brainstorm | ALL free compute | Local 30B, Mistral, Groq, Gemini, OpenRouter | — | Never pays | Opportunistic | ~8K | Maximize all free sources. If all free sources down, skip — never spend money |
| 13 | Morning report compilation | Mistral Large 3 free | Groq Llama 70B, Gemini Flash | — | GPT-5 Nano (~$0.05) | Daily | ~8K | Compile overnight observations + cognitive state into morning report |
| 19 | Outreach draft | Mistral Large 3 free | Groq Llama 70B, Gemini Flash, OpenRouter | — | GPT-5 Nano (~$0.05) | Per outreach | ~4K | Draft surplus insight / blocker / alert messages |
| 36 | Code auditor | OpenRouter Qwen 2.5 72B | Groq Llama 70B, Mistral Large 3 | — | Never pays | Opportunistic | ~8K | Surplus code review — evaluate codebase for bugs and quality issues |
| 37 | Infrastructure monitor | Groq Llama 3.3 70B | Mistral Large 3 free | OpenRouter Nemo ($0.02) | OpenRouter Nemo | Periodic | ~8K | Proactive infrastructure health — trend detection and forecasting |

#### Outsized-Impact Call Sites (⚡)

These Bucket 2 call sites steer expensive downstream processing. They are
**always paid** — never free compute, never local 30B. Getting these wrong
has cascading cost and quality consequences.

| # | Call Site | Primary | Paid Alt | Paid Fallback | Freq | Context | Why ⚡ |
|---|----------|---------|----------|---------------|------|---------|-------|
| 7 | Task retrospective ⚡ | DeepSeek V4 (~$0.14 blended) | Qwen 3.5 Plus ($0.40/$2.40) | GPT-5 Nano | Per completed task | ~8K | Root-cause classification routes to different learning pathways. Wrong classification → wrong lesson stored |
| 15 | Triage calibration ⚡ | DeepSeek V4 (~$0.14 blended) | Qwen 3.5 Plus ($0.40/$2.40) | GPT-5 Nano | Weekly | ~8K | Shapes accuracy of the most frequent call (#2 triage). Miscalibrated triage → wrong reflection depth on every tick |
| 18 | Meta-prompting ⚡ | DeepSeek V4 (~$0.14 blended) | Qwen 3.5 Plus ($0.40/$2.40) | GPT-5 Nano | Per deep/strategic reflection | ~4K | Determines what expensive models think about. Bad meta-prompt → wasted Sonnet/Opus calls |

### Bucket 3: Judgment & Synthesis — Haiku / Sonnet / Opus / Specialized

Tasks requiring genuine reasoning, honest assessment, or creative synthesis.

| # | Call Site | Primary | Paid Fallback | Freq | Context | Notes |
|---|----------|---------|---------------|------|---------|-------|
| 4 | Light reflection | Claude Haiku 4.5 ($1/$5) | Sonnet | On elevated urgency | ~8K | Quick but honest assessment of flagged signals |
| 5 | Deep reflection | Claude Sonnet 4.6 ($3/$15) | Opus | Weekly + high urgency | ~16K | Journal-quality analysis of patterns and trends |
| 10 | Cognitive state regen | GLM5 (~$0.30 blended) | Sonnet | Daily | ~8K | Regenerate compressed cognitive state summary |
| 11 | User model synthesis | Claude Sonnet 4.6 ($3/$15) | Opus | Weekly | ~16K | Update user preference/behavior model from observations |
| 14 | Weekly self-assessment | Claude Opus 4.6 ($5/$25) | — | Weekly | ~32K | Honest evaluation of Genesis's own performance. Anti-sycophancy critical |
| 16 | Quality calibration | Claude Opus 4.6 ($5/$25) | — | Weekly | ~16K | Audit recent outputs for quality regression. Anti-sycophancy critical |
| 17 | Fresh-eyes review | GPT-5.2 or Kimi 3 (switchable) | Sonnet | Per major decision | ~16K | Cross-vendor review of Genesis's reasoning. Must differ from primary vendor |
| 27 | Pre-execution assessment | Same model as task executor | — | Per task | ~4K | Sanity check before executing. Uses whatever model will run the task |
| 28 | Observation sweep | Qwen 3.5 Plus ($0.40/$2.40) | Sonnet | Per awareness tick | ~8K | Scan environment for noteworthy changes |

### Bucket 4: Strategic & High-Stakes — Opus / Cross-Vendor Rotation

The 5% of calls that shape the system's trajectory.

| # | Call Site | Primary | Paid Fallback | Freq | Context | Notes |
|---|----------|---------|---------------|------|---------|-------|
| 6 | Strategic reflection | Claude Opus 4.6 ($5/$25) | — | ~4-8/month | ~32K | Quarterly-depth strategic analysis. Anti-sycophancy critical |
| 20 | Adversarial counterargument | Grok 4 (initial pick) | Kimi 3, GPT-5.2 (rotatable) | Per major decision | ~16K | Devil's advocate review. MUST be different vendor than primary. User-configurable rotation |

### Bucket 5: Code Work — Claude Code CLI / DeepSeek V4

Code generation and modification. CLI uses Pro subscription (separate from API billing).

| # | Call Site | Primary | Paid Fallback | Freq | Context | Notes |
|---|----------|---------|---------------|------|---------|-------|
| 23 | Complex task planning | Claude Code CLI (subscription) | Opus API | Per complex task | ~32K | Architecture-level planning for multi-step tasks |
| 24 | Routine code | Claude Code CLI (subscription) | DeepSeek V4, Qwen 3.5 Plus | Per task | ~16K | Standard code generation and modification |
| 25 | Complex code | Claude Code CLI (subscription) | Opus API, DeepSeek V4 | Per task | ~32K | Difficult code requiring deep reasoning |

### GROUNDWORK V5: Identity Evolution

Not active in V3. Infrastructure only.

| # | Call Site | Primary | Freq | Notes |
|---|----------|---------|------|-------|
| 26 | Identity evolution proposals | Claude Opus 4.6 ($5/$25) | 0 calls in V3 | GROUNDWORK(V5): Static identity in V3. Opus required for self-modification proposals |

#### Runtime Assignments (2026-03-07)

The agentic runtime design (`docs/plans/2026-03-07-agentic-runtime-design.md`)
assigns each call site to a runtime. "Effort" is now a routing dimension alongside
model selection (e.g., "Sonnet high thinking" vs just "Sonnet").

| # | Call Site | Runtime | Model + Effort |
|---|----------|---------|---------------|
| 2 | Triage | AZ (API) | Free API |
| 3 | Micro reflection | AZ (API) | Free API / LM Studio 30B |
| 4 | Light reflection | CC background | Haiku, low effort (moved from API to CC pipeline 2026-03-22) |
| 5 | Deep reflection | CC background | Sonnet, high thinking |
| 6 | Strategic reflection | CC background | Opus, high thinking |
| 7 | Task retrospective | CC background | Sonnet, high thinking |
| 8 | Memory consolidation | AZ (API) | Free API |
| 9 | Fact extraction | AZ (API) | Free API |
| 10 | Cognitive state | CC background | Sonnet, high thinking |
| 11 | User model synthesis | CC background | Opus, high thinking |
| 12 | Surplus brainstorm | AZ (API) | Free API only |
| 13 | Morning report | AZ (API) | Free API |
| 14 | Weekly self-assessment | CC background | Opus, high thinking |
| 15 | Triage calibration | CC background | Sonnet, high thinking |
| 16 | Quality calibration | CC background | Opus, high thinking |
| 17 | Fresh-eyes review | AZ (API) | Grok 4 / Kimi 2.5 / GPT-5.2 (cross-vendor) |
| 18 | Meta-prompting | Deferred (V4) | — |
| 19 | Outreach draft | AZ (API) | Free API |
| 20 | Adversarial counterargument | AZ (API) | Grok 4 / Kimi 2.5 / GPT-5.2 (cross-vendor) |
| 21 | Embeddings | AZ (API) | ollama-embedding |
| 22 | Tagging | AZ (API) | Free API |
| 27 | Pre-execution assessment | CC foreground | Absorbed into CC system prompt reasoning |
| 28 | Observation sweep | CC background | Sonnet, high thinking |

---

## Free Compute Sources

Priority order for Bucket 2 tasks. Prefer stronger models at the same cost.

### 1. Local 30B GPU (intermittent)

- **Role:** Micro reflection (#3) and surplus brainstorm (#12) ONLY
- Not available 24/7 — separate host from the Ollama container
- Awareness Loop pings GPU health every 5-min tick
- Timeout: 60s for micro reflection, 120s for heavier tasks
- On failure: mark GPU as "potentially down," don't send more until health check passes
- **Last resort** for all other Bucket 2 tasks (only if ALL free APIs are down)

### 2. Mistral Large 3 Free — DEFAULT for background work

- **Access:** All Mistral models including Large 3 (strongest)
- **Rate limits:** 2 RPM, 1B tokens/month
- **Privacy:** Data NOT used for model training (advantage over Gemini)
- **Why default:** Strongest free model available. 2 RPM is sufficient for
  scheduled background tasks that fire one at a time. Privacy-friendly.

### 3. Groq Free — Llama 3.3 70B

- **Rate limits:** 30 RPM, 1,000 RPD
- **Use when:** Speed or burst matters, or Mistral rate limit hit

### 4. Gemini 3 Flash Free — Narrowed Use Only

- **Rate limits:** ~250 RPD, 15 RPM (as of Feb 2026)
- **ONLY use for:**
  - Web search / recon (Google Search grounding)
  - Long-context ingestion (1M token window)
  - Simple code grunt work (plan already written, just generate code)
- **NOT for:** General extraction, compilation, drafting, judgment, quality
  assessment, anything requiring honest evaluation
- **Privacy concern:** Free tier data MAY be used for Google model training
- **Sycophancy risk:** Validates premises instead of challenging them. Never
  use for review or assessment tasks.

### 5. OpenRouter Free — 29 Models

- **Rate limits:** 20 RPM, 200 RPD
- **Use as:** Overflow when other free sources are exhausted

---

## Opus Budget Analysis

Active call sites using Opus API:

| # | Call Site | Estimated Frequency | Avg Tokens | Est. Cost/Call |
|---|----------|-------------------|------------|----------------|
| 6 | Strategic reflection | ~4-8/month | ~40K in, ~4K out | ~$0.30 |
| 14 | Weekly self-assessment | ~4/month | ~30K in, ~3K out | ~$0.23 |
| 16 | Quality calibration | ~4/month | ~16K in, ~2K out | ~$0.13 |

**Total: ~12-16 Opus API calls/month, estimated ~$3.30-4.80/month.**

Call site #26 (identity evolution) is GROUNDWORK V5 — 0 calls in V3.

Code work (#23/#25 fallback to Opus API) is rare — CLI handles most code.
Estimated ~2-4 API fallback calls/month, adding ~$0.50-1.00.

---

## GPU Machine Failure Handling

All background LLM calls are **idempotent** — they analyze input and produce
output with no stateful transactions.

- **Timeout per call:** hard 120s wall-clock cap (`_DEFAULT_TIMEOUT_S`),
  enforced via `asyncio.wait_for` with `num_retries=0` so litellm's internal
  retries cannot stack past it (they previously did — ~361s on a 120s
  timeout). Callers may override per call.
- **On timeout:** classified `TIMEOUT` and failed fast to the next provider in
  the chain — a hung provider is NOT retried in place (retrying a hung
  provider only multiplies the wall-clock). The circuit breaker still records
  the failure, so a repeatedly-hanging provider trips OPEN and is skipped.
- **Awareness Loop 5-min tick:** Pings GPU health endpoint
- **On failure:** Mark GPU as "potentially down." Stop sending tasks until
  the next health check passes
- **Recovery:** GPU health check passes → resume routing eligible tasks

---

## Configurable User Preferences

These assignments are user-switchable via UI (planned) or manual override:

| # | Call Site | Options | Current Pick |
|---|----------|---------|-------------|
| 17 | Fresh-eyes review | GPT-5.2, Kimi 2.5 | GPT-5.2 |
| 20 | Adversarial counterargument | Grok 4, Kimi 2.5, GPT-5.2 | Grok 4 |

The full registry is user-editable. These two are called out because they
have explicit rotation pools designed for user preference.

---

## Model Review Lifecycle

1. **Recon-MCP** scans for new model releases, pricing changes, benchmark updates
2. **Strategic reflection** reviews accumulated model recon findings
3. **Genesis proposes** changes with evidence + output contract validation results
4. **User approves/rejects** via UI
5. **Output validation contracts** (not prompt tuning) determine model compatibility

### Output Validation Contracts

Each call site defines what valid output looks like:
- Required fields and schema
- Value ranges and types
- Structural constraints

When swapping a model, run the new model against the contract. If output
passes validation, the model is compatible. This decouples "what we need"
from "how we ask for it" and serves as acceptance tests for model changes.

---

## Cross-References

- **Model catalog (the menu):** `docs/reference/models.md`
- **Runtime YAML config:** `config/model_routing.yaml` — the machine-readable registry
  loaded by `genesis.routing.config.load_config()`. 13 providers, 23 call sites.
  Some registry entries use placeholder providers (e.g., `gpt-5-nano` for GPT-5.2/Grok 4/Kimi 2.5)
  until those APIs are available.
- **Routing implementation:** `src/genesis/routing/` — Phase 2 package (router, circuit
  breaker, cost tracker, dead-letter queue, degradation tracker, retry logic)
- **Runtime operations (config, failures, degradation):** `docs/plans/2026-03-03-model-routing-operations-design.md`
- **Resilience patterns (retry, circuit breaker, dead-letter):** `docs/architecture/genesis-v3-resilience-patterns.md`
- **Compute hierarchy (design context):** `docs/architecture/genesis-v3-autonomous-behavior-design.md` → Pattern 1
- **Build plan:** `docs/architecture/genesis-v3-build-phases.md` → Phase 2
- **Agent Zero integration:** `docs/architecture/genesis-agent-zero-integration.md` → Compute Routing
- **Gemini-specific routing:** `docs/reference/gemini-routing.md` — YouTube video analysis,
  hallucination avoidance, model deprecation patterns

> **Addendum (2026-03-09) — Model Deprecation as First-Class Failure Mode:**
> Every model string in the routing config is a staleness risk. Unlike Claude
> Code (which auto-updates), Genesis's background API calls pin specific model
> IDs. When providers release new versions, older ones get throttled or removed
> — often without notice. When any routed call fails, the first diagnostic
> question must be: "Is this model still valid?" before debugging application
> logic. This is expected industry behavior for the foreseeable future and must
> be a core assumption in routing operations. See `config/model_routing.yaml`
> header and `docs/reference/gemini-routing.md` for operational details.

---

## Related Documents

- [genesis-v3-build-phases.md](genesis-v3-build-phases.md) — Phase 2: compute routing

## Reload, model replacement and persisted health

`Router.reload_config()` prepares the production delegate, rate gates and breaker
bindings before publishing configuration. Each request captures its configuration,
delegate, pacing and full breaker map before its first await. An old request
therefore finishes with the model identity it actually requested, even if a provider
is removed or repointed while it waits. Custom delegates retain their existing
`CallDelegate` contract; the router still supplies the generation's model ID.

Existing account pacing locks and last admissions are shared across reloads and
registered alias renames; configured RPM changes update that shared gate.
Dashboard toggles select and change the current breaker under the registry lock,
so reload cannot discard a reported operator action. Routed trip events carry a
health identity; escalation rejects events for retired identities, including events
held up by another event-bus listener.

The runtime's registry object stays in place. Replaced or renamed breakers receive
copies of raw hold state with the new provider binding; old persistence and recovery
callbacks are detached. Recovery after a rename reports the current alias. Registry
updates own their provider dictionary rather than mutating the delegate's old config.
Previously, shared-dictionary mutation indirectly refreshed the delegate for normal
same-alias reloads; it did not provide coherent in-flight routing.

Persisted health carries SHA-256 request identity (provider type, model ID and
endpoint) plus bounded failure provenance. It stores neither raw errors nor endpoint
credentials. A changed identity clears only retirement-only history evidenced by a
provider response with HTTP 410 and an exact affirmative model-retirement statement.
The exact observed `litellm.APIError: ` wrapper is removed once before checking;
unknown wrappers and appended qualifications remain ambiguous.
Every failed retry must establish retirement; a mixed 500/410 visit stays held.
Negated, account-related or otherwise ambiguous statements stay held. Persisted
retirement provenance must agree with its saved identity and transient call failure. A generic
410, 404, authentication, quota, entitlement, operator, mixed or ambiguous legacy
hold stays held. Alias-only renames do not count as model replacement. Existing
operator hold expiry and restart backoff caps still apply. Returning to a sole
legacy alias also preserves its hold.

Daily usage remains independent of model health and keyed by the exact configured
provider name. Same-name model replacement and late completion preserve its row;
removing an explicitly independent alias does not transfer its counts to a survivor.
UTC rollover, persistence and limit semantics are unchanged. A reentrant lock and
copied pure views protect concurrent dashboard reads. Registered cross-alias budget
migration is deferred: it needs durable historical ownership captured with requests,
not inference from the current alias set. A rename can therefore undercount a shared
account until UTC rollover, as before this change; vendor rate limits remain the backstop.

Runtime provider probes and dashboard health snapshots use the router's current
config and breaker bindings. A probe started before reload is discarded rather
than changing replacement health; reload invalidates cached routing health.

Probe support, credential configuration and breaker authority are separate
evidence. The health API's provider results expose `configured`,
`probe_supported` and `can_affect_breaker` alongside the existing reachability,
model-listing, latency and error fields. Unsupported probing does not imply a
missing key or outage: a configured chain entry omits `probe_status`, records
`probe_reason: unsupported_probe`, and retains the established breaker-based
display. Actual missing credentials retain `not_configured/no_api_key`.

NVIDIA's public `/v1/models` catalog is supported but observational only
(`can_affect_breaker: false`), for both successful and failed observations.
Its listing cannot validate credentials, prove callable completion access or
clear held health state. Other supported providers retain their existing
probe-synchronization policy. No routing chain, model default, daily usage,
failure provenance or breaker-restoration policy changes with this distinction.

Verification uses local HTTP responses and mocked provider completions with the production delegate and
disposable SQLite/JSON state. `tests/test_routing/test_routing_generation.py` covers
reload/restart identity, suspension at budget/rate/retry/completion, late callbacks,
hold preservation, preparation failure and alias accounting. It makes no provider
accuracy claim and performs no live Fusion inference.

`tests/test_routing/test_routing_generation_http.py` verifies real LiteLLM HTTP
dispatch, old/new result identity, token usage and SQLite cost recording against a
localhost server. It performs no external model inference.

Captured dashboard health uses the full `CircuitBreakerRegistry.health_view()`
interface, including breaker aggregation and essential-site coverage derived from
the captured configuration. Constructing a view does not restore or persist state.

A proven retirement replacement also starts a fresh persisted incident namespace.
Legacy incidents keep their existing hashes; authentication, quota, operator and
ambiguous holds retain their namespace and accumulated escalation evidence. Every
retirement transition gets a new namespace, including a switch back to a previous
model, so historical trip counts and notification acknowledgments cannot suppress
or prematurely escalate the replacement. Restart restores the namespace.

Retirement resolves the old incident's failure and notification rows with an
explicit `auto-retired` reason, never a claim of observed recovery. Deferred old
observation/notification writes check the namespace and retire an obsolete write.
The awareness notification sweep reads the current namespace, so its outage clock
and deduplication apply to the current incident. Existing separately committed
recovery/resolve failure behavior remains unchanged; a database failure is logged,
and retirement resolution is not a new transactional guarantee.

The shared current-incident predicate treats a missing provider binding as obsolete,
distinct from a live legacy incident whose namespace is `None`. This applies before
and after both deferred failure and notification writes, including registered alias
renames and removal while a database write waits. Cleanup resolves the obsolete
row without claiming a successful provider call.

Ordinary registered renames preserve a persisted incident owner (the original
provider alias) as well as its namespace. Escalation hashes, outage timestamps and
user acknowledgments therefore remain valid across rename/restart/rollback without
rewriting observation rows. Fresh configured-alias lookup remains direct-first;
historical evidence resolves by its owned alias/namespace pair. Adding a formerly
used alias as an independent sibling assigns a distinct namespace if needed and
does not take ownership of the continuing incident. Partial HALF_OPEN recovery
persists changed failure provenance even before the two-success close threshold.

Resilience, call-site and API-key rendering use the same captured breaker view.
Snapshot rendering does not publish stale cloud-axis updates; the awareness tick
continues deriving the live cloud axis independently from the live registry.


State restoration suppresses persistence until all saved bindings and incident fields
have been applied, then atomically saves the completed registry. CLOSED pre-trip
failures retain validated provenance with their counters without asserting a call-opened
hold. Removing a provider retires only historical incidents that have no continuing
owner; an ordinary rename keeps its incident intact.

Captured pacing policy is immutable across reload: old and new generations share
admission serialization and timestamps, while each retains its configured RPM.
Health snapshots project cloud resilience only while their routing generation is
current, under the reload publication lock. Full and readonly runtimes inject this
projector; standalone live-registry health services retain their existing updates.
