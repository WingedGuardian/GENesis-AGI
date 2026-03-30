# Genesis v3: Pre-Implementation Gap Assessment

**Date:** 2026-02-23
**Status:** Open — must be addressed before implementation begins
**Inputs:** V2 architectural critique (Feb 2026), v3 dual-engine plan (superseded by agentic-runtime-design), v3 autonomous behavior design, consistency pass review

> This document identifies gaps, risks, and unresolved questions in the Genesis v3
> architecture that must be addressed before building begins. It is organized by
> severity: blockers first, then risks that need mitigation plans, then questions
> that need answers.

---

## How V3 Addresses the V2 Critique

Before enumerating new gaps, a brief accounting of the seven V2 blindspots and
where they stand after V3 planning.

| # | V2 Blindspot | V3 Status | Notes |
|---|-------------|-----------|-------|
| 1 | Intelligence before structure (navigator duo built before budget/safety) | **Designed, not proven** | Governance checks, quality gates, budget checks are in the architecture. Same pattern could repeat in implementation if cognitive layer is built first. |
| 2 | Sequential queuing contradicts delegation | **Partially addressed** | Parallel sub-agents within a task (browser + claude_code + computer_use). Parallel independent tasks not specified. |
| 3 | LLM-first applied inconsistently (structural enforcement gaps) | **Well addressed** | Programmatic governance at capability boundaries. Drive weight bounds are hard limits. Autonomy regression is rule-based. |
| 4 | Retrospective system assumes data that doesn't exist | **Partially addressed** | Quality gates catch bad execution in real-time. Self-Learning Loop has broader signal surface. Cold-start bootstrapping still a gap. |
| 5 | WebUI ahead of backend | **Reset** | Fresh codebase. No UI exists yet. Sequencing discipline needed. |
| 6 | Two-phase intake undesigned | **Still a gap** | V3 doesn't address the ambiguity between user conversation and user task request. No structured scoping before decomposition. |
| 7 | Escape hatch frameworks not evaluated | **Resolved** | V3 IS the framework migration. Agent Zero chosen, Claude SDK + OpenCode integrated. |
| CB | No clear milestone for supervised → autonomous | **Well addressed** | Autonomy hierarchy L0-L6, per-category tracking, evidence thresholds, regression mechanism. |

**Carried forward:** #1 (risk of repeating pattern), #2 (parallel tasks), #4
(cold start), #6 (intake design).

---

## Tier 1: Blockers (Must resolve before writing code)

### B1. No Build Priority / Phase Plan

**Problem:** The v3 architecture describes a rich system — three cognitive layers,
4 MCP servers, autonomy hierarchy, engagement tracking, drive-weighted scoring,
procedural memory, signal weighting. But there is no phased build plan that says
"build X first, then Y, then Z."

V2 had an explicit tiered action list: active bugs → foundation → task safety →
UX → infrastructure. V3 has no equivalent. Without one, the V2 pattern repeats:
sophisticated capabilities get built before the structural foundations they depend on.

**What's needed:** A phased implementation plan with clear milestones:
- Phase 0: Agent Zero container + memory-mcp + basic CLAUDE.md handshake (already scoped)
- Phase 1: ? (What's the minimum system that does something useful?)
- Phase 2+: ? (What order do the cognitive layers come online?)

Each phase needs a "done when" definition and a "what this enables" statement.

**Risk if skipped:** We build the Reflection Engine and Self-Learning Loop before
the Awareness Loop reliably collects signals and health-mcp can detect crashes. Same
mistake as navigator duo before budget enforcement.

---

### B2. Task Intake Design Missing

**Problem:** Carried from V2 blindspot #6. The v3 architecture has rich execution
infrastructure (execution traces, governance, quality gates) but no design for how
tasks enter the system:

- How does the system distinguish "user conversation" from "user task request"?
- How does it scope a task before committing decomposition tokens?
- How does it confirm intent before spawning sub-agents?
- How does it reject infeasible tasks early?
- What happens when the user's request is ambiguous?

Agent Zero may have native patterns for this, but the ambiguity problem is
architectural, not framework-level. A user saying "maybe look into X when you get
a chance" vs. "build X now" requires judgment that needs to be designed, not
discovered during implementation.

**What's needed:** A concrete intake flow:
1. Signal detection (is this a task or conversation?)
2. Scoping (what tools, what budget, what timeline?)
3. Confirmation (user approves scope before execution begins)
4. Feasibility check (can we actually do this with available capabilities?)

This can be LLM-driven (consistent with design philosophy) but the *prompt
architecture* needs to be designed, not improvised.

**Risk if skipped:** System either misses legitimate tasks buried in conversation,
or aggressively converts casual remarks into expensive task executions.

---

### B3. Minimum Viable Parity Checklist for V2 → V3 Migration

**Problem:** The dual-engine plan has a migration section but no explicit "minimum
viable parity" definition. V2 has working capabilities that users depend on:

- 13-job dream cycle (nightly)
- Plan-based model routing with multi-provider failover
- Episodic + semantic + structured memory with FTS5 search
- Self-escalation
- Cost tracking and alerting
- WhatsApp channel integration
- Heartbeat with cognitive context
- Recon cron jobs (email, web, GitHub, model landscape)

**What's needed:** A checklist of V2 capabilities with three columns:
1. **Port** — must work in V3 before V2 can be decommissioned
2. **Replace** — V3 has a better mechanism, but it must be working first
3. **Drop** — V2 capability that's intentionally not carried forward

Without this, there's a risk of turning off V2 before V3 can actually do
everything the user relies on, or of porting things that shouldn't be ported.

**Risk if skipped:** Regression during migration. User loses capabilities they
depend on (morning brief, cost alerts, memory recall) with no timeline for
restoration.

---

## Tier 2: Risks Needing Mitigation Plans

### R1. Cold-Start Bootstrapping

**Problem:** Multiple V3 systems depend on accumulated data to function well:

| System | Depends on | At launch has |
|--------|-----------|---------------|
| Self-Learning Loop | Engagement signals, task outcomes | Nothing |
| Salience calibration | History of what user found useful | Nothing |
| Drive weight adjustment | Evidence of what drives matter | Hardcoded defaults |
| Procedural memory | Extracted patterns from past tasks | Nothing |
| Autonomy advancement | Evidence threshold per category | L1 everywhere |
| time_multiplier curves | History of reflection timing | Hardcoded curves |
| Outreach timing | User engagement patterns | Default heuristics |

The system will be *dumbest* on day 1, when first impressions matter most.

**What's needed:** An explicit cold-start strategy:
- What are the sensible defaults for each system? (Partially done — drive weights
  and engagement heuristics are specified.)
- Can we seed any data from V2? (Retrospectives, memory, lessons, cost history,
  engagement patterns from WhatsApp message logs.)
- What's the "first week experience" — what does the system do that's useful before
  calibration data exists?
- At what data volume does each system become meaningful? (10 tasks? 100 outreach
  attempts? 30 days of engagement data?)

**Risk if unmitigated:** System makes bad decisions early, user loses trust,
autonomy advancement stalls because engagement signals are negative.

---

### R2. Cognitive Layer Testability

**Problem:** The cognitive architecture introduces systems that are hard to verify:

- **Reflection Engine depth selection:** Does the signal-weighted scoring + calendar
  floors + time_multiplier actually trigger the right depth at the right time?
- **Drive-weighted scoring:** Do the 4 drives produce sensible priority ordering?
- **Salience evaluation:** Does the LLM correctly assess what matters to the user?
- **Governance checks:** Do programmatic pre-spawn checks actually prevent bad
  sub-agent spawns?

Traditional unit tests work for governance checks (deterministic). But the
Reflection Engine's adaptive depth selection is a continuous optimization problem
with no clear "correct answer" — only "reasonable in context."

**What's needed:**
- **Deterministic tests** for governance checks, budget enforcement, autonomy
  level gating, drive weight bounds (these are code, test them like code)
- **Scenario-based integration tests** for depth selection: "Given these signals,
  at this time, with this history, the system should trigger [Light/Deep/Strategic]
  reflection." Build a library of 20-30 scenarios with expected depths.
- **Logging and observability** for the cognitive layer: every depth selection
  should log the scoring breakdown so a human can audit "why did it choose Deep
  here?" This is the cognitive equivalent of the WebUI activity stream.
- **A/B comparison capability**: Run the same signal set through two versions of
  the scoring algorithm and compare outputs. Essential for tuning.

**Risk if unmitigated:** Cognitive layer makes subtly wrong decisions that are hard
to notice — reflecting too often (wasting tokens) or too rarely (missing important
signals), with no way to diagnose which.

---

### R3. Agent Zero as Single Framework Dependency

**Problem:** V3 bets the entire foundation on Agent Zero. The framework is
described as "evolving rapidly" — which means:

- APIs may change between versions
- Features assumed in the architecture may not exist yet
- Bugs in Agent Zero become bugs in Genesis
- If the project stalls or pivots, Genesis is stranded

The dual-engine plan acknowledges this but the mitigation ("we've built on
frameworks before") is thin.

**What's needed:**
- **Phase 0 validation** (already scoped) should include a specific compatibility
  checklist: Can Agent Zero do dynamic CLAUDE.md injection? MCP server lifecycle
  management? Extension hooks at the required points? Sub-agent spawn with
  governance middleware?
- **Abstraction layer** for the 3-4 Agent Zero APIs Genesis depends on most
  heavily. Not a full abstraction (that's over-engineering), but thin wrappers
  around MCP client management, extension lifecycle, and sub-agent dispatch.
  If Agent Zero changes an API, you change one wrapper, not 50 call sites.
- **Fallback assessment**: If Agent Zero can't do X, what's the workaround?
  For each critical capability, have a Plan B that doesn't require switching
  frameworks entirely.

**Risk if unmitigated:** A breaking Agent Zero update or missing feature forces
an emergency architectural pivot mid-build.

---

### R4. Parallel Task Execution

**Problem:** Carried from V2 blindspot #2. V3 supports parallel sub-agents within
a task, but the "CEO delegation" model requires parallel independent tasks:

- "Research X" running while "build Y" executes
- "Monitor Z" as a long-running background task alongside foreground work
- User submits 3 tasks, expects all to progress

This requires:
- Task-level resource allocation (how much budget per concurrent task?)
- Conflict detection (two tasks editing the same file?)
- Priority scheduling (which task gets the next available sub-agent slot?)
- User attention management (how to report progress on 3 tasks without flooding?)

**What's needed:** Not necessarily a full design now, but an explicit decision:
1. Is parallel task execution in scope for V3 initial release?
2. If yes, at what phase does it come online?
3. If no, what's the honest UX for sequential queuing? (Don't pretend it's
   delegation if it's a FIFO queue.)

**Risk if unmitigated:** Same V2 problem — user delegates 3 things, only #1
progresses. Stalled tasks block the queue silently.

---

### R5. Code Execution Economics

> **Resolved (2026-03-07):** The dual-runtime architecture
> (`docs/plans/2026-03-07-agentic-runtime-design.md`) resolves this gap. Claude Code
> (Max subscription) replaces Claude SDK as the code execution engine at flat rate —
> no per-token cost, no cost estimation needed, no user confirmation for routine work.
> OpenCode is deferred to V5. The three-engine cost routing below is superseded.

**Original problem (historical):** The three-engine architecture originally assumed
Claude SDK usage would be covered by a Pro/Max subscription. Per Anthropic's TOS,
this is not the case — Claude Agent SDK bills at API rates (~$15/$75 per MTok for
Opus). This invalidates the economic premise of using the SDK as the default code
engine.

**What this changed:**
- Claude SDK becomes a premium tool for complex work, not the default code path
- OpenCode is promoted from "fallback" to "workhorse" for routine code tasks
- The Claude Code as subprocess (uses subscription OAuth) is an experimental option
  worth validating in Phase 0, but could be closed by Anthropic at any time
- Cost consciousness becomes a first-class governance feature: before spawning
  Claude SDK, the system must estimate cost and get user confirmation

**Original mitigation (superseded by CC subscription model):**
- **Phase 0 validation**: Test Claude Code subprocess invocation from Agent Zero.
  Can it capture output? Handle errors? Resume sessions? If this works, it's the
  economically optimal path for subscription holders.
- **Cost estimation model**: Before invoking Claude SDK, estimate token usage based
  on task complexity (number of files, estimated turns, model tier). Present this
  to the user as part of task confirmation.
- **Routing logic**: A decision tree for code task routing: routine → OpenCode,
  complex → CLI subprocess (if available) → SDK (with cost notification) →
  Bedrock/Vertex (volume pricing).
- **Budget tracking per engine**: Track spend separately for Agent Zero LiteLLM,
  Claude SDK, OpenCode, and CLI subprocess so the user can see where money goes.

**Risk if unmitigated:** ~~The system burns API dollars on routine code tasks that
could be handled by cheaper alternatives.~~ Resolved — CC subscription is flat rate.

See `genesis-v3-dual-engine-plan.md` §"Code Execution Economics" for the full
revised engine role breakdown.

---

### R6. Complexity Budget

**Problem:** The v3 architecture describes a system with:
- 3 cognitive layers (Awareness Loop, Reflection Engine, Self-Learning Loop)
- 4 reflection depths (Micro, Light, Deep, Strategic)
- 4 MCP servers (memory, recon, health, outreach)
- 4 drives with weighted scoring
- 7 autonomy levels with regression
- Per-channel engagement heuristics
- Signal weighting with time_multiplier curves
- Execution traces with governance and quality gates
- CLAUDE.md dynamic injection with handshake protocol

Each piece is well-motivated individually. Together they form a system that is
ambitious beyond what most production AI systems attempt.

**What's needed:** An explicit complexity budget:
- How many of these systems can be built, tested, and debugged simultaneously?
- What's the maximum number of interacting systems that one developer can
  reason about?
- Which systems are independent (can be built and tested in isolation) vs.
  tightly coupled (must be built together)?
- What's the "walking skeleton" — the thinnest possible slice through all layers
  that demonstrates the architecture works end-to-end?

The walking skeleton is particularly important. Rather than building each layer
to completion before starting the next, build a thin vertical slice:
Awareness Loop (1 signal) → Reflection Engine (Light only) → Self-Learning Loop
(1 feedback type) → memory-mcp (recall + store) → one outreach channel.

**Risk if unmitigated:** Building 20% of each system instead of 100% of the
critical path. Everything is half-working, nothing is useful.

---

### R7. Infrastructure Resilience (Transient Fault Handling)

**Problem:** The v3 architecture describes what happens when API calls succeed or
fail cleanly. Real-world LLM APIs are messy — they timeout, return 429s, drop
connections, return partial/malformed responses, and experience provider outages.
Without explicit resilience patterns, transient failures cascade:

- The Awareness Loop's 5-minute tick makes multiple API calls. If they all fail
  and retry simultaneously, they hit the same rate limit again (retry storm).
- Ollama (sibling container) goes down → every tick tries embeddings → fails →
  retries 3x → fails again → next tick does the same. Without a circuit breaker,
  this continues indefinitely.
- A memory write times out after succeeding server-side → retry creates a
  duplicate record. Without idempotent writes, the memory accumulates duplicates.
- A reflection produces observations but the write fails after all retries →
  observations are lost. Without a dead-letter mechanism, cognitive work is wasted.

**What's needed:**

A dedicated resilience patterns design covering:
1. **Exponential backoff + jitter** as default retry policy for all API calls
2. **Circuit breaker** per provider endpoint (Ollama, Gemini, paid APIs, Qdrant)
3. **Per-provider retry budget** to prevent retry storms
4. **Idempotent write operations** for all MCP servers (upsert semantics)
5. **Dead-letter staging** for operations that produce outputs but fail to persist
6. **Graceful degradation levels** when compute is impaired
7. **Error categorization** (transient vs degraded vs permanent) before retry

**Status:** Fully implemented. Tactical patterns in `genesis-v3-resilience-patterns.md`
(Phase 2). System-level resilience in `genesis-v3-resilience-architecture.md` with
composite state machine (4 axes: cloud, memory, embedding, CC), deferred work queue,
embedding backlog recovery, CC budget management, recovery orchestration, and
out-of-band status file. Code in `src/genesis/resilience/` (7 modules, 132 tests).

**Implementation phase:** Phase 2 (tactical: retry/circuit breaker/budget, complete).
Cross-cutting resilience layer (state machine, deferred work, recovery) implemented
alongside Phase 8 prep. Remaining: outreach notification delivery (Phase 8),
autonomy-gated escalation (Phase 9).

**Risk if unmitigated:** The cognitive layer silently degrades under routine
infrastructure instability. The system either wastes budget retrying permanent
errors, or fails to retry transient ones and produces unnecessary failures.
Duplicated memories and lost observations corrupt the learning system over time.

**Source:** Microsoft Azure transient fault handling best practices, Confluent
agentic AI reliability analysis.

---

## Tier 3: Questions Needing Answers

### Q1. What Does Genesis Do in Week 1?

The architecture describes what Genesis will eventually be capable of. But what
does it do on day 1 that makes the user's life better? The "AGI-like assistant/
friend/coworker/peer/expert/mentor" vision is a product vision. The architecture
supports it mechanically. But the first interaction needs to deliver value.

Concrete question: If V3 Phase 1 is complete and V2 is turned off, what can the
user do that they couldn't do before? If the answer is "the same things but on a
better framework," that's a valid answer — but it should be stated explicitly so
expectations are calibrated.

### Q2. How Does V2 Memory Migrate?

The dual-engine plan says memory is ported as an MCP server. But V2 has:
- Qdrant vectors (episodic memory)
- SQLite structured facts, lessons, observations, retrospectives
- FTS5 search indexes
- Identity files (SOUL.md, USER.md, MEMORY.md, AGENTS.md, POLICY.md)
- Session metadata with routing plans and extractions

Which of these migrate as-is? Which need schema changes? Which get
re-indexed? What's the data migration plan and how do we verify nothing
was lost?

### Q3. What's the Full LLM Cost Model?

Two cost dimensions need estimation:

**Cognitive layer (background):** The autonomous behavior doc specifies models for
each depth (Micro: local/Flash, Light: Haiku, Deep: Sonnet, Strategic: Opus) but
doesn't estimate steady-state cost. With Micro every 30min-1h, Light every 2-4h,
Deep every 1-2 days, Strategic every 3-7 days, plus engagement tracking, salience
evaluation, quality gates, governance checks — what's the monthly cognitive burn?
V2 optimizes for free-tier models. If V3's cognitive layer costs $50/month, that's
a design constraint.

**Task execution (foreground):** With the Claude SDK billing at API rates (see R5),
task execution costs are no longer subsidized by subscription. A day of heavy code
work could cost $20-50 at Opus API rates. The cost model needs to account for:
- What percentage of tasks route to Claude SDK vs. OpenCode vs. CLI subprocess?
- What's the realistic monthly spend for an active user doing 2-3 code tasks/day?
- At what point does the API spend exceed the value of the subscription entirely?
- Can the Claude Code subprocess (if it works) offset SDK costs enough to matter?

### Q4. How Does the System Handle Prolonged User Absence?

The design assumes regular user interaction for engagement signals, autonomy
advancement, and salience calibration. What happens during a week of no
interaction?

- Do time_multipliers keep escalating reflection depth indefinitely?
- Does the system keep sending outreach with no engagement feedback?
- Is there a "quiescent mode" where the system reduces activity to maintenance
  levels?
- What's the token burn rate during prolonged absence?

### Q5. Error Propagation Across Cognitive Layers

The Awareness Loop feeds the Reflection Engine feeds the Self-Learning Loop. If
the Awareness Loop miscollects a signal (false positive on a health alert), the
Reflection Engine may trigger an unnecessary Deep reflection, and the Self-Learning
Loop may learn the wrong lesson from the outcome. Error propagation through
layered systems is a known problem.

What's the error isolation strategy? Can a bad signal be corrected retroactively?
Does the Self-Learning Loop have any mechanism to detect that its training data
was corrupted by a false signal?

### Q6. Knowledge Base Groundwork Scope

The memory-mcp design now includes placeholder tools for a post-v3 knowledge base
(immutable reference material — course content, specs, docs — stored alongside but
separate from episodic memory). The v3 groundwork requirements are documented in the
memory-mcp section of the autonomous behavior design.

Questions to resolve during v3 implementation:
- What's the minimum viable Qdrant client abstraction that supports multiple named
  collections without over-engineering? (Parameter on existing methods vs. a collection
  registry?)
- Should the FTS5 schema use a `collection` column filter or separate virtual tables
  per collection? (Column filter is simpler; separate tables scale better)
- How does the token budget system allocate between memory and (future) knowledge
  injection when both are active? (Fixed split? Dynamic based on query relevance?)
- Should raw text be stored in Qdrant payload, a separate SQLite table, or both?
  (Qdrant payload is simpler but increases collection size; SQLite is cheaper for bulk
  re-embedding operations)

These don't block v3 but must be answered when implementing memory-mcp to avoid
groundwork that doesn't actually ground anything.

### Q7. Data Privacy and PII in the Memory Pipeline

Genesis stores user conversations in memory-mcp, extracts facts/entities, and
synthesizes user model profiles. The outreach system sends messages containing
user-relevant information across channels (WhatsApp, Telegram, web UI).

Questions to address:
- When storing conversational memory, should PII (names, addresses, financial
  details mentioned in conversation) be stored as-is, anonymized, or tagged for
  special handling?
- When the user model is synthesized during Light+ reflections, does it contain
  PII that could leak through outreach messages?
- Sub-agents spawned for tasks have `permitted_tools` restrictions, but do they
  inherit access to the full memory corpus? Should memory recall be scoped by
  sensitivity level?
- If Genesis is ever multi-user (V5+), memory isolation between users is
  critical. Should the schema support user-scoping from V3 even if only one
  user exists?

These are not blockers for V3 (single-user, self-hosted system), but the schema
decisions made in Phase 0 will determine how hard it is to add privacy controls
later. At minimum, memory records should have a `sensitivity` field (even if
unused in V3) to enable future PII-aware retrieval filtering.

---

## Document Hierarchy

This document sits alongside the other v3 architecture documents:

1. **genesis-v3-dual-engine-plan.md** — Framework decision, three engines, memory
   MCP wrapping, CLAUDE.md handshake, migration plan, container architecture, risk
   assessment
2. **genesis-v3-autonomous-behavior-design.md** — Cognitive layer: Awareness Loop,
   Reflection Engine, Self-Learning Loop, drives, autonomy, engagement
3. **genesis-v3-gap-assessment.md** (this document) — Pre-implementation gaps,
   risks, and open questions that must be addressed before building begins

For nanobot v1/v2 architecture, see `reconciled-architecture.md`.

---

## Related Documents

- [genesis-v3-build-phases.md](genesis-v3-build-phases.md) — Build plan this assessment was based on
- [genesis-v3-vision.md](genesis-v3-vision.md) — Vision this assessment evaluated readiness for
