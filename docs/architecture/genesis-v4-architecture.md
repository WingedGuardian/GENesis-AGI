# Genesis V4 Architecture

**Status:** Designed | **Last updated:** 2026-03-25

> V3 built the organs. V4 builds the nervous system.

---

## 1. V4 Vision: From Subsystems to Coherent Intelligence

V3 delivers a complete autonomous agent: perception (Awareness Loop), cognition
(Reflection Engine), learning (Self-Learning Loop), outreach, memory, health
monitoring, and earned autonomy through L4. Nine phases, all operational. The
infrastructure works.

But infrastructure is not intelligence. V3's subsystems act independently --
reflections trigger outreach, surplus computes findings, morning reports draft
content -- with no shared awareness of what the system is focused on or what
other modules are doing. The result feels like a collection of capable parts
rather than a singular mind. Redundant messages, conflicting decisions, missed
cross-cutting patterns.

V4 addresses this with two moves:

1. **A coordination layer** built on Global Workspace Theory that gives
   Genesis coherent autonomous behavior -- a mechanism for subsystems to
   compete for attention, broadcast decisions, and act as a unified whole.

2. **Six feature upgrades** that replace V3's static configurations with
   evidence-driven adaptive systems -- meta-prompting, drive weight adaptation,
   strategic reflection, expanded outreach, research-driven capabilities, and
   procedural confidence decay.

The philosophy is unchanged. V4 does not add new principles; it makes the
existing ones work together. A system with good perception, good cognition,
and good learning should be more than the sum of its parts. V4 builds the
integration layer that makes that true.

### What V4 is Not

V4 stays within clear boundaries:

- No L5-L7 autonomy (self-modification remains V5)
- No identity evolution (SOUL.md changes remain user-initiated)
- No meta-learning on the learning system itself (V5)
- No multi-agent coordination protocols (V5)

These boundaries are not limitations -- they are dependencies. Each requires
months of V4 operational data to build correctly. Building them prematurely
means building them on guesses instead of evidence.

---

## 2. Global Workspace Theory: The Unifying Cognitive Framework

### The Cognitive Science

Global Workspace Theory (Baars, 1988) offers a model of how coherent behavior
emerges from specialized, parallel processes. The core insight: the brain has
dozens of specialized modules running simultaneously -- vision, language,
motor planning, emotion -- but subjective experience is unified. You do not
simultaneously "see" and "hear" and "plan" as separate streams. Something
integrates them.

GWT proposes that integration happens through a **broadcast mechanism**.
Specialized modules process in parallel. They compete for access to a shared
workspace. Winners get broadcast to every module in the system. That broadcast
is the moment of coherence -- the instant where distributed processing becomes
coordinated behavior.

The key properties:

- **Competition, not consensus.** Modules do not negotiate. They compete.
  The workspace has limited capacity. Strong signals win; weak signals wait.
  This prevents the system from trying to attend to everything at once.

- **Broadcast, not polling.** When something wins the competition, it is
  actively pushed to every module. Modules do not have to check whether
  something important happened -- they are told.

- **Capacity limitation.** The workspace holds a small number of items (in
  human cognition, roughly 5-9). This is not a deficiency -- it is the
  mechanism that forces prioritization. A workspace that holds everything
  prioritizes nothing.

### The LIDA Cycle

The Learning Intelligent Distribution Agent (LIDA) architecture (Franklin et
al.) operationalizes GWT as an eight-step cognitive cycle:

```
SENSE -> PERCEIVE -> ATTEND -> BROADCAST -> PROPOSE -> SELECT -> ACT -> LEARN
```

Each step has a clear function:

| Step | Function | Cost Model |
|------|----------|------------|
| **SENSE** | Collect raw signals from the environment | Programmatic, free |
| **PERCEIVE** | Interpret signals -- identify patterns, flag anomalies | Cheap LLM |
| **ATTEND** | Run the salience competition -- which signals win the workspace? | Cheap LLM |
| **BROADCAST** | Push workspace contents to all modules | Programmatic, free |
| **PROPOSE** | Modules submit action proposals based on broadcast | Parallel cheap LLMs |
| **SELECT** | Evaluate proposals, resolve conflicts, make coherent decisions | Capable LLM |
| **ACT** | Execute approved actions | Variable |
| **LEARN** | Track outcomes, update weights, calibrate the cycle | Existing loop |

The cycle runs continuously. Light cycles (SENSE through BROADCAST) execute
on a short cadence -- every few minutes when idle. Full cycles (all eight
steps) run when proposals exist. No proposals means no action, no wasted
compute.

### Mapping LIDA to Genesis

The central insight of our V4 design: most LIDA steps already exist in V3
under different names. The gap is not in the parts but in the coordination.

```
SENSE     -> Awareness Loop collectors (exists)
PERCEIVE  -> Micro/Light reflection (exists, reframed)
ATTEND    -> Workspace Controller (new -- extends signal weights + urgency scorer)
BROADCAST -> Event bus + Intent State injection (new -- extends SessionStart hook)
PROPOSE   -> Modules write proposals (new -- replaces independent action)
SELECT    -> Workspace Controller decision session (new -- the "ego")
ACT       -> CC dispatch, outreach MCP (exists, now gated by SELECT)
LEARN     -> Self-Learning Loop (exists, extended with cycle metrics)
```

Five of eight steps leverage existing subsystems. Two are genuinely new
(ATTEND and SELECT, both functions of the workspace controller). One
(BROADCAST) is an extension of existing context injection. V4 is an
integration architecture, not a rebuild.

### Six Measurable Markers

We operationalize GWT through six markers, measured from day one:

1. **Global Availability** -- do CC sessions reference current workspace focus
   areas? Target: >50%.
2. **Functional Concurrency** -- how many subsystems are active simultaneously?
   Target: >2.
3. **Coordinated Selection** -- are workspace entries acted upon before they
   expire? Target: >40%.
4. **Capacity Limitation** -- workspace stays within 3-7 focus areas. Too many
   means loose competition; too few means over-aggressive filtering.
5. **Persistence with Controlled Update** -- workspace turnover rate of 20-40%
   per cycle. Below 20% is stale. Above 40% is thrashing.
6. **Goal-Modulated Arbitration** -- positive correlation (>0.2) between drive
   weights and workspace entry category distribution.

These are not aspirational targets -- they are the diagnostic criteria that
tell us whether the architecture is working. If we cannot measure it, we
cannot improve it.

### The Workspace State (Intent State)

> **V3 bridge (2026-03-26):** Session patches + activity tiers implemented as
> an incremental fix for cognitive state staleness. Consider activity-triggered
> LLM regeneration as part of the V4 workspace controller -- the workspace
> could regenerate its summary when session patch count exceeds a threshold,
> rather than waiting for the next full deep reflection cycle.

The workspace state -- what we call the intent state -- is the evolution of
V3's cognitive state summary. It is what Genesis is "conscious of" right now:

```
Intent State
+-- Focus Areas (3-7 active, ranked by salience)
|   +-- source signal, salience score, entry time, expiry
|   +-- rationale: why this entered the workspace
|
+-- Active Decisions (what SELECT decided and why)
|   +-- action taken, rationale, confidence
|   +-- expected outcome (for later measurement)
|
+-- Pending Proposals (awaiting next SELECT cycle)
|   +-- source module, action type, content, urgency
|
+-- Open Questions (persistent curiosity, carried forward)
|   +-- question, origin, relevance decay
|
+-- State Flags (system health, budget, active blockers)
```

Design decisions:

- **Capacity-limited.** Maximum 7 focus areas. New winners evict lowest-
  salience entries. This forces real competition.
- **Expiry-based eviction.** Focus areas expire if not renewed within N
  cycles. The workspace cannot become a stale todo list.
- **Decisions carry rationale.** When SELECT approves an action, reasoning is
  stored. The next cycle's SELECT reads "I decided X because Y" -- continuity
  of intent across cycles.
- **Dual-format.** Written as structured markdown for CC session injection.
  Backed by database tables for querying and measurement.

### Cycle Cadence

- **Light cycle** (SENSE through BROADCAST): runs on the existing 5-minute
  tick. Updates workspace state without requiring proposals or selection.
- **Full cycle** (all 8 steps): every 15-30 minutes when idle, triggered
  immediately on high-urgency signals.
- **Steps 5-7** only run when proposals exist. No proposals = no action = no
  wasted compute.

### Foreground Conversation

When the user opens a conversation session, that session becomes the core for
its duration. It receives the intent state (broadcast) and has full decision
authority. User actions bypass the proposal cycle -- user sovereignty is
absolute. But the conversation session reads the workspace, so it knows what
Genesis is focused on and what decisions have been made.

### Degraded Mode

If the workspace controller is unavailable (circuit breaker open, provider
down), the cycle degrades gracefully:

- Steps 1-4 continue: the system perceives and broadcasts.
- Steps 5-7 pause: no autonomous actions taken.
- Step 8 continues: outcomes of previously approved actions are still tracked.

The system enters "perception-only" mode. It watches but does not act. This
is the correct degradation -- a system that cannot make coherent decisions
should not make incoherent ones.

---

## 3. The Six V4 Features

Each feature is designed as a standalone capability that integrates into the
LIDA cycle at a specific step. They can be enabled independently via feature
flags, and each has a shadow mode that runs alongside V3's existing behavior
before taking over.

### 3.1 Meta-Prompting Protocol

**LIDA integration:** PERCEIVE step (V4 static, V5 meta-prompted) and SELECT
step (V5).

V3 uses static monolithic prompts for reflection. A single large prompt
carries the full signal landscape, all instructions, and all context. This
works, but it has two weaknesses: the LLM loses focus in large contexts
(positional bias), and every reflection asks the same structural questions
regardless of what the signals actually contain.

Meta-prompting replaces this with a 3-step protocol:

**Step 1 -- Meta-Prompt (cheap model).** Given the full signal landscape:
"What are the 3-5 most important questions this reflection should answer?
Consider cross-cutting patterns, not just individual items." Cost: ~$0.01.

**Step 2 -- Deep Reflection (capable model).** Each question answered
independently with only its relevant context. Enables parallel execution,
focused context per question, and natural cost scaling.

**Step 3 -- Synthesis (capable model, fresh call).** Sees only the answers
from Step 2, not the reasoning. "Do any of these answers interact? Are there
patterns across them that the individual answers missed?" A fresh context
prevents anchoring on Step 2's framing.

The critical principle: the meta-prompter should err toward breadth. One
unnecessary question is cheap (easily answered and discarded). One missed
question that mattered is expensive (entire reflection misses an insight).

Total cost is often less than a single monolithic prompt because each step's
context is smaller. Meta-prompting is cost-neutral or cheaper while producing
better output.

V4 also introduces DSPy optimization: treating prompts as programs with
trainable parameters, using V3's operational data (which reflections produced
actionable outputs, which were noise) to algorithmically optimize prompt
structure, few-shot examples, and instruction phrasing.

### 3.2 Signal and Drive Weight Adaptation

**LIDA integration:** ATTEND step (salience competition) and LEARN step
(calibration).

V3 uses fixed weights for both signals and drives. A signal that was important
at deployment retains its weight forever, regardless of whether it actually
predicts valuable reflections. V4 replaces fixed weights with two evidence-
driven tuning loops:

**Loop 10 -- Signal Weight Adaptation (days timescale).** After each Deep or
Strategic reflection, classify output quality: did the reflection produce
observations that were subsequently used? For each contributing signal: if the
reflection was actionable, nudge the signal's weight up. If noise, nudge down.
Learning rate starts at 0.02 (conservative). Weights clamped to bounds.

**Loop 9 -- Drive Weight Adaptation (weeks timescale).** Every two weeks,
compute outcome quality per drive: what fraction of drive-aligned actions
produced positive outcomes? Adjust weight accordingly. Adjustment rate 0.03
per cycle (slower than signal weights -- drives are more fundamental).

The Self-Learning Loop is the sole writer to both loops. Strategic reflection
can propose overrides but does not directly modify weights. This separation
prevents reflection from gaming its own triggering conditions.

All autonomous adjustments stay within +/-20% of initial values. Adjustments
beyond that boundary require user approval. This is not cost control -- it is
intelligence discipline. A system that aggressively rewrites its own
sensitivity parameters risks oscillation.

### 3.3 Strategic Reflection (MANAGER/DIRECTOR)

**LIDA integration:** Merges into the workspace controller. MANAGER runs as a
periodic ATTEND enhancement (weekly). DIRECTOR runs as a periodic SELECT
enhancement (monthly).

V3's weekly self-assessment asks "Am I improving at learning?" Strategic
reflection asks two different questions:

**MANAGER (weekly):** "What should change about how the system operates?"
Reviews architecture effectiveness, cross-references system metrics (procedure
success rates, cost data, signal distributions, memory quality), and produces
proposed parameter adjustments, architecture observations, capability gap
identification, and outreach preference adjustments.

**DIRECTOR (monthly -- every 4th Strategic tick):** "Am I heading where I
should be heading?" Audits goal drift (which stated goals got zero attention?),
reviews 30-day time allocation (what did Genesis actually spend compute on?),
proposes budget adjustments, capability acquisitions, and strategic priority
reordering.

Both roles operate in shadow mode during their first four weeks -- proposals
are logged but not applied. The user reviews proposals via morning report or
dashboard. Anti-sycophancy is critical at this depth: strategic reflection
must challenge its own prior recommendations. The model used is frontier-class
(no fallback), because strategic depth requires frontier judgment.

### 3.4 Expanded Outreach

**LIDA integration:** Outreach categories become proposal types that modules
submit to the LIDA cycle. The workspace controller (SELECT step) replaces
independent outreach decisions with coordinated selection.

V3 outreach is limited to two categories (Blocker and Alert) plus exactly one
surplus-driven outreach per day. V4 expands to five additional categories:

| Category | Source | Example |
|----------|--------|---------|
| **Finding** | Reconnaissance + salience threshold | "New framework relevant to your project" |
| **Insight** | Reflection pattern detection | "You have built 3 similar pipelines -- template?" |
| **Opportunity** | User model + new info + capability cross-ref | "Based on your skills + goals, high-leverage idea" |
| **Digest** | Scheduled batch of low-priority items | "Here is what happened while you were away" |
| **Surplus** | Daily brainstorm staging area | Labeled as surplus-generated |

Each requires a calibrated user model and engagement data to deliver well.
The growth ramp is explicit and evidence-gated:

- **Bootstrap:** Exactly 1/day (V3 default)
- **Calibrating:** 1-2/day (20+ data points, engagement >40%)
- **Calibrated:** 1-3/day (50+ data points, engagement >50%, user approval)
- **Autonomous:** Self-determined, bounded by daily cap (100+ data points,
  consistent engagement, strategic reflection confirms)

Regression is automatic: if engagement drops below 25% over two weeks,
frequency drops one phase. The system announces the regression and its reason.

Before sending outreach, the system predicts engagement probability. After
outcome data arrives, it computes prediction error. Over time, it tracks
prediction accuracy -- and that accuracy determines autonomy. This is
intelligence applied to its own behavior, not a heuristic.

### 3.5 Research-Driven Capabilities

**LIDA integration:** New capabilities run through the LIDA proposal cycle.
Tools and integrations are proposed by modules and approved by the workspace
controller.

V4 introduces infrastructure that enables Genesis to expand its own
capabilities:

**Hot-Reload Tool Discovery.** Directory-based tool discovery -- drop a Python
file in a tools directory, it auto-registers as an MCP tool. Uses a decorator
with docstring-based descriptions and type-hint-derived parameter schemas.
Validation: tools must pass a dry-run before registration.

**AI Functions.** Genesis can define new capabilities at runtime using natural
language specifications plus validation conditions, with automatic code
generation and validation. Procedures that can produce and execute code, not
just guide the LLM. Safety boundary: sandboxed execution only.

**API-to-MCP Gateway.** Automated conversion of REST API specs (OpenAPI)
into MCP-compatible tools. Point at an API spec, get MCP tools. Reduces the
marginal cost of each new API integration.

**Tool Search API.** Deferred tool loading for API-routed sessions. Only 3-5
most frequently used tools loaded immediately; the rest discoverable via
search. Reduces tool definition context by approximately 85% as tool count
grows past 30-50.

**Context-Efficient Sessions.** Sandbox execution for tool outputs exceeding
a size threshold -- large outputs auto-indexed into full-text search, only
refined results enter context. Extends effective session lifetime for longer
autonomous tasks.

**Agentic Retrieval.** Wraps memory retrieval in a reason-retrieve-evaluate
loop for background tasks where latency is tolerable. Single-pass stays for
interactive paths.

### 3.6 Procedural Confidence Decay

**LIDA integration:** Feeds the LEARN step through retrieval quality
calibration.

V3 uses Laplace smoothing for procedure confidence -- a statistical estimator
based solely on success and failure counts, with no time dimension. A
procedure that worked perfectly six months ago but has not been used since
retains its high confidence forever. This creates a knowledge inventory that
never expires, where stale procedures compete unfairly with fresh ones.

V4 adds exponential time decay:

```
decayed_confidence = base_confidence * (decay_rate ^ weeks_since_use)
```

With a decay rate of 0.95/week and a floor of 0.1:

| Weeks Since Use | Confidence (starting 0.80) |
|----------------|---------------------------|
| 0 | 0.800 -- Fresh |
| 4 | 0.654 -- Active |
| 12 | 0.436 -- Low |
| 26 | 0.210 -- Near floor |
| 52 | 0.100 -- At floor |

Each successful use resets the decay clock. Failed use also resets the clock
but drops base confidence via Laplace smoothing. The net effect: actively-used
procedures maintain confidence; unused procedures fade; procedures used but
failing drop fastest.

Decay is computed at query time -- a pure function of base confidence, last
use timestamp, and current time. No batch job needed. Always current.

Critically, decay is maturity-gated. During the EARLY maturity stage (fewer
than 50 procedures), decay is disabled or very slow. The system needs to
accumulate knowledge before it can afford to forget. The floor of 0.1 ensures
procedures are never invisible -- they can be revived by successful use at
any time.

---

## 4. Activation Criteria

V4 does not activate on a calendar date. It activates when evidence thresholds
are met. This is a consequence of the design philosophy: build on data, not on
schedules.

### Global Prerequisites

| Prerequisite | Threshold | Rationale |
|---|---|---|
| V3 operational data | 4+ weeks full-stack | Need baseline behavioral data |
| Reflection quality labels | 50+ Deep, 20+ Strategic with outcomes | Required for meta-prompting and weight adaptation |
| Engagement data | 50+ outreach events with outcomes | Required for expanded outreach calibration |
| Self-assessment data | 4+ weekly assessments | Required for strategic reflection input |
| Shadow mode | Required for every feature | Dual-run before cutover |

### Per-Feature Gates

Each feature has its own activation gate independent of the others:

- **Meta-Prompting:** 50+ Deep reflections with quality labels, shadow mode
  comparison of meta-prompted vs. monolithic output quality.
- **Weight Adaptation:** 4+ weeks V3 data, sufficient actionable/noise
  classifications, shadow mode logging proposed adjustments without applying.
- **Strategic Reflection:** 4+ weekly self-assessments, meaningful procedure
  success/failure counts, shadow mode logging proposals without acting.
- **Expanded Outreach:** V3 basic outreach operational, 20+ surplus data
  points, engagement rate >40%, calibrated user model.
- **Research-Driven Capabilities:** Tool count approaching the accuracy cliff
  (~30-50 tools) where deferred loading becomes necessary.
- **Procedural Decay:** 50+ procedures with varying ages (spanning 2+ months),
  usage data populated, shadow mode tracking what would change.

### Shadow Mode Protocol

Every V4 feature runs in shadow mode before taking over:

1. The new system runs alongside V3's existing behavior.
2. Both produce outputs; only V3's outputs are acted upon.
3. Quality comparison: is the V4 output equal or better?
4. User reviews comparison data and approves the cutover.

This doubles cost temporarily but prevents quality regression. The cost is
bounded (shadow mode duration is typically 4 weeks per feature) and the
insurance value is high.

---

## 5. V3 Groundwork Already in Place

V4 is not starting from scratch. V3 was designed with V4 in mind, and
significant groundwork is already operational:

### Schema Forward-Compatibility

- `signal_weights` table has `last_adapted_at` and `adaptation_notes` columns
  (NULL in V3, ready for V4 adaptation tracking)
- `drive_weights` table stores weights with bounds (0.10-0.50), independent
  and non-normalized
- `depth_thresholds` table with floor/ceiling constraints
- `autonomy_state` table supports levels 1-7 (V3 caps at L4 architecturally,
  not by schema constraint)
- `outreach_history` table has CHECK constraint for all 7 categories including
  V4 types (finding, insight, opportunity, digest)
- `person_id` columns tagged `GROUNDWORK(multi-person)` across 8 tables

### Operational Subsystems

- **Signal weights CRUD** with clamped updates (`MAX(min_weight, MIN(max_weight, ?))`)
- **UrgencyScorer** with time multiplier curves, ready for signal-contribution
  tracking
- **PromptBuilder** with round-robin and focus-based template selection, ready
  for meta-prompting integration
- **Engagement heuristics** with per-channel thresholds, ready for learned
  thresholds
- **Outreach MCP** with 5 tool stubs ready for expanded categories
- **Procedural memory** with Laplace smoothing, quarantine mechanism, maturity
  model, and `last_used` tracking
- **Self-Learning Loop** with observation usage tracking (the foundation for
  reflection quality labeling)

### Tagged Groundwork in Code

V3 code contains `GROUNDWORK` tags marking infrastructure built specifically
for future features:

- `GROUNDWORK(outreach-pipeline)` -- engagement tracking interface on channel
  base class
- `GROUNDWORK(outreach-alerts)` -- outreach alert wiring point
- `GROUNDWORK(v4-surplus-tasks)` -- surplus task type expansion point
- `GROUNDWORK(v4-parallel-dispatch)` -- concurrent task dispatch capability
- `GROUNDWORK(v4-rate-tracking)` -- per-provider rate limit tracking
- `GROUNDWORK(skill-autonomy-graduation)` -- per-skill autonomy category
- `GROUNDWORK(cross-vendor-review)` -- cross-vendor review for auto-approval
- `GROUNDWORK(multi-person)` -- multi-person support across schema
- `GROUNDWORK(unified-bridge)` -- channel adapter framework
- `GROUNDWORK(pre-execution-gate)` -- decision gate before task execution
- `GROUNDWORK(user-model-synthesis)` -- user model cache update point
- `GROUNDWORK(provider-migration)` -- provider registry migration path

These tags are protected by project rules: they cannot be deleted or
refactored as "dead code." They are removed only when the feature they
support is fully active.

### Existing Event Infrastructure

- `GenesisEventBus` with severity-based dispatch (V4 extends with type-based
  subscription)
- `SessionStart` hook with cognitive state injection (V4 evolves to intent
  state injection)
- `GenesisRuntime` bootstrap with capability registration
- Composite state machine (cloud, memory, embedding, CC) for resilience

---

## 6. How Features Map to the LIDA Cycle

| Feature | Primary LIDA Step | How It Fits |
|---------|-------------------|-------------|
| Meta-Prompting | PERCEIVE | 3-step protocol replaces monolithic prompts for reflection |
| Signal/Drive Adaptation | ATTEND + LEARN | Weights feed salience competition; LEARN calibrates |
| Strategic Reflection | ATTEND + SELECT | MANAGER enhances ATTEND weekly; DIRECTOR enhances SELECT monthly |
| Expanded Outreach | PROPOSE | New outreach categories become proposal types |
| Research-Driven | PROPOSE + ACT | New capabilities run through proposal cycle |
| Procedural Decay | LEARN | Decay calibrates retrieval quality over time |

This mapping is not forced. Each feature was originally designed independently
(as standalone V4 specs), then repositioned within the LIDA framework when the
GWT architecture was designed. The fact that they map cleanly to specific LIDA
steps validates the framework -- it is not imposing structure on features that
resist it.

### What Gets Retired

- Independent outreach decisions by reflection sessions (replaced by proposals)
- Reflections "triggering outreach" directly (replaced by proposals to SELECT)
- Direct surplus-to-delivery pipeline (replaced by proposal cycle)

### What Gets Reframed

- Awareness Loop becomes SENSE + trigger PERCEIVE
- Micro/Light reflections become PERCEIVE
- Deep reflection becomes a PROPOSE source
- Strategic reflection merges into workspace controller at strategic cadence
- Self-Learning Loop becomes LEARN with cycle-level metrics

---

## 7. Resilience and Failure Modes

### Workspace Controller Failure

- **ATTEND failure:** Skip to next tick. Previous workspace state remains
  valid -- focus areas have expiry-based eviction, so a missed ATTEND cycle
  means slightly stale but not corrupted.
- **SELECT failure:** Proposals remain in queue for next cycle. Previously
  approved actions remain valid for one additional cycle (prevents action
  stall).
- **SELECT timeout (60s):** Cycle degrades to broadcast-only -- steps 1-4
  run, steps 5-7 defer.

### Proposal Queue Management

- Maximum 50 proposals. Overflow evicts lowest-urgency first.
- Proposals expire after 3 cycles (45-90 minutes at normal cadence). Expired
  proposals logged for LEARN but not evaluated.
- Consistently expiring proposals flag a cadence mismatch.

### Integration with Existing Resilience

The LIDA cycle integrates with V3's resilience layer: composite state machine,
deferred work queue, circuit breakers, dead-letter recovery. CC sessions fail.
The cycle degrades gracefully. The degradation hierarchy is clear: perception
continues, action pauses, learning persists.

---

## 8. Outstanding Design Work

### 8.1 Fire Alarm Taxonomy: What Wakes the Pilot

**Status:** Implemented | **Added:** 2026-04-08 | **Implemented:** 2026-04-09

The 2026-04-08 memory exhaustion incident had multiple fire alarms sounding
simultaneously — watchdog reclaim failures, Guardian stuck in confirmed_dead,
Guardian dialogue returning "need_help", CC unavailable for diagnosis — and
nobody inside Genesis responded. This is now addressed by the **Sentinel** —
Genesis's container-side guardian (see `src/genesis/sentinel/`).

**Principle:** These are META-failures — not "a component is down" but "the
system designed to detect and fix failures is itself broken." Individual
component failures are handled by reflexes. When the reflexes fail, the
pilot needs to know.

**Tier 1: Defense mechanism failures** (the guards are down)
- Watchdog reclaim fails — the OOM prevention mechanism is broken
- Guardian stuck in confirmed_dead or surveying for >10min — external
  monitor is blind
- Guardian dialogue returns "need_help" for a condition reflexes COULD
  handle — missed self-heal opportunity
- CC unavailable for Guardian diagnosis — can't even think about the
  problem (happened at 21:54 UTC during incident)
- Auth/config blocking health probes — the monitoring system itself is
  misconfigured

**Tier 2: Cascading / compounding failures**
- Multiple infrastructure components degraded simultaneously
- Error spike coinciding with provider failures
- Reflexes firing but conditions not improving (reclaim runs, memory
  keeps climbing)

**Tier 3: Persistent unresolved conditions**
- Same reflex fires N times without resolving the underlying condition
- An observation stays unresolved past a severity-appropriate threshold
- A condition recurs after restart (the restart didn't fix root cause)

**Response architecture: Two complementary roles**

Two distinct actors, not competing but complementary:

**First Responder (separate CC call):** Immediate, reactive, tactical. "Qdrant
is down, fix it now." Spins up in seconds. No continuity, no strategic context,
but fast. This is what the Guardian does from the host side — Genesis needs the
same capability from INSIDE the container for conditions the Guardian can't see.
The auth misconfiguration in the 2026-04-08 incident was invisible from outside
because the Guardian's own probes were the thing being blocked. Only an internal
first responder could have caught and fixed that.

**Ego (incident commander):** Slower, strategic, contextual. "Qdrant has gone
down 5 times this week — why? Is the underlying disk failing? Should we
migrate?" Runs on its normal cadence. Sees the pattern across time. Makes
decisions about root cause and systemic fixes.

The first responder handles the immediate crisis. The ego reviews the incident
after and decides what to change systemically. The first responder CREATES the
observation that the ego later processes. They form a response chain:

```
Fire alarm → First Responder CC → immediate fix + observation
                                          ↓
              Ego (next cycle) ← observation: "Qdrant restarted 5x this week"
                                          ↓
              Ego proposes: "investigate disk health, consider migration"
```

**Trigger sources for the first responder CC call:**

The first responder is a CC session dispatched by Genesis itself. It can be
triggered by any of:

1. **Fire alarm signals** — awareness loop detects a Tier 1/2 condition that
   reflexes can't handle. Spawns the CC call directly.
2. **Guardian comms** — the Guardian is already programmed to contact Genesis
   and tell it to fix its problems before escalating. When the guardian-dialogue
   handler receives a concern it can't reflexively fix, it dispatches a first
   responder instead of immediately returning "need_help."
3. **Reflection engine** — a reflection detects a pattern of failures that
   warrants immediate investigation. Escalates to first responder.
4. **Ego subagent** — the ego, during its cycle, identifies something that
   needs immediate tactical attention. Dispatches a first responder as a
   subagent rather than waiting for the next ego cycle.

**Open questions:**
- Should the ego cadence be overridden for Tier 1 events? ("Wake up NOW,
  not in 60 minutes.") Or is the first responder CC call sufficient for
  the immediate response, with the ego reviewing at its normal pace?
- How many simultaneous Tier 1 signals before "drop everything"?
- Does the first-responder CC call need continuity across invocations,
  or is each call fresh context?
- Where does this live architecturally? Awareness loop spawn? Remediation
  registry escalation? New subsystem?
- Model selection: first responder needs speed over depth. Sonnet? Or does
  infrastructure diagnosis need Opus?

---

### 8.2 Ego Context Redesign: Pilot, Not Dashboard

**Status:** Partially implemented | **Added:** 2026-04-08 | **Updated:** 2026-04-09

The Sentinel implements the three-layer separation (EYES/REFLEXES/BRAIN) and
the breathing metaphor for reflection integration. Sentinel events flow into
the observation pipeline as low-priority items (resolved) or critical items
(escalated). The ego context strip is deferred to ego wiring.

The current `EgoContextBuilder` injects a full health snapshot (provider
status, latency, resilience state, queue depths, session counts) into
every ego cycle. This treats the ego as a reactive monitoring dashboard.
The ego should be a **pilot** — an independent actor that decides where
its own attention goes.

**Core principle:** Everything we inject into the ego's context influences
where its attention goes. A blanket health dump crowds out curiosity and
strategic thinking. The ego should spend ~80% of its cycles on self-directed
exploration and ~20% on mandatory items that only it can handle.

**Architecture:**

Three-layer separation:

1. **Infrastructure Monitor (call site 37, free models) — EYES.** Observe,
   detect anomalies, classify severity. Never act. Creates observations.

2. **Mechanical Remediations (no LLM) — REFLEXES.** If/then rules for
   known-safe conditions: memory reclaim, service restarts. These run in
   the watchdog/awareness loop. When reflexes handle a condition, the
   observation is resolved — the ego never sees it.

3. **Ego — BRAIN.** Only receives:
   - **Mandatory escalations**: CRITICAL observations that lower layers
     explicitly escalated ("I tried, I failed, this needs judgment"). The
     escalation itself is the signal.
   - **Anomalies from reflections**: The reflection engine digests raw data
     and surfaces surprising patterns, contradictions, or novel observations
     as open questions — curiosity fuel, not conclusions.
   - **Thread updates**: The ego's own follow-ups from previous cycles,
     enriched by anything the reflection engine learned since.

**The reflection engine is the ego's staff.** Reflections do the heavy lifting
of reading, processing, synthesizing. The ego gets distilled insights ready
for decision-making. Raw health data, routine observations, and resolved
incidents never reach the ego.

**Design decisions still open:**

- **Urgency bypass:** Infrastructure conditions that jeopardize Genesis's
  operations (the kind of cascading failure from 2026-04-08) should fast-path
  directly to the ego's mandatory items, potentially skipping the reflection
  digest. The trigger: reflexes failed AND the condition is system-threatening.

- **Anomaly detection quality:** Reflections currently run on cheaper models
  (Haiku for light, Sonnet for deep). Are these capable enough to produce
  genuinely surprising anomalies? Bad anomaly detection is worse than none.
  Model advancement may help — Genesis should track models.md and swap in
  better anomaly detectors as they become available at similar cost.

- **Structured follow-ups:** The ego's `follow_ups` field is freeform text.
  Should this be a typed list of "open threads" with metadata (when started,
  what's been tried, why it matters)? This would let the context builder
  match threads to new information automatically rather than hoping the LLM
  makes the connection.

- **Curiosity architecture:** LLMs given an empty canvas satisfice. They
  need hooks to pull on. The reflection engine should surface anomalies and
  open questions as curiosity fuel. The ego's compacted history provides
  momentum (continuing threads across cycles). Tension and contradiction
  spark better thinking than status reports.

- **What to strip from EgoContextBuilder:**
  - Health snapshot (provider status, latency, resilience state)
  - Routine observations (handled at lower levels)
  - Queue depths, session counts, operational metrics
  - Anything reflexes or the infrastructure monitor can handle

- **What to keep/add:**
  - Compacted history (continuity across cycles)
  - Follow-ups from previous cycles (the ego's own agenda)
  - Mandatory escalations (filtered, critical only)
  - Anomalies from reflections (curiosity fuel)
  - User context (who it's serving)
  - Session heartbeats (awareness of individual active sessions)

- **Session heartbeat integration:** The `session_heartbeats` table provides
  Layer 1 (mechanical, per-prompt) cross-session awareness. Each CC session
  writes a heartbeat on every `UserPromptSubmit` with user summary, genesis
  summary (from tool_observations.jsonl), and timestamp. The proactive memory
  hook reads concurrent heartbeats and injects `[Concurrent]` tags. This works
  independently of the ego — when the ego is wired, it can query heartbeats
  for Layer 3 (periodic synthesis) awareness of what all sessions are doing.
  Essential knowledge also surfaces active session count at generation time.

- **Cross-session awareness layers:**
  - **Layer 1 (mechanical, per-prompt):** Session heartbeats — write/read in
    UserPromptSubmit hook, ~15ms overhead, always completes
  - **Layer 2 (session state):** Essential knowledge — ego focus + active
    session count, regenerated at SessionEnd
  - **Layer 3 (ego synthesis, periodic):** Future — ego reads heartbeats and
    synthesizes cross-session awareness during ego cycles
  - **Layer 4 (persistent knowledge):** Memory store — durable cross-session
    learnings stored via memory_store

- **The 80/20 split:** Enforce architecturally (context budget — cap mandatory
  items to 20% of context tokens) or via prompt (tell the ego to prioritize
  curiosity)? Probably both.

**Implementation sequence:** Strip the health dump first (simplest, immediate
improvement). Add escalation filtering. Then build the anomaly pipeline from
reflections. Structured follow-ups last.

---

## 9. References

### Individual Feature Specifications

For implementation-level detail on each V4 feature, see:

- `docs/plans/v4-meta-prompting-spec.md` -- 3-step protocol, DSPy optimization,
  progressive disclosure
- `docs/plans/v4-signal-drive-weight-adaptation-spec.md` -- Loop 9/10
  algorithms, bounded self-adjustment, shadow mode
- `docs/plans/v4-strategic-reflection-spec.md` -- MANAGER/DIRECTOR roles,
  proposal lifecycle, activation criteria
- `docs/plans/v4-expanded-outreach-spec.md` -- growth ramp, self-rating,
  channel learning, governance
- `docs/plans/v4-research-driven-features-spec.md` -- 14 capability items with
  dependency chains
- `docs/plans/v4-procedural-confidence-decay-spec.md` -- decay algorithm,
  maturity gating, amnesia prevention

### Cognitive Architecture

- GWT Cognitive Architecture Design (internal spec, synthesized into this document) —
  full GWT/LIDA design with measurability framework, migration plan, and
  infrastructure requirements

### Foundation Documents

- `docs/architecture/genesis-v3-vision.md` -- core philosophy, four drives,
  identity model
- `docs/architecture/genesis-v3-autonomous-behavior-design.md` -- primary
  design, loop taxonomy, philosophical foundations
- `docs/architecture/genesis-v3-build-phases.md` -- safety-ordered build plan,
  V4 activation model

### Research

- Baars, B.J. (1988). *A Cognitive Theory of Consciousness.* Cambridge
  University Press.
- Franklin, S. et al. *LIDA: A Systems-level Architecture for Cognition,
  Emotion, and Learning.*
- Ye et al. (2025). CogniPair: first computational GWT implementation for LLM
  agents.
- *Evaluating GWT Markers in LLM Systems* (2026). Preprints.org.
- Lambert, N. "Lossy Self-Improvement." Interconnects AI (2026).
- Karpathy, A. *autoresearch* (2026). Triangular experiment loop.
