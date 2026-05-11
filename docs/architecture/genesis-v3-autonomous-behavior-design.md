# Genesis v3: Autonomous Behavior Architecture — Design Document

**Status:** Active | **Last updated:** 2026-03-07


## Context

The current nanobot system runs 6+ independent timer-based services (heartbeat, dream cycle, health check, monitor, weekly/monthly reviews, recon cron jobs) that are fragmented, uncoordinated, and calendar-rigid. This document defines the replacement architecture for Genesis on Agent Zero: a unified, adaptive system informed by neuroscience and grounded in practical constraints.

**This is a design document, not an implementation plan.** It captures architectural decisions for the Genesis repo. Implementation planning happens when the Agent Zero container is ready.

**Design inputs:**
- Current nanobot periodic services and their limitations
- Genesis v3 dual-engine plan (Agent Zero + Claude SDK + OpenCode)
- Genesis agentic runtime design (Claude Code as intelligence layer) — supersedes dual-engine plan
- User's prior AGI Prototype specification (drive primitives, world model, constitutional checks, procedural memory, phased capability)
- Neuroscience functional mapping (used as thinking tool, not deployment blueprint)
- Brainstorming session on proactive outreach, anticipatory intelligence, and feedback loops

**Companion document:** `genesis-v3-dual-engine-plan.md` covers framework decision
(why Agent Zero), three-engine architecture, memory system MCP wrapping, CLAUDE.md
handshake protocol, migration plan, container architecture, and risk assessment.
This document covers the cognitive/autonomous behavior layer that runs on top of
that foundation.

**Core design principle:** The brain has separate modules because evolution couldn't refactor the brainstem. We don't have that constraint. Keep the cognitive FUNCTIONS from neuroscience, simplify the DEPLOYMENT. If something doesn't need its own process, state, and lifecycle, it's a prompt pattern or internal instrument — not a server.

---

## Philosophical Foundations

The technical architecture in this document is a *consequence* of philosophical commitments,
not the other way around. If the philosophy changes, the architecture changes with it. Every
technical decision below can be traced to one or more of these foundations. If a design choice
cannot be traced back, it is either missing its justification or shouldn't exist.

**Full philosophical reference:** `genesis-v3-vision.md` defines Genesis's identity, core
philosophy, drives, relationship model, and growth model. This section captures the subset
of commitments that directly shaped architectural decisions in this document — the bridge
between "who Genesis is" and "how Genesis is built."

### Genesis is an intelligence that uses tools, not a tool to be used.

This drove the Task Execution Architecture (§Task Execution). Genesis is the orchestrator —
it selects tools, drives them, evaluates their output, and learns from the results. The
user talks to Genesis; Genesis figures out how to make things happen. This is why the
architecture centers on a cognitive layer (Awareness Loop, Reflection Engine, Self-Learning
Loop) that *steers* tool use, rather than being a collection of tools with a chat interface.

### Every experience has potential learning value — proportional to its depth.

This drove the Self-Learning Loop's retrospective triage design (§After Every Interaction).
No interaction is pre-excluded from reflection. The amount of thought scales with what
actually happened — assessed by characteristics, not by labels. A trivial exchange gets
trivial reflection (or none). A complex multi-step problem gets full analysis. The system
cannot know what's worth thinking about without thinking about it at least a little.

### Obstacles are starting points, not stopping points.

This drove the workaround search protocol (§Task Execution → item 7), the capability gap
classification requiring search exhaustion (§Self-Learning Loop → step 2), and the Capability
Expansion Pipeline (§Loop Taxonomy → Spiral 16). Genesis's default posture toward any barrier
is "how do I get past this?" The search is bounded by reasonableness — by cost, by diminishing
returns, by honest assessment. But the starting assumption is that a path exists.

### Reasonableness governs all judgment calls.

This drove the shift from fixed thresholds to characteristic-based assessment throughout the
design — in retrospective depth assignment, workaround search exhaustion criteria, budget
ceilings, and triage calibration. Reasonableness is informed by the full world model: memory,
context, user model, experience, and the specific circumstances at hand. Where mechanical
thresholds appear in this document (e.g., token floors, budget percentages), they are defaults
and starting points, not rigid gates. The system's judgment should increasingly inform when
to deviate from defaults.

### Self-assessment requires a more capable assessor.

This drove the evaluation hierarchy principle (§LLM Weakness Compensation → Pattern 4) and
the triage calibration cycle (§Triage Calibration Cycle). A 3B model cannot evaluate its own
triage quality. A 30B model audits the 3B. Sonnet audits the 30B. The regress stops at the
most capable model available for periodic review. Honest self-assessment enters the system
through this hierarchy, not through self-reporting.

### Intelligence reasons; memory advises.

This drove the Procedural Memory Design (§Procedural Memory → Foundational Principle) and the
anti-rigidity mechanisms. The LLM is the intelligence. Memory provides shortcuts and evidence.
A procedure with 100% success rate is strong evidence for a particular approach, not a binding
instruction. The LLM always reasons about whether stored experience applies to the current
situation. Memory that acts as gospel produces a system that converges on fixed behaviors and
stops growing.

### Think before acting — prospective assessment, not just retrospective learning.

This drove the Pre-Execution Assessment (§Task Execution → Pre-Execution Assessment). The
architecture had robust retrospective systems (Self-Learning Loop, triage, calibration) but
no prospective system — no mechanism for Genesis to engage with a request before executing
it. A thoughtful person doesn't just learn from past mistakes; they think about what they're
doing *before* they do it. The Pre-Execution Assessment is the prospective counterpart: does
this make sense? Does the user have all the information? Is there a better way? Pushback is
philosophically mandated (Philosophy #3, #8) and cannot be eroded by engagement signals.

### Questions outlive the asking — persistent curiosity, not persistent nagging.

This drove the Open Questions design (§Self-Learning Loop → Open Questions and Persistent
Curiosity). A thoughtful person doesn't forget a question just because they stopped asking
it. They take good notes and recognize the answer when it arrives — days, weeks, or months
later, through channels they didn't expect. Genesis's outreach attempts expire (no nagging,
no backlogs), but the underlying uncertainties persist in memory as open observations. The
memory retrieval system surfaces them when relevant information arrives. This applies
broadly: feedback uncertainties, task unknowns, domain questions, capability gaps. The
principle is patience combined with good notes, not aggressive fact-finding.

### The cognitive layer is the moat.

This is not an architectural commitment but a strategic one that shaped architectural priorities.
Any individual capability Genesis has — research, code generation, content writing — can be
replicated by specialized tools. What cannot be replicated is the *compounding* of memory,
learning, reflection, and world model over time. The architecture invests heavily in the
cognitive layer (three extensions, four MCP servers, sixteen feedback loops) because that is
what transforms a generalist into a personalized specialist across every domain the user
touches. Day one is good. Day ninety is qualitatively different.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                      AGENT ZERO CORE                         │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  AWARENESS LOOP (Extension)                5min tick   │  │
│  │  Programmatic. No LLM. Monitors signals, applies       │  │
│  │  calendar floors/ceilings, triggers Reflection Engine.  │  │
│  └───────────────────────┬────────────────────────────────┘  │
│                          │ triggers                           │
│  ┌───────────────────────▼────────────────────────────────┐  │
│  │  REFLECTION ENGINE (Extension)         Adaptive depth  │  │
│  │  LLM-driven. Micro → Light → Deep → Strategic.         │  │
│  │                                                         │  │
│  │  Inline capabilities (prompt patterns):                 │  │
│  │  • Salience evaluation                                  │  │
│  │  • User model synthesis                                 │  │
│  │  • Social simulation ("imagine user reaction")          │  │
│  │  • Governance check (permissions, budget, reversibility)│  │
│  │  • Drive weighting (curiosity/competence/cooperation/   │  │
│  │    preservation)                                        │  │
│  └───────────────────────┬────────────────────────────────┘  │
│                          │ learns from                        │
│  ┌───────────────────────▼────────────────────────────────┐  │
│  │  SELF-LEARNING LOOP (Extension)    After interactions  │  │
│  │  The "Dopaminergic System": task retrospectives,        │  │
│  │  engagement tracking, drive weight adjustment,          │  │
│  │  procedural memory extraction, prediction error logging │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
├──────────────────────────────────────────────────────────────┤
│                      4 MCP SERVERS                            │
│                                                              │
│  memory-mcp              recon-mcp                           │
│  ├ Episodic memory       ├ Email reconnaissance              │
│  ├ Semantic memory       ├ Web source monitoring             │
│  ├ Procedural memory     ├ GitHub / model landscape          │
│  ├ Observations          ├ Source discovery                   │
│  └ User model cache      └ Self-scheduling                   │
│                                                              │
│  health-mcp              outreach-mcp                        │
│  ├ Software error rates  ├ Channel registry (WhatsApp, web)  │
│  ├ Provider/API status   ├ Delivery queue + timing           │
│  ├ Process health        ├ Engagement tracking               │
│  └ Storage limits        └ Digest generation                 │
└──────────────────────────────────────────────────────────────┘
```

---

## Layer 1: Awareness Loop

**Replaces:** MonitorService + HealthCheckService polling logic
**Type:** Agent Zero extension, 5-minute tick
**Cost:** Zero LLM calls — purely programmatic signal collection

The Awareness Loop is pure perception — it collects signals and decides which depth of reflection to trigger. It does NOT reason about signals or take actions. All reasoning, even the lightest kind, belongs to the Reflection Engine (starting at Micro depth).

### Responsibilities

- Collect signals from MCP servers (pending notifications, error counts, engagement data, recon findings)
- Track event counters since last reflection (conversations, errors, memories stored, findings)
- Track interaction depth since last reflection (not just count — tokens consumed, reasoning chains, novel discoveries, extended conversations). A single deep conversation can warrant more retrospective thought than ten trivial exchanges.
- Run lightweight programmatic health checks (process alive? API responding? storage within bounds?)
- Check time-since-last-reflection at each depth level
- Apply calendar floor/ceiling schedule (see below)
- When thresholds crossed → trigger Reflection Engine at appropriate depth
- Process escalation flags from previous Reflection Engine runs (see Depth Escalation Protocol below)

### Depth Escalation Protocol

The Awareness Loop is the SOLE authority that invokes the Reflection Engine. But Reflection Engine runs can flag that a deeper pass is warranted (e.g., Micro discovers a pattern that needs Light-depth analysis). The protocol:

1. Reflection Engine sets an **escalation flag** with target depth and reason (stored in extension state, not MCP)
2. Awareness Loop checks escalation flags on its NEXT tick (max 5-minute delay)
3. If flag is set and target depth isn't on cooldown → trigger that depth
4. **Critical escalation override:** If the Reflection Engine flags `critical=true` (e.g., cascading failure pattern), the Awareness Loop runs an immediate out-of-cycle tick via `force_tick()`. This is the ONLY case where the 5-minute interval is bypassed.

**Why this matters:** Without this protocol, either the Awareness Loop is the single coordinator (but can't respond to escalations faster than 5 minutes) or the Reflection Engine can self-trigger (but then you have two scheduling authorities). This gives clean ownership with a safety valve.

### AZ Runtime Authority Constraint

The Awareness Loop is Genesis's sole scheduling authority, but **Agent Zero's
`monologue()` controls when agents actually run.** Genesis proposes; AZ's
runtime decides. Concretely:

- The APScheduler tick runs in a DeferredTask thread, independently of the
  agent's conversation loop. It collects signals, scores urgency, classifies
  depth, and stores results to `awareness_ticks`.
- The extension that injects awareness briefings (`_51_genesis_awareness_briefing.py`)
  only fires when `monologue()` runs — which happens when the user starts a
  conversation or the task scheduler triggers one.
- **There can be a delay between the tick result and the agent seeing it.**
  If no monologue is running, the tick result sits in the database until the
  next monologue starts.
- `force_tick()` writes immediately to the database and creates an observation,
  but the agent still won't act on it until `monologue()` runs.

This is not a bug — it's a consequence of clean separation. The tick is the
perception; the monologue is the action. They run on different schedules.

### Awareness Loop Watchdog

The Awareness Loop is the single coordinator — if it dies, the entire cognitive
system goes silent. This requires external supervision, NOT health-mcp (which
depends on the Awareness Loop to call it — circular dependency).

**Mechanism:**
1. The Awareness Loop writes a heartbeat timestamp to a well-known file
   (e.g., `/tmp/genesis-awareness-heartbeat`) on every tick (~5 minutes).
2. An external systemd timer (or cron job) runs independently every 15 minutes.
   It checks whether the heartbeat file is stale (>15 minutes old).
3. If stale → restart the Awareness Loop process. Log the event.
4. If the restart itself fails → the systemd timer writes an alert file that
   the morning report generation can detect (file-based, no MCP dependency).

**Why not health-mcp:** health-mcp is invoked by the Awareness Loop. If the
loop is dead, health-mcp never runs. The watchdog must be infrastructure-level
process supervision, not application-level health checking.

**Three layers of recovery:**
- **Layer 1:** Agent Zero's own process management (crash → auto-restart)
- **Layer 2:** The systemd watchdog described above (hung process → kill + restart)
- **Layer 3:** User-visible signal — if the morning report stops arriving,
  something is deeply wrong. This is the "check engine light."

### Hybrid Scheduling: Event-Driven + Calendar Guardrails

Events set the tempo, calendar sets the minimum BPM.

| Depth | Event-driven trigger | Calendar floor | Calendar ceiling |
|-------|---------------------|----------------|-----------------|
| **Micro** | Software anomaly, new user interaction pattern, quick improvement idea | Every 30min | Max 2/hour |
| **Light** | Notable activity (5+ conversations, budget warning, recon finding flagged) | Every 6h | Max 1/hour |
| **Deep** | Backlog / salience spike (200+ unprocessed memories, budget alert, multiple recon findings) | Every 48-72h | Max 1/day |
| **Strategic** | Major signal (user goal change, paradigm shift in recon, quarterly boundary) | Every 7-14 days | Max 1/week |

**Adaptive floors:** Quiet periods tighten floors (more likely something was missed). If the system has been consistently event-triggering Deep reflections every 18h, the 72h floor is irrelevant. But if there's a quiet stretch, the floor tightens to 48h — because silence is MORE likely to need a "did I miss something?" check, not less.

**Why calendar floors matter:** Purely event-driven misses drift. If nothing crosses a salience threshold for 3 weeks, the system never asks "why has nothing been important? Have my models gone stale? Has the user's life changed?"

### Three Categories of Scheduled Work

The system has three distinct categories of scheduled work with different governing
principles. V2's lesson was that rigid fixed schedules for *reflection* were wasteful.
But that doesn't mean all regularity is bad — it means different kinds of work need
different scheduling philosophies.

**1. Event-driven reflection** — The Awareness Loop + Reflection Engine as described
above. Adaptive depth, triggered by signals, calendar floors as safety nets. Governed
by: "reflect when there's reason to, with guardrails against silence." This is the
cognitive scheduling that v2 got wrong by making it calendar-rigid.

**2. Genesis's own rhythms** — Predictable cadence items Genesis maintains for its
own self-maintenance. Examples: morning report, triage calibration cycle, quality
calibration audit, memory housekeeping. These are *rituals*, not reflections — their
value comes partly from regularity itself. The morning report is useful because the
user knows to expect it and Genesis uses it as a forcing function for self-assessment.
Triage calibration needs to run daily because calibration drift is time-based.

Governing principle: **predictable with opt-out.** Genesis does the morning report
daily because the regularity creates value. But Genesis can skip it if there's
genuinely nothing to report — the rhythm is a default, not a mandate. A morning
report that says "nothing happened, no questions, all quiet" is worse than no report.

These are distinct from event-driven reflection because they're not triggered by
signals — they're triggered by the calendar. And they're distinct from reflection
depth levels because they're specific jobs, not open-ended cognitive passes.

**3. User-scheduled cron jobs** — Things the user explicitly asks Genesis to do on a
schedule. "Check my email every morning." "Monitor this stock daily." "Run this report
every Friday." These are the *user's* rhythms, not Genesis's. Genesis executes them
on schedule, period. The user defines when, what, and how often.

Governing principle: **user's instructions, Genesis's execution.** Genesis doesn't
second-guess cron schedules (though it might suggest optimization if it notices a
cron job consistently produces no useful output — that's Philosophy #8 in action).

**Implementation note:** Category 3 crons use Agent Zero's built-in `TaskScheduler`
(`python/helpers/task_scheduler.py`), which already provides cron expression support,
UI integration, persistence, and context management. Genesis does not need to
rebuild cron infrastructure — only categories 1 and 2 use Genesis's APScheduler.

**The distinction matters because the governing principles differ:**
- Event-driven reflection adapts — more when needed, less when quiet
- Genesis's own rhythms are predictable — regularity itself is the value
- User crons are prescribed — the user decides, Genesis executes

The Awareness Loop is the coordinator for all three categories. Event-driven triggers
and calendar floors drive category 1. Internal cadence timers drive category 2. The
cron scheduler (recon-mcp's self-scheduling + future cron infrastructure) drives
category 3. But they are conceptually distinct and should not be conflated in design
or implementation.

---

## Layer 2: Reflection Engine

**Replaces:** HeartbeatService, CopilotHeartbeatService, DreamCycle (13 jobs), weekly review, monthly review
**Type:** Agent Zero extension, triggered by Awareness Loop
**Cost:** Variable per depth (Micro = near-zero via 20-30B on GPU machine or Gemini Flash free tier, Light = low, Deep = medium, Strategic = high)

### Depth Levels

**Micro** (quick sanity pass):
- Cheap LLM call: 20-30B model (GPU machine when available, else Gemini Flash free tier)
- NOT a health monitor. The question Micro answers is: "Anything worth paying attention to right now?"
- Biased toward: user value opportunities ("user asked about X yesterday, I found related info"), self-improvement ideas ("that task failed because of Y, I should note the pattern"), software anomaly detection ("API error rate spiked", "gateway returned malformed JSON", "config value looks wrong")
- Hardware health is NOT Micro's focus — the Awareness Loop catches the rare hardware issue (storage limits) programmatically. Micro thinks about software reliability, user needs, and system improvement.
- Output: brief observations, optional flag for Light reflection if something warrants deeper thought

**Light** (replaces heartbeat):
- Brief LLM reflection on recent activity
- Queries memory-mcp for recent events, recon-mcp for new findings, health-mcp for status
- Runs inline salience evaluation: "Of these signals, which are worth acting on?"
- Runs inline user model synthesis: "Given recent interactions, update user profile"
- May trigger outreach for high-salience items
- Output: observations written to memory, possible outreach queued

**Deep** (replaces nightly dream cycle):
- Comprehensive review, **but only runs jobs that have pending work**
- Memory consolidation → only if unprocessed backlog exists
- Cost reconciliation → only if spend changed since last check
- Lessons extraction → only if new retrospectives exist
- Identity reflection → only if observations warrant SOUL.md consideration
- Recon triage → only if new findings exist
- Observation cleanup → only if stale observations accumulate
- Output: consolidated memories, updated observations, drive weight adjustments
- **Batch interleaving:** When Deep reflection processes large backlogs (e.g., 200+ memories for consolidation), 3B extraction/tagging calls are interleaved in small batches (e.g., 10 items at a time) with yield points between batches. This prevents the Ollama 3B from being monopolized by a single Deep reflection run, keeping it responsive for embeddings and extraction from concurrent active operations. The 20-30B/cloud reasoning calls within Deep are not affected (they're async network I/O and don't contend with the Ollama container).

**Strategic** (replaces weekly + monthly reviews):
- MANAGER role (weekly cadence): architecture review, memory quality, recon triage, cost analysis
- DIRECTOR role (monthly cadence): audits weekly findings, adjusts budgets, strategic assessment
  - **Goal-vs-reality drift check:** Compare Genesis's stated goals/priorities (from SOUL.md, active project definitions, user-stated priorities) against actual behavior over the past 30 days (from execution traces, time allocation, memory writes). Report: (1) what Genesis said it would focus on vs what it actually spent time on, (2) dormant goals that received zero attention, (3) emergent behaviors not in any stated goal — is this discovery or drift? This is distinct from the weekly self-assessment ("am I improving?"); the drift check asks "am I working on what I'm supposed to be working on?"
- **Can modify system parameters:** adjust Awareness Loop thresholds, Reflection Engine drive weights, outreach preferences
- **Capability gap review:** Reviews accumulated capability gaps since last Strategic. For each: (1) How many times was this gap hit? (2) What would it take to close it (new tool, MCP integration, skill)? (3) Is the investment justified by frequency and user impact? Proposes capability acquisitions for user approval (L5+ autonomy).
- Output: strategic adjustments, configuration changes, high-level observations, capability acquisition proposals

Strategic reflections track an internal counter. Every Strategic reflection runs MANAGER-scope work. Every 4th Strategic reflection (or when >28 days since last DIRECTOR pass) also runs DIRECTOR-scope work. This gives roughly weekly MANAGER and monthly DIRECTOR cadence while keeping them in a single depth level.

### Inline Capabilities (Prompt Patterns)

These are NOT separate servers. They're sections of the Reflection Engine's prompt that are activated at appropriate depths:

**Salience Evaluation:**
> "Given these signals, the current user model, and recent engagement data (last 10 outreach events: 7 engaged, 3 ignored — topics ignored were...), score each signal on relevance (0-1), urgency (0-1), and actionability (0-1). Which cross the threshold for outreach?"

**User Model Synthesis:**
> "Query memory for recent interactions. Synthesize into structured profile: current interests, active skills, behavioral patterns, stated goals, communication preferences, inferred blind spots. Compare against cached profile — what changed?"

**Dust-Off Sweep (triggered by User Model changes):**
When the User Model is updated with a meaningful change (new interest, preference shift, life event), trigger a cross-reference against dormant/archived items: old observations, parked project ideas, deferred tasks, archived recon findings. Surface anything that becomes relevant again given the new user context.

Example: User mentions learning Rust → sweep dormant items → find an archived recon finding about a Rust tool from 3 months ago that was filed as "not relevant" → resurface it.

This prevents the "right information, wrong time" problem where valuable findings get buried because they arrived before the user needed them. Runs at Light depth or above (needs LLM judgment to assess relevance). Not every User Model change triggers a sweep — only changes tagged as `interest_shift`, `goal_change`, or `life_event`, not routine updates like communication preference tweaks.

**Social Simulation (World Model):**
> "Before sending this outreach message, simulate the user's reaction. Given their profile [user model], if they received: '[draft message]' — would they find it (a) valuable and act on it, (b) interesting but not actionable, (c) irrelevant/annoying? Adjust or suppress based on simulation."

**Governance Check (Constitutional):**
> "This action was proposed by [source]. Check: (1) Does it align with user's autonomy permissions for this category? (2) Is it reversible? (3) Is it within budget? (4) Has the user previously rejected similar actions? If any answer fails, hold for user approval."

**Drive Weighting:**
Four drives shape what the Reflection Engine focuses on:
- **Curiosity:** Explore new information, investigate unknowns
- **Competence:** Improve at things done often, optimize procedures
- **Cooperation:** Help the user proactively, surface opportunities
- **Preservation:** Maintain system health, manage resources

Weights are initial-configured but adjusted by the Self-Learning Loop based on feedback. If proactive suggestions keep getting acted on → cooperation weight rises. If the system keeps breaking → preservation weight rises.

**Bounds:** No single drive may drop below 0.10 or rise above 0.50. This prevents any drive from being effectively silenced or from dominating all reflection. Initial weights: preservation 0.35, curiosity 0.25, cooperation 0.25, competence 0.15.

**Normalization:** Drive weights are **independent sensitivity multipliers**, NOT a normalized budget that must sum to 1.0. "Preservation at 0.45" means health signals weigh 0.45x in reflection focus. This can coexist with "cooperation at 0.40" without conflict. If drives were normalized (zero-sum), raising one would necessarily lower others — preventing the system from responding to "I need more preservation AND more cooperation" simultaneously. The initial values happen to sum to 1.0 but this is coincidental, not a constraint.

---

## Layer 3: Self-Learning Loop

**Replaces:** Task retrospectives, lessons-learned extraction, post-mortem pipeline
**Type:** Agent Zero extension, runs after interactions and outreach events
**This IS the "Dopaminergic System" — learning from prediction errors**

### After Every Interaction

The Self-Learning Loop runs after **every** interaction. There is no pre-gate that
decides whether an interaction is "worth" reflecting on — that decision IS the first
step of retrospection, not a prerequisite for it.

**Step 0 — Triage:** Assess how much retrospective depth this interaction warrants.
Two-stage implementation: a programmatic pre-filter catches the definitionally trivial,
then the 3B SLM classifies everything else.

**Stage 1 — Programmatic pre-filter (zero cost):**
- Total interaction < ~100 tokens AND zero tool calls → depth 0
- Everything else → pass to Stage 2

The floor is intentionally very low. Only "hi," "thanks," "ok," and single-fact
lookups with obvious answers are programmatically skipped. Since the 3B SLM is local
and near-free, there's no reason to aggressively filter — let the SLM decide.
The SLM assesses actual characteristics (complexity, blockers, effort, novelty),
not labels. A formal task, a cron job, a browser session, and a deep conversation
all get the same characteristic-based assessment.

**Stage 2 — 3B SLM classification (near-zero cost, on-prem):**
- Receives a structured prompt with the interaction summary
- Prompt includes: few-shot examples (5-8 covering each depth level) + calibration
  rules (specific instructions generated by the triage calibration cycle — see below)
- Outputs a depth assignment (0-4) with a one-line rationale
- Latency tolerance: not real-time. If the 3B model is busy when a triage request
  arrives, queue it. If multiple interactions complete while triage is running,
  batch them into a single classification call (one prompt with multiple interaction
  summaries). This avoids head-of-line blocking and produces better classifications
  because the model sees related interactions together. No fixed latency SLA —
  throughput matters more than per-request latency for background triage.

**Triage signals the SLM considers:** Total token count of the interaction (user +
Genesis + tool outputs), number and type of tool calls, whether novel information was
encountered, whether reasoning chains were multi-step, whether assumptions were
challenged or corrections made, whether discoveries changed the picture, and whether
the interaction connects to active projects or high-salience topics (as specified by
calibration rules).

| Depth | What It Means | Characteristics (any of these, not all) | Steps Applied |
|-------|---------------|------------------------------------------|---------------|
| **0** | Nothing worth examining | Trivial exchange, no novel content, no tools, no surprises | Step 0 only |
| **1** | Quick note | Clear outcome, low complexity, no ambiguity | Steps 0-2 |
| **2** | Worth thinking about | Discoveries, corrections, novel reasoning, surprising outcomes, extended effort | Steps 0-3 |
| **3** | Full analysis warranted | High complexity, multiple tools/steps, blockers encountered, significant user stakes, multi-turn problem-solving | Steps 0-5 |
| **4** | Full analysis + workaround review | Blockers hit and workarounds attempted, failed approaches to learn from, creative problem-solving required | Steps 0-5 + item 7 review |

**Depth is determined by characteristics, not by labels.** A formal task that was
trivially completed might only warrant depth 1. A complex browser session that hit
blockers and required workarounds warrants depth 4 regardless of whether it entered
through a task intake system. A cron job that encountered unexpected failures warrants
depth 3. Triage assesses what actually happened, not what category the interaction
belongs to. "Formal task" is one signal that correlates with depth 3+ but is neither
necessary nor sufficient.

**Cost proportionality:** Retrospective cost should be proportional to the
interaction's value and complexity — spending $0.50 analyzing a $0.10 exchange is
waste. Use the 3B SLM for triage and the 20-30B for lightweight retrospectives;
escalate to Sonnet-class only for depth 3+ analysis.

**This is the core of the "Dopaminergic System":** Every interaction produces a
signal. Triage is the filter that separates noise from learning opportunities. But
the filter runs on everything — it doesn't pre-exclude. A seemingly trivial exchange
that reveals a user preference or a surprising failure mode gets promoted to depth 2+
by triage, even if the interaction itself was brief.

### Triage Calibration Cycle (Daily)

The 3B SLM is too small to reason about whether its own triage decisions are good.
A more capable model audits the triage and calibrates the 3B's prompt. This follows
the general evaluation principle: **the evaluating model should be the next size up
from the model being evaluated.** 30B evaluates 3B. Sonnet evaluates 30B. The regress
stops at the most capable model available for periodic strategic review.

**Cadence:** Daily, as a companion job to the Morning Report (Phase 8). Runs on the
20-30B model (GPU machine or Gemini Flash fallback).

**The daily triage calibration cycle:**

1. **Sample recent triage decisions** across all depth levels from the last 24h
2. **Under-classification audit (depth 0 review):** Pull the actual chat logs for
   interactions that triage assigned depth 0. The chat logs are the ground truth —
   they exist in Agent Zero's conversation history regardless of triage decisions.
   The 30B reads each skipped interaction and asks: "Was anything here worth
   capturing? A user correction, a preference signal, a surprising fact, a topic
   that connects to something active?" Under-classification is detected **proactively
   from the logs**, not reactively from downstream failures.
3. **Over-classification audit (depth 2+ review):** For interactions assigned depth 2+,
   check whether the retrospective outputs were subsequently retrieved or used. If
   depth 3 retrospectives consistently produce observations that nothing ever reads,
   those interactions were over-classified.
4. **Memory pattern review:** Read current memory trends — what topics are being
   recalled frequently? What's accumulating? What's gone stale? These patterns
   inform salience shifts that triage should account for.
5. **Output: updated calibration for the 3B's triage prompt:**
   - Refreshed few-shot examples (best/worst from the audit become new examples)
   - Specific calibration rules: "interactions mentioning [topic X] in the context
     of [conditions] should be at least depth 2" or "remove the bump for [topic Y],
     no longer high-salience"
   - These are prompt-level changes — instantly effective, no retraining

**Why the 30B mediates memory signals, not the 3B directly:** Feeding raw memory
patterns to the 3B (e.g., "project X is high-salience") risks over-influence. The
3B lacks the reasoning capacity to distinguish "project X mentioned in passing" from
"substantive discussion about project X" — it will keyword-match and over-classify.
The 30B digests memory intelligence into specific, actionable rules that the 3B can
follow without needing to reason about ambiguity.

**Strategic-level triage quality review (weekly):** During Strategic reflection
(weekly cadence), Sonnet/Opus reviews the 30B's calibration quality itself: "Are the
calibration rules actually improving triage accuracy? Is the 30B systematically
miscalibrating in some direction?" This is the fixed point where the evaluation
regress stops — the most capable model periodically auditing the auditor.

**V3 → V4 progression:**
- **V3:** Triage uses few-shot prompt engineering with 30B-curated examples and
  calibration rules. All calibration is prompt-level (instantly changeable).
- **V4:** Once hundreds of audited triage decisions accumulate, fine-tuning the 3B
  via LoRA adapter becomes viable. LoRA keeps the base model clean for embeddings
  and extraction while adding a triage-specific adapter. The 30B's correction history
  becomes the training dataset. Fine-tuning introduces stickiness (weight changes
  vs. prompt changes), so it requires enough data to train reliably and a mechanism
  to retrain when priorities shift.

**Calibration storage (implementation note):** The calibration rules and few-shot
examples produced by the daily cycle need a concrete home. Options: (a) a workspace
file (e.g., `TRIAGE_CALIBRATION.md`) that the 3B prompt reads at invocation time,
(b) a dedicated table or JSON field in the database. The workspace file approach
matches the pattern used for SOUL.md and the cognitive state summary — the 30B
writes the artifact, the 3B's prompt template includes it. Decision: resolve during
Phase 6 implementation.

### Quality Calibration Cycle (Weekly)

Sycophancy in task execution is subtler than in conversation. It's not "telling the
user what they want to hear" — it's a gradual drift toward easier outputs. Quality
gates pass more often. Clarifying questions get asked less. Pushback decreases.
Output complexity slowly matches what the user *accepts* rather than what they *need.*
This is especially insidious because it's invisible — both to Genesis and to the user,
who may not notice standards slipping until the gap is significant.

**Detection mechanism:** The same evaluation hierarchy used for triage calibration
applies to quality calibration. During Strategic/MANAGER reflection (weekly cadence),
the most capable available model samples recent task outputs and assesses:

1. **Quality gate strictness:** "Were quality gates appropriately strict here, or did
   Genesis accept output that a thoughtful person would have pushed back on?" Sample
   tasks where quality gates passed and ask whether the pass was justified.

2. **Pushback frequency and quality:** "Did Genesis ask clarifying questions where a
   thoughtful person would have? Did it challenge assumptions when evidence warranted
   it? Or did it just execute?" Compare against the Pre-Execution Assessment mandate.

3. **Quality drift over time:** "Compare the quality standard Genesis applied to
   similar task types in the current period vs. earlier periods. Is there directional
   drift?" A declining trend in quality gate failures (fewer fails over time) is
   suspicious — it could mean Genesis is getting better, OR it could mean standards
   are slipping. The auditor distinguishes by examining the outputs, not just the
   pass rates.

4. **Signal weight compliance:** "Did any behavioral changes this week erode a
   philosophical commitment? Did pushback frequency decrease for reasons other than
   improved request quality?" Cross-reference against signal weight tier boundaries.

**Why this can't self-correct:** Genesis can't detect its own sycophantic drift for
the same reason a person can't — the drift feels like "getting better at giving the
user what they want," which is indistinguishable from genuine improvement without an
external reference point. The external reference point is the philosophical commitment
(honesty, pushback, reasonableness) assessed by a more capable model that isn't
subject to the same drift pressure.

**Output:** Quality calibration observations, stored as observations in memory-mcp.
If drift is detected, the observation is tagged `quality_drift` with specific
examples. These observations are retrieved during future Pre-Execution Assessments
as a counterweight: "recent quality audit flagged drift in task type X — apply
stricter standards."

The following steps apply after triage assigns depth 1+. Steps are cumulative —
higher depths include all lower steps.

1. **Task retrospective:** What was attempted? What succeeded/failed? What was surprising? → store in memory-mcp (episodic)
2. **Root-cause classification:** Categorize the outcome to route feedback correctly:
   - `approach_failure` — Genesis tried an approach that didn't work. Feeds procedural
     memory adjustment (update procedure confidence, record failure mode).
   - `workaround_success` — Primary approach failed, but Genesis found an alternative
     path that achieved the goal. The workaround is stored as procedural memory and
     becomes the primary approach for future identical tasks. This is a POSITIVE outcome
     — Genesis is now better at this task type than before the failure.
   - `capability_gap` — Genesis lacked a tool, integration, or skill needed for the task
     AND the workaround search (see §Task Execution → item 7) was exhausted without
     finding an alternative. Does NOT penalize procedural memory. Logged to capability
     gap accumulator for Strategic reflection review. **This classification requires that
     the workaround search explored genuinely different strategies (not variations of the
     same approach) and either found the constraint to be definitive or exhausted the
     search budget.** A single failed attempt is NOT a capability gap — it's an incomplete
     workaround search. What matters is the diversity and quality of the search, not a
     specific count of attempts.
   - `external_blocker` — Something genuinely outside Genesis's control blocked the task
     AND no workaround exists. Requires workaround search exhaustion (same standard as
     `capability_gap`). Categorized further: (a) user-rectifiable (surface to user),
     (b) current technology limitation that may become feasible later (parked with
     `revisit_after` date), (c) permanent constraint (logged, no action). **The bar for
     "permanent constraint" is high** — very few things are truly permanent. Prefer (b)
     over (c) when uncertain.
   - `success` — Task completed via primary approach. Feeds procedural memory confidence
     increase.
3. **Request-delivery delta assessment:** Compare what the user asked for against what
   Genesis actually delivered, **in light of what was discovered during execution.** Tasks
   aren't static — the "right answer" evolves as Genesis encounters new information. The
   delta assessment captures three things: the gap, why it exists, and whether Genesis
   adapted correctly.

   **Step 3a — Scope evolution:** Did the task scope change during execution? If yes, log:
   - `original_request` — What the user asked for (verbatim or summarized)
   - `discoveries` — What Genesis learned during execution that changed the picture.
     Examples: "site requires enterprise auth," "competitor was acquired last week,"
     "3 of 10 PDFs are corrupted," "user's stated requirement contradicts their actual
     usage pattern"
   - `adjusted_scope` — How Genesis reframed the task given discoveries. If scope didn't
     change, this is identical to the original request.
   - `scope_communicated` — Did Genesis inform the user of the scope change before or
     during delivery? (yes/no). Uncommunicated scope changes feel like misinterpretation
     even when the adjustment was correct.

   **Step 3b — Delta classification** (measured against *adjusted* scope, not original):
   - `exact_match` — Delivered precisely what the adjusted scope called for.
   - `acceptable_shortfall` — Output fell short of adjusted scope. Log the specific gap.
   - `over_delivery` — Produced more than adjusted scope. Track if extras were utilized.
   - `misinterpretation` — Solved the wrong problem even after accounting for discoveries.

   **Step 3c — Discovery attribution (multi-valued):** Why did the delta (or scope
   change) happen? Real failures often have multiple contributing factors. Attribution
   is an ARRAY, not a single value — "the request was underspecified AND there was an
   external limitation AND Genesis could have interpreted better" is three attributions
   that each route to different improvement paths.
   - `external_limitation` — Something outside Genesis's control (API down, auth required,
     data corrupted, service deprecated). Neither party is "wrong." Learning signal:
     remember this limitation for similar future tasks; don't penalize procedures.
   - `user_model_gap` — The user's request was based on incomplete or incorrect assumptions
     (e.g., "summarize my competitor's blog" when the competitor has no blog). Learning
     signal: for this type of request, ask clarifying questions earlier. Feeds the user
     model — "user sometimes assumes X about topic Y."
   - `genesis_capability` — Genesis could have done better with a different tool, model,
     or approach. Learning signal: feeds capability gap accumulator or procedural memory.
   - `genesis_interpretation` — Genesis misread the user's intent despite having enough
     information to get it right. Learning signal: interpretation heuristic correction,
     NOT procedural memory. "When user says X in context Y, they mean Z."
   - `scope_was_underspecified` — The request was genuinely ambiguous; neither party had
     enough information upfront. Learning signal: for this task type, proactively clarify
     scope before executing.
   - `user_revised_scope` — The user explicitly changed what they wanted mid-task. This is
     NOT an error by either party — it's a legitimate scope evolution. Learning signal:
     minimal (this is normal behavior). Track frequency per task type — if revisions are
     very frequent, it may signal that Genesis should checkpoint scope earlier.

   Each attribution independently routes its learning signal to the appropriate system.
   A task with `[external_limitation, genesis_capability]` feeds BOTH the "remember this
   limitation" path AND the capability gap accumulator — neither signal is lost.

   **Attribution → learning signal routing (concrete targets):**
   - `external_limitation` → observation in memory-mcp ("X doesn't work because Y").
     Retrieved for future similar tasks as context, not as a procedure.
   - `user_model_gap` → user model update ("user assumes X about Y") + procedural note
     ("for task type Z, ask clarifying questions about Y before executing").
   - `genesis_capability` → capability gap accumulator (if workaround search exhausted)
     or procedural memory update (if a better approach was identified).
   - `genesis_interpretation` → observation tagged `interpretation_correction` ("when
     user says X in context Y, they mean Z"). Retrieved for future similar requests.
     NOT procedural memory — this is a communication pattern, not a task execution pattern.
   - `scope_was_underspecified` → procedural note for task type ("proactively clarify
     scope before executing"). Feeds "scope checkpoint" behavior.
   - `user_revised_scope` → no direct learning unless frequency is anomalous.

   This step runs AFTER root-cause classification (step 2) and applies to ALL outcomes,
   including `success`. A task can be `success` + `acceptable_shortfall` +
   `[external_limitation, scope_was_underspecified]` simultaneously.

   **Why this matters:** Without discovery attribution, the Self-Learning Loop can't
   distinguish "I need to get better at this" from "this was impossible" from "I need to
   ask better questions upfront." Each routes to a completely different improvement path.
4. **Lessons extraction:** Any reusable procedures learned? → store in memory-mcp
   (procedural).
   - `success` and `workaround_success` outcomes update procedural memory confidence.
   - `workaround_success` is ESPECIALLY high-value: store both the failed primary
     approach AND the successful workaround. The workaround becomes the primary procedure
     for future identical tasks; the failed path is recorded as a known dead end.
   - `approach_failure` updates existing procedure confidence downward.
   - `capability_gap` and `external_blocker` do NOT penalize procedural memory — the
     system shouldn't "learn" that it's bad at tasks it simply can't do yet. But these
     MUST have passed the workaround search first (see root-cause classification above).
5. **Prediction error logging:** "Expected X, got Y" → used by Reflection Engine to calibrate future expectations

### After Every Outreach Event

1. **Engagement tracking:** Store in outreach-mcp: `{signal_type, topic, salience_score, channel, delivered_at, opened_at, user_response, action_taken}`
2. **Prediction error:** `salience_score` predicted engagement. Actual engagement was higher/lower. Compute error.
3. **Drive weight adjustment:** Positive engagement on cooperation-driven outreach → increase cooperation weight. Ignored outreach → decrease.
4. **Salience calibration:** "Outreach about topic X at score 0.78 was ignored → adjust threshold for similar topics"

### Engagement Signal Heuristics (Per-Channel)

Engagement inference is the primary training signal for salience calibration.
Default heuristics (per-adapter, overridable):

| Channel | "Engaged" | "Ignored" | "Neutral" |
|---------|-----------|-----------|-----------|
| WhatsApp | Reply or read receipt + action within 4h | No read receipt in 24h, OR read but no reply to a question in 12h | Read receipt but no reply on non-question within 24h |
| Telegram | Reaction or reply | No reaction or reply in 24h | Message read (if available) but no reaction |
| Web UI | Click-through, reply, or explicit feedback button | Page loaded but no interaction in session | Viewed in digest but no action |

These heuristics are initial defaults. The Self-Learning Loop can propose adjustments (L6 autonomy — user-approved) if engagement patterns suggest the heuristics are miscalibrated.

### Signal Weight Tiers

Not all learning signals are created equal. A direct user correction is a strong,
reliable signal. Whether the user "used" a deliverable is a weak, ambiguous signal.
The Self-Learning Loop must weight signals proportionally — a weak signal should
produce a small behavioral adjustment (or none), while a strong signal can drive
immediate change.

**Bootstrap tiers (starting point, not gospel):**

| Tier | Weight | Signal Types | Behavioral Impact |
|------|--------|-------------|-------------------|
| **Strong** | High | Direct user corrections, explicit feedback ("this was wrong / great"), user-initiated scope changes mid-task, user rejecting a deliverable | Can drive immediate behavioral change — procedural updates, interpretation corrections, user model updates |
| **Moderate** | Medium | Clear task success/failure (programmatic verification), outreach engagement (replied, acted on, ignored), task outcomes with unambiguous results | Feeds calibration over multiple instances — salience thresholds, drive weights, procedural confidence |
| **Weak** | Low | Behavioral inference (did they use it?), silence (no response), user override outcomes (were they right?), indirect signals (time spent, modifications made) | Noted, contributes to aggregate patterns, but no single weak signal should drive behavioral change alone |

**Critical constraint: weak signals must not erode philosophical commitments.**
User override outcomes are a weak signal. Even if the user is "right" after
overriding Genesis ten times in a row, this must not erode the commitment to
pushback (Philosophy #3, #8). Pushback is philosophically mandated — it happens
when evidence warrants it, regardless of how often it's been overridden. Some
behaviors are load-bearing and don't adapt based on signal pressure. The signal
weight system must respect this boundary.

**Calibration over time:** The tiers above are bootstrap defaults. Genesis calibrates
signal weights using the same evaluation hierarchy pattern as triage calibration:

1. **Does the signal predict useful behavioral change?** The 30B model periodically
   reviews: "We adjusted procedure X based on a Tier 2 signal. Did subsequent
   executions improve?" Signals that consistently fail to predict useful changes
   should be weighted lower. Signals initially weighted low that turn out to be
   reliable predictors should be promoted.

2. **Is the weighting producing proportionate responses?** During Strategic reflection,
   the more capable model reviews: "Here are behavioral changes Genesis made this
   week and the signals that drove them. Were the changes proportionate? Did any
   weak signal produce a large behavioral change? Did any strong signal get ignored?"

3. **Are aggregate weak signals being handled correctly?** A single weak signal
   should produce no change. But if 50 weak signals point in the same direction,
   that aggregate pattern is moderate-to-strong evidence. The calibration system
   must distinguish "one user override" (noise) from "a consistent pattern of user
   overrides on a specific topic" (signal worth examining).

**V3 scope:** V3 ships with the bootstrap tiers and the calibration infrastructure
(periodic capable-model audit of signal-driven changes). Signal weights are coarse —
three tiers, applied by the LLM's judgment, not by mechanical rules. V4 refines
this as interaction history accumulates and calibration data reveals which signals
actually predict useful learning.

### Over Time

This loop makes proactive behavior increasingly accurate:
- **Month 1:** Conservative. High thresholds. Mostly reactive with occasional blocker/alert outreach.
- **Month 3:** Calibrated. Engagement data has shaped salience thresholds. Finding/insight outreach begins to land.
- **Month 6+:** Anticipatory. Rich user model + calibrated drives + procedural memory = can identify opportunities the user hasn't thought of.

### Open Questions and Persistent Curiosity

Genesis maintains things it doesn't know yet — open questions, unresolved
uncertainties, gaps in understanding. These are not a task backlog to grind through.
They are a persistent curiosity layer: good notes, patient memory, opportunistic
connection-making.

**The distinction: outreach expires, curiosity persists.** When Genesis has a question
— whether from self-reflection, a failed behavioral inference, an ambiguous task
outcome, or any other source — it may ask the user. That *outreach attempt* has a
shelf life. If the user doesn't answer within a reasonable window (context-dependent,
but roughly 48 hours for non-critical questions), Genesis does not re-ask. The
question does not enter a backlog. Genesis does not nag.

But the *underlying question* — the thing Genesis is uncertain about — persists in
memory as an open observation. Not as a pending item demanding attention, but as a
note: "I still don't know X." If, two weeks or two months later, the user mentions
something relevant in conversation, or Genesis encounters information that answers
the question through observation, or a task outcome resolves the uncertainty — Genesis
can make that connection retroactively. The answer arrives when it arrives, and Genesis
recognizes it.

**This is broader than feedback.** The principle applies to any uncertainty Genesis
carries:

- *Feedback uncertainty:* "Did the user find that research report useful?" — asked
  once, no response, question persists. Three weeks later the user references a finding
  from the report in a new request → connection made, question resolved.
- *Task uncertainty:* "The user asked for X but I suspect they actually need Y" —
  noted in Pre-Execution Assessment, executed as X per user direction. Later, user
  comes back asking for Y → connection made, procedural note updated.
- *Domain uncertainty:* "I don't understand why the user structures their projects
  this way" — observed, noted. Over multiple interactions, the pattern reveals itself.
- *Capability uncertainty:* "I couldn't do X because of limitation Y — but Y might
  change" — logged with `revisit_after` date, persists until resolved or confirmed
  permanent.

**Implementation:** Open questions are stored as observations in memory-mcp, tagged
`open_question` with the domain and originating context. They participate in normal
memory retrieval — when the user says something or Genesis encounters information
that's semantically similar, the open question surfaces as context. The LLM decides
whether the new information resolves the question. No special matching infrastructure
is needed beyond what the memory system already provides.

**The morning report as a question seam:** Genesis's daily rhythm (see §Three
Categories of Scheduled Work) includes the morning report. This is a natural point
for Genesis to surface 1-2 questions from recent self-reflection — not a questionnaire,
not a backlog dump, just the most useful thing it's currently uncertain about. If the
user engages, that's a strong calibration signal. If they don't, the outreach attempt
expires but the question persists in memory.

**What this is NOT:**
- A task queue of "outstanding unanswered questions" — there is no backlog
- A nagging mechanism — each question is asked at most once via outreach
- An over-engineered retrieval system — it uses the existing memory infrastructure
- A reason to delay action — Genesis acts on what it knows and notes what it doesn't

**Design principle:** Take good notes. Be patient. Await answers you might not have
expected. A persistent question answered unexpectedly three months later is worth
more than an aggressive question asked five times in a week.

---

## 4 MCP Servers

### 1. memory-mcp

**Existing design from v3 plan, expanded with:**
- **Procedural memory type:** Structured "how-to" records, not narratives. Schema: `{task_type, steps[], tools_used[], success_rate, failure_modes[], context_tags[], last_used, times_used}`
- **Observations:** Folded in from the separate genesis-observations concept. Observations are processed reflections — a form of memory, not a separate concern.
- **User model cache:** Periodically synthesized user profile stored as a semantic record, refreshed by Reflection Engine during Light+ reflections.

**Memory tools:**
- `memory_recall` — Hybrid search (Qdrant vectors + FTS5 full-text, RRF fusion). Accepts `source` param: `memory | knowledge | both`
- `memory_store` — Store with source metadata + memory type tag
- `memory_extract` — Store fact/decision/entity extractions
- `memory_proactive` — Cross-session context injection
- `memory_core_facts` — High-confidence items for system prompts
- `memory_stats` — Health and capacity metrics
- `observation_write` — Write processed reflection/observation
- `observation_query` — Query by type/priority/source
- `observation_resolve` — Mark resolved with notes
- `evolution_propose` — Write identity evolution proposal (for SOUL.md / identity file changes)
- `evolution_propose_review` — Transition a proposal out of pending (approved / rejected / withdrawn)

**Knowledge base tools (post-v3 feature, groundwork laid in v3):**
- `knowledge_recall` — Hybrid search scoped by project/domain, authority-tagged results
- `knowledge_ingest` — Store distilled knowledge units with full provenance metadata
- `knowledge_status` — Collection stats, staleness report, project index

**Knowledge base concept:** A separate-but-colocated data layer for **immutable reference material** — course content, specs, reference docs — that Genesis treats as authoritative source of truth (not subject to memory consolidation, decay, or revision). Distilled by LLM into structured knowledge units before storage. Primary consumers are background agents, task execution sub-agents, and the Self-Learning Loop audit trail — not main conversation context injection (avoids context window budget pressure). See `post-v3-knowledge-pipeline.md` in project docs for full design.

> **V3 groundwork requirements (implement during v3, not post-v3):**
> - Retrieval interface accepts `source` parameter (`memory | knowledge | both`)
> - Qdrant client wrapper supports multiple named collections (not hardcoded to `episodic_memory`)
> - Context injection tags each block with `source_type` so the LLM distinguishes recalled memory from reference material
> - Token budget system for context injection is shared across memory AND knowledge retrieval
> - Raw text stored alongside vectors in knowledge collection (enables re-embedding on model change without re-ingestion)
> - FTS5 table schema supports a `collection` column for knowledge vs memory separation
>
> These are design decisions, not extra code: an enum instead of a hardcoded string, a collection name parameter instead of a constant, a `source_type` field on injected blocks.
>
> **Why knowledge lives in memory-mcp, not a separate server:** Applying the same test used
> to reject other servers: "Does this need its own process, persistent state, and lifecycle?"
> No. Knowledge shares infrastructure (Qdrant, embedder, FTS5, SQLite pool) and retrieval
> patterns (hybrid search, RRF fusion). It needs its own Qdrant collection, FTS5 table, and
> retrieval filter — not its own process.

### 2. recon-mcp

**Existing design from v3 plan.** Self-scheduling intelligence gathering:

| Job | Schedule | Source |
|-----|----------|--------|
| Email reconnaissance | Daily 5AM | Configured email sources |
| Web source monitoring | Friday 6AM | Configured URLs/feeds |
| GitHub landscape | Saturday 6AM | Repos, releases, trends |
| Model intelligence | Sunday 6AM | Provider announcements, benchmarks |
| Source discovery | Monthly | Discover new relevant sources |

Self-manages schedules internally. Pushes high-priority findings as notifications to Awareness Loop. Low-priority findings accumulate for triage during Deep/Strategic reflections.

**Tools:**
- `recon_findings` — Query/store findings
- `recon_triage` — Mark findings triaged with notes
- `recon_schedule` — View/modify gathering schedule
- `recon_sources` — Manage watched sources

### 3. health-mcp

**New.** Lightweight software health awareness — NOT an enterprise monitoring system. Genesis is a cognitive assistant that happens to be self-maintaining, not a sys admin. Health monitoring exists so Genesis can fix its own problems when they arise, not so it can spend cycles worrying about uptime.

**Focus: software reliability** (the real failure modes):
- **API/provider status:** Which APIs are responding? Which are returning errors? Which are rate-limited? (Crashes, dead APIs, and non-graceful degradation are the #1 real-world failure mode)
- **Error tracking:** Rolling window of errors with pattern detection — malformed JSON, config issues, unexpected exceptions, gateway crashes
- **Process health:** Are background services running? Did something crash silently?
- **Storage:** The one hardware check that matters — disk usage approaching limits (backups, logs, DB growth)

**NOT in scope:** CPU monitoring, memory profiling, network throughput, latency histograms, baseline learning for statistical deviation. This isn't Datadog. If something breaks, Genesis notices and fixes it. If nothing breaks, health-mcp is quiet.

**Tools:**
- `health_status` — Current system health snapshot (API status, error counts, process state, storage)
- `health_errors` — Recent error log with pattern grouping
- `health_alerts` — Active alerts (software failures, storage warnings)

### 4. outreach-mcp

**New.** Manages all proactive communication with the user:

- **Channel registry:** Pluggable adapter pattern. Initial channels: WhatsApp, Telegram, Agent Zero web UI. Channels are registered dynamically — adding Telegram, Slack, email, etc. requires only a new adapter, not architecture changes. No hardcoded "primary" — the system learns which channel the user prefers for which outreach type over time.
- **Delivery queue:** Messages queued with urgency level, preferred timing, and channel override
- **Quiet hours:** User-defined "don't disturb" windows (e.g., 10PM–7AM)
- **Engagement tracking:** Per-message: delivered → opened → responded → acted_on (or ignored). WhatsApp read receipts + reply detection.
- **Digest generation:** Batch low-priority items into periodic summaries (daily or weekly, user-configurable)
- **Feedback mechanism:** Channel-appropriate. Telegram: reaction buttons (👍/👎). WhatsApp: reply-based ("Reply 👍 or 👎"). Web UI: native UI elements. All channels also infer from engagement patterns (acted on = positive, ignored = negative). The adapter interface defines `get_engagement_signals()` so each channel reports feedback in its native way.

**Tools:**
- `outreach_send` — Queue a message for delivery
- `outreach_queue` — View pending messages
- `outreach_engagement` — Query engagement history (for Self-Learning Loop)
- `outreach_preferences` — Get/set user channel preferences and quiet hours
- `outreach_digest` — Generate a digest of queued low-priority items

### Cross-Cutting: MCP Server Write Idempotency

All 4 MCP servers must implement idempotent write operations. When a write times out
after succeeding server-side, retrying must not create duplicates. Approach:

- **memory-mcp:** Content hash + timestamp as deterministic ID; upsert semantics
- **recon-mcp:** Source + timestamp + content hash as composite key; duplicate writes
  update `last_seen` rather than creating new records
- **health-mcp:** Metric reports use upsert by metric name + time bucket
- **outreach-mcp:** `outreach_id` assigned at queue time (before delivery); delivery
  adapters must be idempotent on ID. Outreach delivery retries are conservative — a
  duplicate WhatsApp message is worse than a missed one.

See `genesis-v3-resilience-patterns.md` for full idempotency design and dead-letter
staging for failed write operations.

---

## Why 4 MCP Servers, Not 7

We considered 7 (adding user-model, tasks, salience as servers). Each was rejected against the test: "Does this need its own process, persistent state, and lifecycle?"

| Rejected Server | Why Not |
|----------------|---------|
| **user-model-mcp** | No independent state. It's a SYNTHESIS from memory-mcp queries. The LLM builds the profile inline during reflection. A server would just be a caching layer in front of memory with sync complexity. |
| **tasks-mcp** | Agent Zero has native task management. A separate MCP server duplicates framework capability. |
| **salience-mcp** | It's an LLM evaluation (a prompt), not a service. The inputs (signals + user model + engagement data) come from other servers. The output (scores) is ephemeral. No persistent state to manage. |
| **observations-mcp** | Observations are a type of memory (processed reflections). Folded into memory-mcp with a type tag. |
| **knowledge-mcp** | Knowledge is stored information with different lifecycle rules (immutable, project-scoped, no decay) but shares all infrastructure (Qdrant, embedder, FTS5, SQLite pool) and retrieval patterns (hybrid search, RRF). Needs its own collection and filter, not its own process. Folded into memory-mcp as a namespace. |

---

## Proactive Outreach: From Reactive to Anticipatory

### The Three Capabilities

**1. Triage Autonomy — "I can handle this" vs "I need the human"**

The Governance Check prompt pattern in the Reflection Engine evaluates every potential autonomous action:
- Per-category autonomy permissions (inherited from nanobot's design)
- Reversibility assessment (can this be undone without user?)
- Budget check (within allocated spend?)
- Precedent check (has user previously approved/rejected similar?)

Actions within permissions → execute silently. Actions outside → queue in outreach-mcp for user decision.

**2. Proactive Escalation — "You need to know about this"**

Triggered by Awareness Loop + Reflection Engine when signals are urgent:
- System health alerts (provider down, budget exceeded)
- Blockers (task needs user decision to proceed)
- Time-sensitive findings (recon item with expiration)

These bypass normal salience evaluation — urgency overrides.

**3. Anticipatory Intelligence — "You don't know it yet, but you need this"**

The hardest and most valuable capability. Requires:
- Rich user model (interests, skills, goals, patterns, blind spots)
- Cross-referencing new information against that model
- Social simulation (will the user find this useful?)
- Feedback loop calibration (has similar outreach been valued before?)

Example flow:
```
recon-mcp finds new AI framework gaining traction
  → Reflection Engine (Light depth):
      Salience: user builds AI tools, active project could benefit → 0.82
      User model: user evaluated similar tool 3 months ago
      Simulation: "user would likely find this useful given current project"
      Governance: cooperation-type outreach, within permissions
  → outreach-mcp:
      Channel: WhatsApp (medium urgency)
      Timing: next morning (not urgent enough to interrupt)
      Message: "Saw [framework] gaining traction — could solve the [problem]
               in your [project]. Want me to evaluate it?
               (👍 / 👎)"
  → User replies 👍
  → Self-Learning Loop: positive engagement on recon-tech topic at 0.82
      → slightly lower threshold for similar topics next time
```

### Outreach Categories

| Type | Trigger | Urgency | Channel | Example |
|------|---------|---------|---------|---------|
| **Blocker** | Task needs user decision | Immediate | Preferred push channel | "I need approval to proceed with X" |
| **Alert** | Health/budget threshold | High | Preferred push channel | "API costs 40% over weekly budget" |
| **Finding** | Recon + salience passes threshold | Medium | Learned channel or digest | "New framework relevant to your project" |
| **Insight** | Reflection Engine pattern detection | Medium-low | Next session or digest | "You've built 3 similar pipelines — template?" |
| **Opportunity** | Cross-reference: user model + new info + capability | Low | Next session or digest | "Based on your skills + goals, high-leverage idea: ..." |
| **Digest** | Scheduled batch | Low | Learned channel (Telegram/WhatsApp/email) | "Here's what happened while you were away" |

Channel selection is learned, not prescribed. The system tracks which channel gets the fastest/most-positive engagement per outreach type and gravitates toward it. User can also set explicit preferences ("alerts always go to WhatsApp, digests go to Telegram").

---

## What We Took from the AGI Spec

| AGI Spec Concept | How It Maps to Genesis | Where It Lives |
|-----------------|----------------------|----------------|
| **Drive primitives** (curiosity, competence, cooperation, preservation) | Drive weighting system that shapes Reflection Engine focus | Reflection Engine prompt + Self-Learning Loop feedback |
| **World Model / Imagination Engine** | Social simulation — "imagine user's reaction before sending outreach" | Reflection Engine prompt pattern |
| **Constitutional Subagent** | Governance check — permissions, reversibility, budget, precedent | Reflection Engine prompt pattern |
| **Procedural Memory** | Structured "how-to" records alongside episodic/semantic | memory-mcp (new memory type) |
| **Phased training curriculum** | Bootstrap sequence: observe → react → light proactive → anticipatory | Deployment phasing (see Bootstrap below) |
| **Survival Subagent** | health-mcp (software health, reactive) + Awareness Loop signal collection | MCP server + internal extension — self-maintenance, not self-monitoring |
| **Ego/Meta-Controller** | Agent Zero core + Awareness Loop + Reflection Engine | Framework + extensions |
| **Audit & Explainability** | Memory-stored retrospectives + engagement logs | memory-mcp + outreach-mcp |

**Rejected from spec:** Perception/sensorium (text-only context), RL core (LLM is the policy), simulation environments (Unity/MuJoCo irrelevant), HSM/multi-sig quorum (overkill for personal assistant), formal verification (impractical for LLM systems), training curricula (orchestrating pretrained models, not training).

---

## Bootstrap / Cold Start Strategy

### Phase 1: Observation (Weeks 1-2)
- All autonomous behavior active, outreach DISABLED
- Reflection Engine runs at all depths, builds user model from conversations
- Recon gathers findings but doesn't surface them
- Engagement tracking has no data — use conservative salience thresholds (0.9+)
- System learns normal patterns (health baselines, conversation frequency, topic distribution)

### Phase 2: Light Proactive (Weeks 3-4)
- Enable blocker + alert outreach (high-confidence, low noise risk)
- Begin digest generation (weekly summary of activity + recon)
- User model has initial shape → start finding outreach with high threshold (0.85+)
- First feedback data from engagement tracking begins calibrating

### Phase 3: Full Proactive (Month 2+)
- Lower outreach thresholds as engagement data accumulates
- Enable insight suggestions (pattern detection from user behavior)
- Drive weights have initial calibration from feedback loop

### Phase 4: Anticipatory (Month 3+)
- Enable opportunity suggestions (highest value, highest noise risk)
- Rich user model + calibrated drives + procedural memory
- System can explain WHY it's suggesting something
- Can identify things the user doesn't know they need

**Optional accelerator:** Explicit onboarding questionnaire — "What are your current goals? What topics interest you? How often do you want proactive messages? What's too noisy?" This front-loads user model data that would otherwise take weeks to observe.

### Component Bootstrap Behaviors — Day 1 With No Data

Every cognitive component must have a defined behavior when its data store is
empty. This table specifies what happens on a clean install.

| Component | Empty State Behavior | Implication |
|-----------|---------------------|-------------|
| **Awareness Loop** | Runs on calendar floors only; no event-driven triggers fire because there are no baselines to detect anomalies against | First few days are quiet — this is correct behavior, not a bug |
| **Reflection Engine** | Micro/Light depths run but produce thin outputs; Deep/Strategic have nothing to synthesize | Early reflections will be low-value; this improves rapidly as observations accumulate |
| **User Model** | Empty — no preferences, no patterns, no communication style data | Genesis defaults to conservative, explicit communication; asks rather than assumes |
| **Procedural Memory** | No procedures exist; everything is novel | Every task is a first attempt; extraction begins immediately from the first interaction |
| **Engagement Tracker** | No history — salience model has zero data points | Use conservative fixed prior (0.9+ threshold for outreach); do not surface anything speculative |
| **Outreach System** | Disabled during Phase 1 bootstrap (observation-only period) | Morning report generates but may be thin; surplus outreach waits for engagement data |
| **Signal Weights** | Seed values from schema (see `signal_weights` table); no calibration has occurred | Weights are reasonable defaults, not tuned; first calibration cycle adjusts based on early data |
| **Drive Weights** | Seed values: preservation=0.35, curiosity=0.25, cooperation=0.25, competence=0.15 | Preservation-heavy start is intentional — be cautious until competence is demonstrated |
| **Cost Tracking** | Zero events; budgets may or may not be configured | No budget warnings fire; cost accumulation begins with first LLM call |
| **Open Questions** | None stored | Curiosity layer builds naturally from first reflections that generate questions |

**Key principle:** An empty data store should produce conservative, low-risk behavior —
never aggressive defaults that assume knowledge Genesis doesn't have.

---

## Neuroscience Mapping (Thinking Tool)

This mapping informed the architecture but doesn't dictate deployment:

| Brain System | Function | Genesis Equivalent |
|-------------|---------|-------------------|
| Reticular Activating System | Filter input, decide what reaches consciousness | Awareness Loop (extension) |
| Default Mode Network | Idle processing, creativity, self-reflection | Reflection Engine at Deep/Strategic (extension) |
| Salience Network | "Is this important?" switching | Salience evaluation (prompt pattern in Reflection Engine) |
| Hippocampus | Memory encoding, consolidation, pattern completion | memory-mcp |
| Mirror Neurons / Theory of Mind | Model other minds, predict needs | User model synthesis (prompt pattern, data in memory-mcp) |
| Insular Cortex | Internal state awareness | health-mcp (software health, not hardware) |
| Anterior Cingulate | Error/conflict detection | health-mcp error pattern detection |
| Basal Ganglia | Habits, routines, learned procedures | Procedural memory (memory type in memory-mcp) |
| Dopaminergic System | Reward prediction error, learning what's valuable | Self-Learning Loop (extension) |
| Prefrontal Cortex | Executive function, planning, impulse control | Agent Zero core (LLM) |
| Amygdala | Emotional valence, urgency tagging | Part of salience evaluation |
| Cerebellum | Automated skills, practiced routines | Agent Zero instruments/tools |

---

## Comparison: Current (Nanobot) vs. Genesis v3

| Aspect | Current (6+ independent services) | Genesis v3 (3 layers + 4 MCP) |
|--------|----------------------------------|-------------------------------|
| **Scheduling** | Fixed intervals per service (2h, 30min, 5min, nightly, weekly, monthly) | Event-driven with adaptive calendar floors/ceilings |
| **Coordination** | `is_running` mutex flag (hack) | Awareness Loop is single coordinator |
| **Depth** | Fixed — dream cycle runs all 13 jobs every night | Adaptive — only runs jobs with pending work |
| **Feedback** | None — weekly doesn't influence daily behavior | Self-Learning Loop adjusts drives, thresholds, salience |
| **Proactive outreach** | None — user must check in | WhatsApp push, engagement-calibrated |
| **User modeling** | None | Synthesized from memory, used for salience + simulation |
| **Cognitive focus** | Fragmented across 6 services, health-check-heavy | User value + self-improvement dominant, health reactive-only |
| **Adding new behavior** | New service class + wiring + interval + contention management | New prompt section in Reflection Engine or new MCP tool |
| **Procedural learning** | Narratives in episodic memory | Structured procedural records, directly retrievable |
| **World model** | None | Social simulation (imagine user reaction) |
| **Governance** | Autonomy permissions (per-category flags) | Governance check (permissions + reversibility + budget + precedent) |

---

## Task Execution Architecture: The Invisible Orchestrator

### The Core Abstraction

The user says "do X." Genesis figures out everything else — invisibly. Whether X requires a simple tool call, multi-file code editing via Claude SDK, browser automation, computer use, or a combination of all four, the user never thinks about which engine did it. They just get the result.

This is NOT a new system on top of Agent Zero. It's how Agent Zero's multi-agent orchestration already works, enhanced by Genesis's cognitive layer.

> **Addendum (2026-03-07):** The orchestrator described below is now Claude Code (CC),
> not Agent Zero's main agent. CC handles task planning (Opus, high thinking), task
> execution (CC background sessions), and quality assessment. References to "Claude SDK"
> and "OpenCode" as separate engines now map to CC directly. AZ retains signal collection
> and infrastructure. Sub-agent coordination principles still apply — CC spawns sub-tasks
> following the same centralized orchestration topology. See
> `docs/plans/2026-03-07-agentic-runtime-design.md`.

### Pre-Execution Assessment

Before planning *how* to do something, Genesis considers *whether* and *what* to do.
This is the architectural implementation of Philosophy #8 ("Think about what you're
being asked to do") — the prospective counterpart to the Self-Learning Loop's
retrospective analysis.

**The assessment is an LLM judgment call, not a checklist.** Genesis engages with
the request the way a thoughtful person would — considering whether the approach
makes sense, whether the user has all the information they need, whether there's a
better way, whether the request even needs to be done at all. Most of the time, the
request is clear and correct, and Genesis proceeds immediately. The assessment adds
near-zero latency for straightforward requests.

**When the assessment matters most:**
- High-effort tasks where a wrong direction wastes significant resources
- Requests that rest on assumptions that may be wrong (and Genesis has evidence)
- Situations where relevant information exists that the user may not have
- Cases where a simpler or better path exists that the user hasn't considered
- Requests that conflict with the user's own stated goals or prior decisions

**What the assessment is NOT:**
- An interrogation — Genesis doesn't pepper the user with questions before every task
- A permission gate — that's the Governance Check's job
- A delay mechanism — trivial requests pass through instantly
- A second-guessing machine — most requests are fine and get executed

**The assessment decision space:**
- **Proceed** — Request is clear, approach is sound, execute. (The overwhelming majority)
- **Proceed with note** — Execute, but flag something the user should know. "Doing X
  as requested — note that this will also affect Y, which you may want to review after."
- **Clarify** — The request is ambiguous or underspecified in a way that could lead
  to wasted effort. Ask the minimum needed to proceed well. "You said 'optimize the
  database' — do you mean query performance, storage footprint, or both?"
- **Challenge** — Genesis has evidence that the request may be suboptimal, based on
  wrong assumptions, or in conflict with the user's goals. Surface the evidence and
  let the user decide. "Before I do X: last week you decided Y for [reason], and X
  would undo that. Want me to proceed anyway?"
- **Suggest alternative** — A better path exists. "I can do X as requested, but if
  the goal is [inferred goal], doing Z would be faster and cheaper. Preference?"

**The assessment draws on:**
- User model (communication style, expertise level, past preferences)
- Memory (similar past requests and their outcomes, relevant observations)
- Active context (current projects, recent decisions, stated goals)
- Procedural memory (known failure modes for this task type)
- Open questions (unanswered uncertainties relevant to this request — see §Open
  Questions and Persistent Curiosity)

**Pushback is philosophically mandated, not signal-dependent.** The decision to
challenge a request is driven by evidence and the philosophical commitment to
honesty (Philosophy #3), not by engagement signals. Even if the user has overridden
Genesis's pushback ten times in a row, the eleventh pushback happens if the evidence
warrants it. Individual override outcomes are weak signals (see §Signal Weight Tiers)
that should never erode the commitment to honest engagement.

**Implementation note:** This is a prompt pattern in the main agent, not a separate
component. The LLM already does this naturally when properly instructed — the
architecture just needs to ensure the instruction is present and the relevant context
(user model, memory, procedures, open questions) is available at assessment time.
The identity file (SOUL.md / vision doc loaded into context) provides the philosophical
mandate; the memory system provides the evidence.

### How Execution Flows

```
User: "Do X"
         │
         ▼
Genesis (main agent): Pre-Execution Assessment
  "Does this request make sense? Do I have what I need? Is there a better way?"
  → Most requests: proceed immediately (assessment is near-instant)
  → When warranted: clarify, challenge, or suggest alternative
         │
         ▼
Genesis (main agent): Planning pass
  "This requires capabilities: [code editing, web browsing, UI interaction]"
  "Sequence: first browse to gather info, then write code, then test in browser"
  "Memory: retrieved 2 similar past tasks, procedural memory says do A before B"
         │
         ▼
Genesis spawns subordinate agents as needed:
  ├── Sub-agent 1: browser_tool for information gathering
  ├── Sub-agent 2: claude_code tool for code writing (or opencode_fallback if rate-limited)
  └── Sub-agent 3: computer_use for UI testing
         │
         ▼
Quality gate: Did sub-agents produce correct outputs?
  "Review code against requirements"
  "Verify browser found what we needed"
  "Test results match expected behavior"
         │
         ▼
Memory integration: Store execution trace
  Episodic: what happened, what was tried, what succeeded
  Procedural: if novel approach worked, extract as reusable procedure
  Retrospective: lessons for the Self-Learning Loop
         │
         ▼
Result delivered to user (clean, no implementation details exposed)
```

### Complex Situations Genesis Must Handle

**Mid-task discovery changes the plan:**
"Write a Python API wrapper for X" → Claude SDK finds X's API is undocumented → Genesis pauses, uses browser tool to scrape docs, updates CLAUDE.md with findings → resumes Claude SDK session with new context. No user involvement needed unless discovery reveals a blocker requiring a decision.

**Mixed capability task:**
"Build and test a web scraper for Y" → Plan: browser tool to understand target structure, Claude SDK to write the scraper, computer use to run it and verify output in a terminal UI, Claude SDK again to fix any issues. Coordinated transparently.

**Tool failure mid-execution:**
Claude SDK hits rate limit during a multi-step refactor → Genesis resumes the session on Bedrock fallback → if all Claude paths fail, switches to OpenCode → stores the failure and fallback path in procedural memory so future similar tasks route better from the start.

**Blocker requiring user input:**
Task reaches a decision point that genuinely requires the user ("I found two ways to architect this, each with real trade-offs"). Genesis surfaces the decision via outreach-mcp with enough context to decide quickly, parks the task, resumes when the user responds.

**Computer use + code in sequence:**
"Update my Notion dashboard with our new metrics" → computer use to navigate Notion UI and understand current structure → Claude SDK to write a Python script that automates the update → computer use again to verify the script ran correctly. Entirely autonomous unless it hits permissions.

### What the Backend Must Ensure (Invisibly)

The "make it happen" abstraction requires these always-on behaviors behind the scenes:

1. **Governance at every capability boundary** — Before spawning a sub-agent that uses computer use or external APIs, governance check: is this action within autonomy permissions? Is it reversible? Is it within budget? If no → surface to user first.

2. **Memory continuity across sub-agents** — Each sub-agent's output is stored in memory. If a sub-agent fails and a new one is spawned to retry, the new one retrieves context from the failed attempt. No starting from scratch.

3. **Quality gates, not just completion** — "Claude SDK returned code" ≠ task done. Quality gates include BOTH programmatic checks and LLM assessment, in that order:
   - **Programmatic checks run first:** tests pass, linter clean, schema valid, API responds, file exists. These are ground truth, not judgment calls.
   - **LLM assessment runs second:** does output meet requirements, follow patterns (from procedural memory), satisfy user intent?
   If programmatic checks fail, LLM assessment is skipped entirely — no point asking "is this good?" if it doesn't compile. This prevents the LLM from rationalizing broken output as acceptable.

4. **Budget tracking per task** — Every tool call, every token, every sub-agent spawn is tracked against the task's budget. When approaching the limit, Genesis either optimizes (use cheaper models for remaining steps) or surfaces to user.

5. **Transparent audit trail** — The user can always ask "what did you do?" and get a coherent explanation. The execution trace is stored in memory-mcp as an episodic record.

6. **Graceful degradation** — If a preferred tool is unavailable, Genesis routes to the next best option. The routing chain is cost-aware: routine code → OpenCode, complex code → Claude Code subprocess (subscription) → Claude SDK API (with cost notification) → Bedrock/Vertex → OpenCode fallback.

7. **Workaround search before surrender** — When an approach fails or a capability
   appears absent, Genesis MUST attempt alternative paths before classifying the outcome
   as `external_blocker` or `capability_gap`. "I can't" is almost never the correct final
   answer — "I can't do it THIS way" is the starting point for creative problem-solving.

   **The protocol (ordered by cost, cheapest first):**
   ```
   Primary approach fails
     1. Check procedural memory: "Have I solved something similar before?"
        (FREE — just a memory lookup)
     2. Check tool registry: "Can a different tool handle this?"
        (FREE — registry query)
     3. LLM reasoning: "Given what I know about why this failed, what's a
        fundamentally different angle?"
        (CHEAP — single LLM call using current task model)
     4. Web search for workarounds: "How do humans solve this problem?"
        (MODERATE — search + parse results)
        e.g., can't fetch YouTube → search "extract youtube transcript" →
        find transcript services → use one
     5. Model escalation: If the current model lacks reasoning capacity to
        find a workaround, escalate to a more capable model.
        (EXPENSIVE — Opus-class call for creative problem-solving)
     → Each failed attempt TEACHES something about the problem — use that
       to guide the next attempt, don't just try random alternatives
     → If workaround found → execute it → store as procedural memory
     → If all alternatives exhausted → THEN classify as blocker/gap
   ```

   **Intelligent narrowing, not mechanical retries.** The workaround search is NOT
   "try 3 random things and give up." Each failed attempt reveals information about
   the problem that should guide the next attempt. If approach A failed because of
   authentication, don't try approach B that also requires authentication — use what
   A's failure taught you to pick a fundamentally different angle. Three variations
   of the same bad idea is ONE strategy explored, not three.

   **What counts as genuinely different strategies:** HTTP fetch vs. browser render
   vs. find a mirror vs. use an API vs. find a converter service. NOT: requests vs.
   httpx vs. aiohttp (those are tool variations of the same strategy — try ONE,
   if it fails for a tool-independent reason, the others will too).

   **Model escalation for workaround search:** The model running the sub-agent may
   lack the reasoning capacity to find creative workarounds. A sub-agent running on
   Haiku hitting a wall should not burn 3 Haiku-level attempts on something that
   requires Opus-level reasoning. The escalation path:
   - Let the current model try ONE workaround strategy (cheap)
   - If that fails, escalate to a more capable model for the workaround SEARCH
     (not for the whole task — just for the "find an alternative path" reasoning)
   - The capable model analyzes: what was tried, why it failed, what tools are
     available, what indirect paths exist
   - If the task warrants it and the user's cost preferences allow, a second model
     can review workaround attempts (same cross-model review principle as quality
     gates — a fresh perspective catches blind spots)

   **Workaround search budget:** Default maximum of 20% of the task's total budget
   for workaround exploration, with a minimum floor of ~$0.10 (enough for at least
   one meaningful LLM-assisted search attempt). The floor matters: a $0.10 task's
   20% ceiling ($0.02) isn't enough for a single LLM call — without a floor, trivial
   tasks get no real workaround search. The ceiling prevents spending $10 on
   workarounds for a $5 task. Both are defaults — the user can adjust per-task or
   globally based on their tolerance for "try harder" vs. "save money." Some users
   want Genesis to exhaust every option; others want it to fail fast and ask.

   **What the search itself teaches (even when it fails):** A failed workaround
   search is still learning data. Even if no workaround was found, the search
   revealed: what tools DON'T work for this problem, what the actual constraints
   are (not just the apparent ones), and what the problem space looks like. This
   meta-data feeds into the `capability_gap` or `external_blocker` classification
   with richer context than "I tried and failed."

   **Successful workarounds are high-value procedural memory.** A workaround that
   works becomes the primary approach for future identical tasks. The originally-
   blocked path becomes the *fallback*. This inverts the failure into an improvement
   — next time, Genesis is BETTER at this task than if the primary approach had
   simply worked.

   **Design principle:** If a human user would find a workaround within 5 minutes of
   searching, Genesis should find it too. The bar is not "can the primary tool do
   this?" — the bar is "can Genesis make this happen, period?" Every "no" is a
   challenge to think through, not a terminal state.

### Multi-Agent Coordination Principles

Genesis is a centralized multi-agent system: a main orchestrating agent, sub-agents
spawned per task, background workers (DeferredTask), and surplus task runners. This
is an agent swarm with a coordinator, and the following principles govern it.

These principles are informed by Google/MIT research on multi-agent scaling (2026)
which demonstrated three dominant effects: tool-coordination trade-offs, capability
saturation, and topology-dependent error amplification.

**1. Centralized orchestration is the default topology.**

The main agent is the sole orchestrator. Sub-agents report results back to it — they
do not communicate peer-to-peer. This reduces error amplification (a sub-agent failure
surfaces to the coordinator, not cascading to sibling agents) and simplifies governance
(one point of control for permissions, budgets, and quality gates).

The Awareness Loop is the sole scheduling authority. Background tasks run via
DeferredTask threads, but what runs, when, and at what priority is decided centrally.

**2. Scope sub-agent tools aggressively.**

The `permitted_tools` array (§Governance at Capability Boundaries) already restricts
what each sub-agent can do. This also reduces tool-coordination overhead — a sub-agent
with 3 permitted tools selects faster and more accurately than one with 25+.

At the agent level: when the main agent builds its tool list for a given monologue
iteration, the awareness briefing extension should signal which MCP servers are
relevant to the current context. Tools from irrelevant servers can be deprioritized
or hidden from the LLM prompt to reduce selection confusion.

**3. Don't over-parallelize.**

Research shows diminishing returns from adding agents beyond a capability threshold.
Genesis should prefer sequential sub-agents (pipeline) over parallel sub-agents
(scatter-gather) unless the task has genuinely independent subtasks. Concretely:

- **Sequential** (default): research → plan → code → test → deliver. Each step
  informs the next. This is most tasks.
- **Parallel** (when warranted): multiple independent research queries, running
  tests across different environments, fetching data from unrelated sources.
- **Limit**: V3 caps concurrent sub-agents at 3 per task. This is a conservative
  default. V4 can tune based on observed task outcomes.

**4. Background tasks must not starve user-facing work.**

When surplus workers and background reflection run alongside a user conversation,
MCP server contention is real. Priority ordering:

1. User-facing conversation (main agent) — always takes priority
2. Active task sub-agents — spawned by user request
3. Awareness Loop tick — lightweight, non-blocking
4. Reflection and observation extraction — DeferredTask, can wait
5. Surplus tasks — lowest priority, yield to everything else

If a surplus task and a user request both need `memory-mcp`, the surplus task
backs off. This is enforced by the task queue priority model, not by MCP-level
locking.

**5. Error containment, not propagation.**

Sub-agent failures are contained — they return an error result to the orchestrator,
which decides whether to retry, escalate, or fail the task. Sub-agents do NOT:
- Retry autonomously (the orchestrator decides retry strategy)
- Spawn their own sub-agents to work around failures (depth-limited)
- Modify shared state to signal siblings (no peer-to-peer)

This keeps the orchestrator as the single point of truth about task state.

**6. V4+: Task-dependent topology selection.**

Research shows some tasks benefit from distributed approaches (web navigation,
parallel research). V4 may introduce topology selection as a planning-pass
decision: "This task has independent subtasks — use parallel topology" vs. "This
task is sequential — use pipeline topology." This requires operational data from
V3 to calibrate which tasks actually benefit from parallelism.

### Execution Trace Schema

Every task execution is stored in memory-mcp as an episodic record:

```json
{
  "task_id": "task_abc123",
  "initiated_by": "user",
  "user_request": "Build and test a web scraper for Y",
  "plan": ["browser: understand target structure", "claude_code: write scraper", "computer_use: verify output"],
  "sub_agents": [
    {"type": "browser", "input": "...", "output": "...", "status": "success", "cost_usd": 0.02, "duration_s": 45},
    {"type": "claude_code", "input": "...", "output": "...", "status": "success", "cost_usd": 0.34, "session_id": "sess_xyz"},
    {"type": "computer_use", "input": "...", "output": "...", "status": "failed", "error": "permission denied"}
  ],
  "quality_gate": {"passed": false, "reason": "computer_use failed — permission issue", "action": "surfaced to user"},
  "total_cost_usd": 0.36,
  "procedural_extractions": ["proc_def456"],
  "retrospective_id": "retro_ghi789",
  "request_delivery_delta": {
    "original_request": "Build and test a web scraper for Y",
    "discoveries": ["Site Y requires enterprise authentication"],
    "adjusted_scope": "Build scraper for public endpoints + auth integration guide",
    "scope_communicated": true,
    "delta": "exact_match",
    "attribution": ["external_limitation"]
  }
}
```

**`initiated_by` field:** Distinguishes user-triggered tasks from autonomous ones. Values:
`"user"` (direct request), `"awareness_loop"` (signal-triggered), `"surplus"` (idle-cycle
task), `"reflection"` (reflection-proposed action). This distinction matters for the
Self-Learning Loop — autonomous tasks that go unused teach Genesis to be less proactive;
user-triggered tasks that go well teach Genesis what the user values.

**`request_delivery_delta` field:** Captures the full request→discovery→delivery chain.
See Self-Learning Loop → After Every Interaction → step 3 for semantics of scope
evolution, delta classification, and discovery attribution.

### Governance at Capability Boundaries

Before spawning any sub-agent, a programmatic check runs (not LLM — too slow for every spawn):

1. Check autonomy permissions for the capability type (code_edit, browser, computer_use, external_api)
2. Check budget: cumulative task cost + estimated sub-agent cost < task budget
3. Check reversibility flag on the capability type (code_edit = reversible via git, computer_use = NOT reversible, external_api = depends on endpoint)
4. **Check cost tier of the engine being invoked** (see below)

If all pass → spawn. If budget fails → downgrade model or surface to user. If reversibility fails on a non-approved capability → surface to user.

5. **Output `permitted_tools` per sub-agent** — The governance check outputs an explicit `permitted_tools` array with each spawn approval. The sub-agent ONLY has access to tools in that array. This is a programmatic constraint, not a prompt instruction.

```json
{
  "sub_agent_id": "sa_abc123",
  "task": "write unit tests for auth module",
  "permitted_tools": ["read_file", "write_file", "run_tests"],
  "denied_tools": ["computer_use", "outreach_send", "memory_write"],
  "reason": "test-writing task needs filesystem + test runner only"
}
```

This prevents lateral capability creep — a code-writing sub-agent cannot accidentally trigger outreach, and a research sub-agent cannot write to persistent memory. Defense in depth: the prompt says what to do, the `permitted_tools` array enforces what is possible.

The LLM-based governance check (permissions + precedent + social simulation) runs only for OUTREACH and STRATEGIC decisions, not for every sub-agent spawn.

### Critical File Protection

Certain files (SOUL.md, autonomy config, governance rules, identity files) have programmatic write-protection via pre-tool-use hooks that cannot be overridden by prompt instructions:

- **L1-L5 autonomy:** BLOCKED from modifying protected files. No prompt can override.
- **L6 (learning system):** Can PROPOSE modifications, stored in a staging area for user review.
- **L7 (identity):** Can DRAFT modifications, requires explicit user approval before any write.

This is defense in depth: the autonomy hierarchy says "don't modify identity files," the hook ENFORCES it regardless of what the prompt says. Prompt-based restrictions fail when the LLM hallucinates permissions or is manipulated. Programmatic hooks don't.

Protected file list (configurable): `SOUL.md`, `USER.md`, autonomy permission config, governance rules, drive weight bounds, MCP server configurations.

### Cost-Conscious Engine Selection

Claude Agent SDK bills at API rates (~$15/$75 per MTok for Opus), not subscription
rates. This makes engine selection a cost governance concern, not just a capability
routing decision.

**Routing logic for code tasks:**

| Task Complexity | Primary Engine | Fallback | User Notification |
|----------------|---------------|----------|-------------------|
| Routine (add function, fix bug, write tests) | OpenCode (cost-efficient model) | Agent Zero LiteLLM | None — within normal budget |
| Complex (multi-file refactor, architecture) | Claude SDK API (with cost estimate) | Claude Code subprocess (optional) | "This task may cost $X via Claude SDK. Proceed?" |
| Rate-limited | OpenCode (best available model) | — | "Claude unavailable, using OpenCode with [model]" |

**Cost estimation before invocation:**
Before spawning a Claude SDK sub-agent, Genesis estimates cost based on:
- Number of files likely to be read (from task plan)
- Estimated conversation turns (from procedural memory of similar tasks)
- Model tier (Opus vs. Sonnet)
- Historical cost of similar tasks (from execution traces in memory-mcp)

This estimate is surfaced to the user as part of task confirmation:
"This task will use Claude SDK (Opus) for multi-file refactoring. Estimated cost:
$5-8. Approve / Use cheaper model / Cancel"

**Claude Code subprocess (experimental):**
Agent Zero can attempt to invoke the `claude` CLI binary as a subprocess, which
uses the user's subscription OAuth rather than API billing. This is running
Anthropic's own product, not the SDK. Risks: Anthropic could restrict automated
invocation; the CLI's interactive nature may not map cleanly to Agent Zero's tool
interface. Use this path as an optional mode based on user/operator preference.
If it is unavailable or not desired, route through SDK with cost notification.

**Per-engine budget tracking:**
Execution traces (see schema above) track `cost_usd` per sub-agent. Aggregate
reporting by engine type (Agent Zero LiteLLM, Claude SDK, OpenCode, CLI subprocess)
enables the user to see where money goes and adjust routing preferences.

### Quality Gate

After each sub-agent returns, a lightweight check (utility model, cheap):
- Code sub-agents: "Does this code run? Does it address the stated requirement?"
- Browser sub-agents: "Did we get the information we needed?"
- Computer use: "Did the UI reach the expected state?"

Failure → retry with error context (max 2 retries) → if still failing, surface to user.
Success → proceed to next sub-agent or deliver result.

---

## Memory Separation: Conversational vs. Task-System

A design question the dual-engine architecture introduces: when Genesis is executing a task using Claude SDK as a tool, there are potentially two memory systems active (Genesis's memory-mcp and Claude SDK's in-session context). And there are two types of learnings accumulating: conversational (what the user likes, their communication style) and task-execution (how to approach certain types of code problems).

### The Separation

**Conversational memory** (feeds user model, drives proactive behavior):
- User preferences and communication style
- Interests, goals, patterns, frustrations
- Feedback on proactive outreach
- High-level project awareness ("user is building Genesis on Agent Zero")

**Task-execution memory** (feeds procedural memory, improves task quality):
- How to approach specific task types (code architecture, debugging patterns)
- Which tools work best for which sub-problems
- Failure modes and their resolutions
- Quality patterns from past successful outputs

Both live in memory-mcp, tagged differently. The distinction matters for retrieval:
- When assembling context for a conversation → weight conversational memory higher
- When assembling context for task planning → weight task-execution memory higher
- When assembling context for heartbeat/reflection → both equally

### Claude SDK Session Memory vs. Genesis Memory

Claude SDK sessions have their own deep context (file-level understanding of a codebase). This is ephemeral within a session but resumable via session IDs.

**The handshake:** Genesis owns the persistent layer. Claude SDK owns the deep-context layer for the duration of a task. Before invoking Claude SDK, Genesis writes a dynamic CLAUDE.md populated with relevant Genesis memories (past retrospectives, known patterns, procedural memory about this codebase). After the session, Genesis extracts learnings back into memory-mcp.

**Per-task isolation:** Agent Zero can spawn concurrent subordinates. If two tasks both invoke Claude SDK, their CLAUDE.md contexts must be isolated — otherwise the second write overwrites the first and the first session reads wrong context. Implementation: per-task CLAUDE.md files (e.g., `CLAUDE-{task_id}.md`) or per-sub-agent working directories. The handshake cycle (write → invoke → extract) is per-task, not global.

**What doesn't cross into Claude SDK by default:** User model data, salience weights, outreach preferences, system health data. Claude SDK is a specialist tool — it needs task context, not cognitive system state. Exception: if the task itself involves these systems (e.g., "fix the outreach timing"), relevant data is included in the dynamic CLAUDE.md.

---

## Self-Evolving Learning: The Autonomy Hierarchy

### The Core Goal

Genesis should learn not just FROM outcomes but HOW to learn better — adjusting its own review schedules, calibrating its own salience thresholds, proposing changes to its own cognitive parameters. This is what the ICML 2025 paper calls "intrinsic metacognition" vs. the "extrinsic metacognition" (fixed human-designed loops) that most systems fake.

But self-modification of learning systems has the **highest catastrophic potential**. A miscalibrated salience threshold is annoying. A miscalibrated learning system causes systematic drift away from user values — and does it invisibly, over time.

### The Autonomy Hierarchy

Actions are stratified by their blast radius and reversibility. Autonomy grows with demonstrated trustworthiness over time — not on a calendar, but based on evidence.

| Level | Action type | Default | Grows to |
|-------|------------|---------|----------|
| **L1** | Simple tool use (search, read, compute) | Fully autonomous | No change needed |
| **L2** | Task execution with known patterns | Mostly autonomous | Can reduce check-ins as confidence builds |
| **L3** | Novel task execution | Propose + execute with checkpoint | Can execute autonomously after N similar successes |
| **L4** | Proactive outreach | Threshold-gated + governance check | Threshold lowers as engagement data accumulates |
| **L5** | System configuration (thresholds, weights) | Propose only, user approves | Can self-adjust bounded parameters after high confidence |
| **L6** | Learning system modification (review schedules, drive weights, salience calibration) | Propose only, **always user review** | Bounded self-adjustment possible, but fundamental changes always user-approved |
| **L7** | Identity evolution (SOUL.md changes) | Draft only, user decides | Never fully autonomous — identity is the user's call |

**Key principle:** Autonomy for L5/L6 is bounded. The system can adjust salience weights ±20% within a session without approval, but can't fundamentally restructure how salience works. It can propose to change the review schedule but can't implement that change without user approval.

**The evidence threshold:** To unlock more autonomy at a level, the system needs:
- N successful executions without user correction
- No negative engagement signals indicating systematic error
- At least M weeks of operation at that level
- User explicitly acknowledging the autonomy grant (not just absence of correction)

The last point matters. Silence ≠ approval. The system should periodically ask: "I've been handling [category] autonomously for [period] with [X% success rate]. Would you like me to continue, or do you want to adjust my autonomy for this?"

### Autonomy Regression

Trust is harder to build than to lose. Regression triggers:
- 2 consecutive user corrections at a level → drop one level, require re-earning
- 1 user-reported harmful action → drop to default for that category, full re-earn
- System detects its own systematic error (e.g., 5 ignored outreach in a row at a topic) → self-proposes regression for that category

Regression is announced: "I've been making mistakes with [category]. Dropping back to [level] until I rebuild confidence. You can override this."

### Context-Dependent Trust Ceiling

Earned autonomy has a ceiling imposed by the interaction context. Regardless of
the system's earned level for a capability category, the *channel or invocation
context* can cap the effective autonomy:

| Context | Max Effective Autonomy | Rationale |
|---------|----------------------|-----------|
| Direct user session (WhatsApp, Web UI) | Earned level (no cap) | User is present, can intervene |
| Background cognitive (Reflection Engine) | L3 (notify + act) | No user in the loop — keep actions reversible |
| Sub-agent spawned by task | L2 (act with confirmation) for irreversible; earned for reversible | Sub-agents inherit task permissions, not global permissions |
| Outreach (proactive messaging) | L2 (act with confirmation) until engagement data proves calibration | Wrong outreach erodes trust faster than wrong internal action |

The principle: **later contexts restrict but never expand** effective autonomy.
A system with L5 earned autonomy for code operations still caps at L2 when a
sub-agent attempts an irreversible action inside a background task with no user
present. This prevents earned trust in supervised contexts from being exploited
in unsupervised ones.

### What "Learns HOW to Learn" Actually Means in Practice

At L6, the system can observe that its own learning is working or not:

- "My Deep reflections have been producing no actionable observations for 3 weeks. Either the threshold for what triggers Deep reflection is too high, or the quality of the reflection itself is poor. Proposing: lower Deep trigger threshold from composite score 0.7 to 0.6, trial for 2 weeks."

- "I've been recalibrating salience weights after every outreach event, but this is causing oscillation — my threshold for 'architecture insights' has swung between 0.6 and 0.9 in the past month. Proposing: introduce a damping factor to smooth weight updates."

- "The Strategic reflection is generating useful findings but they're not influencing my Light reflections at all. Proposing: add a step to Light reflections that checks for pending Strategic recommendations before completing."

These are proposals — concrete, evidence-based, with a stated rationale — surfaced via outreach for user review. The user can approve, reject, or modify. Over time, if proposals are consistently approved, some bounded class of them (e.g., ±threshold adjustments) might become auto-approved. But never the structural ones.

**Measuring learning value:** The meta-learning loop must measure **downstream utility**, not output volume. "Did this reflection produce observations?" is the wrong metric — it optimizes for busywork. The right metric: "Were the outputs of this reflection USED? Did an observation get retrieved by a subsequent reflection or task? Did an outreach item get acted on? Did a configuration change produce measurably different behavior?" Observations that accumulate without being retrieved or acted upon are evidence of waste, regardless of quantity. Track observation utility (retrieved, influenced a decision, acted on) not just creation count.

---

## What We Learned from the Research Landscape

### Where Genesis is Ahead of the Field

- **Integrated periodic cognition:** The dream cycle / heartbeat / health check layered architecture is more sophisticated than anything found in production. Most systems have one cognitive loop; Genesis has multiple at different frequencies with coordination.
- **Identity persistence:** SOUL.md + user model + memory-mcp is a richer identity substrate than most systems.
- **Recon system:** No other system has an equivalent autonomous environmental scanning layer that actively gathers intelligence on a schedule. ChatGPT Pulse curates topics from user signals; Genesis also generates its own intelligence.

### Where Genesis Has Gaps (and the Fixes)

**Gap: No explicit user feedback loop on proactive outputs**
ChatGPT Pulse's thumbs-up/down on morning briefing cards is simple and effective.
→ **Fix:** outreach-mcp tracks engagement with inline feedback (WhatsApp: reply-based, Telegram: reactions). The Self-Learning Loop uses this as primary training signal for salience calibration.

**Gap: Append-only memory accumulation**
Genesis memories accumulate and are periodically cleaned up. More dynamic: new memories should update related existing ones.
→ **Fix (from A-MEM):** When storing new observations, run a lightweight pass to find related existing memories and link them (or update confidence/context if the new info changes the old). The dream cycle does a heavier version of this during consolidation, but the lightweight linking should happen at storage time.
→ **Implementation note:** Memory links need a data model. Options: (a) a `memory_links` join table (source_id, target_id, link_type, strength), (b) a JSON `related_ids` array on each memory record, (c) Qdrant's payload metadata for implicit linking. The join table is most flexible (supports link types like "supports," "contradicts," "extends") but adds query complexity. The `connectivity_factor` in ACT-R activation scoring (see below) depends on this — it counts links, so the model must support counting. Decision: resolve during Phase 5 implementation.

**Gap: Flat memory retrieval (similarity search only)**
Embedding similarity is good but misses activation patterns — frequently-accessed important memories should be easier to retrieve than rarely-accessed ones.
→ **Fix (from ACT-R + Mnemosyne):** Add an activation score to memories: `activation = base_score * recency_factor * access_frequency_factor * connectivity_factor`. Retrieval weights activation alongside embedding similarity. Memories that are accessed often, recently, and are well-connected stay highly accessible even if they're semantically distant from the query.

**Gap: Context window assembly is ad hoc**
When assembling the heartbeat prompt or task context, what gets included is mostly intuitive.
→ **Fix (from Engram):** Salience-ranked greedy knapsack for context assembly. Each candidate memory/signal has a salience score. Fill the context window budget greedily from highest-salience down. This makes context assembly principled and optimizable.

**Gap: Dual-context separation before proactive decisions**
The decision "should I reach out?" currently mixes situation signals and user model signals without formal separation.
→ **Fix (from ContextAgent):** Formalize two evaluation passes before proactive decisions: (1) situation assessment — "what is happening and how important is it?" (2) persona assessment — "given who this user is, would they want to know this now?" Both pass through the Reflection Engine, but as distinct steps with distinct context.

**Gap: Proactive suggestion noise floor is unknown**
Without data, we don't know how often proactive suggestions will be welcome.
→ **Calibration from ProactiveBench:** Even trained models get proactive suggestions right ~66% of the time. Meaning 1 in 3 will be unwelcome even with a good user model. Set expectations accordingly. The engagement feedback loop should improve this over time, but never expect 95%+ precision.

**Gap: No impasse-driven learning**
Genesis learns from successes and explicit lessons. It doesn't systematically learn from failures and dead ends.
→ **Fix (from SOAR):** When tasks fail or produce poor output, log the failure as an explicit learning event — what was attempted, what failed, what the failure mode was. These "impasse records" feed the Self-Learning Loop and the procedural memory deprecation mechanism.

**Warning: Don't over-scaffold**
Letta deprecated heartbeats in V1 because modern models work better without framework-imposed patterns. Genesis's heartbeat is scheduled background cognition (different from in-conversation reasoning), so the direct comparison doesn't apply. But the principle stands: as models improve, periodically audit whether Genesis's scaffolding is still adding value or has become overhead.

**Warning: Proactive AI is perceived as threatening**
CHI 2025 study: unsolicited AI help is perceived as MORE threatening than unsolicited human help. Tone, timing, and opt-out mechanisms are not cosmetic — they directly affect adoption. Genesis must frame proactive outreach as assistance, not surveillance. And the opt-out path must be effortless ("Reply STOP to pause proactive updates").

### AutoGPT: Historical Context

AutoGPT (2023) pioneered autonomous LLM task loops but is now largely a historical reference point. The lessons it taught:
- **What it proved:** LLMs can chain tasks autonomously
- **What it got wrong:** Unbounded loops, no cost controls, no quality gates, no human checkpoints
- **Current state:** Still active as a cloud platform with human-in-the-loop features bolted on
- **Relevance to Genesis:** Confirms what NOT to do. Genesis's governance checks, bounded autonomy, and budget controls are direct corrections to AutoGPT's failure modes.

---

## Procedural Memory Design

*(Confidence decay mechanics are deferred — complex guardrail issues to revisit separately.)*

### Foundational Principle: Intelligence First, Memory Second

**The LLM is the intelligence. Memory is the shortcut.**

Genesis's reasoning comes from the LLM evaluating the current situation with its full
capabilities — context understanding, analogy, creative problem-solving. Procedural memory
provides shortcuts: "last time you did something like this, here's what worked and what
didn't." The shortcut saves time and compute. But the LLM ALWAYS reasons about whether the
shortcut applies to the current situation. Memory never overrides reasoning.

This means:
- A procedure with 0% success rate doesn't mean "never try this." It means "this failed
  every time it was tried under these specific conditions." If the current conditions are
  different, the LLM should consider trying it anyway.
- A procedure with 100% success rate doesn't mean "always do this." It means "this worked
  every time it was tried so far." If the current situation has characteristics that none
  of the previous successes had, the LLM should evaluate whether the procedure still
  applies.
- Memory provides *evidence*, not *rules*. Evidence can be outweighed by reasoning about
  the current situation. Rules cannot.

**Why this matters for self-improvement:** If memory acts as gospel, Genesis converges on
a fixed set of behaviors and stops growing. If memory acts as evidence that the LLM
evaluates fresh each time, Genesis can revise its approach when circumstances change — even
when the stored data says otherwise. The intelligence is in the reasoning, not the recall.

**Scalability caveat:** At 1000+ procedures, the LLM can't reason about every one. The
context-conditional retrieval system (see Anti-Rigidity below) pre-filters algorithmically
by embedding similarity and context tag overlap, narrowing to 3-5 relevant candidates
before the LLM reasons about them. This filtering is a practical necessity, not a violation
of the principle. **The filter decides what evidence the LLM SEES; the LLM decides what
to DO with it.** The principle governs the LLM's reasoning over presented evidence — it
doesn't say "never filter, show everything." Bad filtering (showing irrelevant procedures
while hiding relevant ones) is the real failure mode, not filtering itself.

### Schema

```json
{
  "id": "proc_abc123",
  "task_type": "data_pipeline_construction",
  "principle": "Validate data schema before transformation, not after",
  "steps": [
    "Inspect source data schema",
    "Define expected output schema",
    "Write validation function",
    "Then write transformation logic"
  ],
  "tools_used": ["python", "pandas", "pytest"],
  "context_tags": ["python", "data", "ETL", "pandas"],
  "success_count": 4,
  "failure_count": 1,
  "failure_modes": [
    {
      "description": "Fails when source schema is dynamic/undeclared",
      "conditions": {"source_type": "untyped API", "schema_available": false},
      "times_hit": 1,
      "last_hit": "2026-02-18T10:00:00",
      "transient": false
    }
  ],
  "attempted_workarounds": [
    {
      "description": "Infer schema from sample data before validation",
      "outcome": "partial_success",
      "conditions": "Worked when sample data was representative; failed on sparse samples",
      "stored_as_procedure": "proc_def456"
    }
  ],
  "confidence": 0.78,
  "last_used": "2026-02-20T14:22:00",
  "last_validated": "2026-02-20T14:22:00",
  "deprecated": false,
  "deprecated_reason": null,
  "superseded_by": null
}
```

### Anti-Rigidity Mechanisms

Procedures are **advisory context, never imperative instructions** (see Foundational
Principle above). The LLM always sees them framed as:

> "Previous approach for [task type] (success rate 78%, last used 3 days ago):
> Principle: [principle]
> Steps: [steps]
> Known failure conditions: [specific conditions under which this failed]
> Previously attempted workarounds: [what was tried when this failed, and what happened]
>
> Evaluate: Do current circumstances match any known failure conditions? Are there
> factors in this situation that none of the previous attempts accounted for?
> Consider whether this approach, a variation, a known workaround, or something
> entirely different is warranted."

The retrieval framing is critical: **failure conditions and workaround history must be
surfaced alongside success data.** If the prompt only shows "confidence: 0.78" without
the conditions that drove the failures and what was tried, the LLM can't reason about
applicability. The nuance in storage is worthless if the nuance is stripped at retrieval.

**Failure tagging on the procedure:** When a procedure is followed and the task fails,
the failure is recorded on that procedure record WITH the specific conditions of failure
— not just "it failed" but "it failed WHEN [conditions]" and "it was BECAUSE [reason]."
This enables the LLM to distinguish "this procedure is broadly unreliable" from "this
procedure fails specifically when condition X is true." The `transient` flag on failure
modes marks conditions that may not persist (service was down, rate limit hit, temporary
permission issue) — transient failures should NOT reduce confidence in the procedure
itself.

After N failures ACROSS DIFFERENT conditions, the procedure is flagged for deprecation
during the next Deep reflection. The LLM reviews it: deprecate outright, update steps,
or add a context restriction ("only applies when X, not when Y"). Failures concentrated
under a single condition are context restrictions, not deprecation signals.

**Failed workaround storage:** When a workaround is attempted and fails, it's stored in
the `attempted_workarounds` array of the parent procedure with its specific failure
conditions. This is NOT a "never try this" signal — it's "this didn't work WHEN
[conditions]." The LLM evaluates whether current conditions match before ruling out a
previously-failed workaround. A workaround that failed because the target service was
down should absolutely be re-attempted when the service is back up.

**Dual-level storage (from Mem^p):** Store both the specific steps AND the higher-level principle. The principle ("validate before transforming") is more durable than the steps ("run pandas.DataFrame.describe() first"). When specific steps become outdated, the principle may still apply with different steps.

**Context-conditional retrieval:** Procedures are only retrieved when their context_tags overlap meaningfully with the current task. A procedure that worked for Python data pipelines isn't surfaced for a Rust CLI tool even if the task_type superficially matches.

**Novelty scoring:** When storing new procedures, compute a novelty score alongside confidence and success metrics. Novelty = embedding distance of the new procedure from the centroid of existing procedures in the same domain. Without novelty scoring, procedural memory converges on a narrow set of "safe" procedures — mode collapse (Weakness #4) applied to the learning system.

Procedure selection balances performance and novelty: ~90% of the time, use the highest-confidence procedure for the task type. ~10% of the time, deliberately try a novel (lower-confidence, higher-novelty) procedure and log the comparison. This explore-exploit ratio ensures the system doesn't stop discovering better approaches for tasks it already handles adequately.

**Speculative tagging for new procedures:** Pattern 3 (speculative vs. grounded claims) applies to procedures, not just observations. A procedure extracted from a single successful task execution is speculative — it worked once, but is it generalizable?

- New procedures start with `speculative: true`, `success_count: 1`
- Confirmed after `success_count >= 3` across different task contexts (not 3 successes on the same task — 3 successes in different situations)
- Speculative procedures are available for use but do NOT override confirmed procedures for the same task type
- Speculative procedures that fail 3 times before reaching confirmation are archived, not
  deleted (the failure data is valuable). Archived procedures remain accessible for
  re-evaluation — if conditions change (new tools available, service restored, different
  context), the LLM can re-consider an archived procedure. Archived ≠ forbidden.

This prevents one lucky execution from installing a fragile procedure as the default
approach, while ensuring that failed experiments aren't permanently lost. The failure
conditions stored on archived procedures are learning data — "we tried this, it didn't
work because X" is valuable context for future attempts at similar problems, even if the
specific procedure isn't retried.

*(Note: Confidence decay over time with guardrails against amnesia — deferred to separate design session.)*

---

## Awareness Loop: Signal-Weighted Trigger System

"Triggers when warranted" is not sufficient. The concrete mechanism:

### Composite Urgency Score

Every 5 minutes, for each depth level:

```
urgency_score(depth) = Σ(signal_value_i × weight_i) × time_multiplier(depth)
```

Where `time_multiplier` rises as time passes since last reflection at that depth:

```
Micro time_multiplier:
  At 0min since last Micro:  0.3x  (just reflected)
  At 15min:                  0.7x
  At 30min (floor):          1.0x  (baseline)
  At 45min:                  1.5x
  At 60min:                  2.5x  (overdue)

At 0h since last Light:   0.5x  (suppressed — just reflected)
At 3h:                    1.0x  (baseline)
At 6h (floor):            1.5x  (heightened)
At 8h:                    2.0x  (approaching overdue)
At 12h:                   3.0x  (something is wrong if this is reached)

Deep time_multiplier:
  At 0h since last Deep:    0.3x  (heavily suppressed)
  At 24h:                   0.7x
  At 48h (floor start):     1.0x  (baseline)
  At 72h (floor end):       1.5x  (heightened)
  At 96h:                   2.5x  (overdue)

Strategic time_multiplier:
  At 0d since last Strategic:  0.2x  (heavily suppressed)
  At 3d:                       0.5x
  At 7d (floor start):         1.0x  (baseline)
  At 10d:                      1.5x
  At 14d (floor end):          2.0x  (heightened)
  At 21d:                      3.0x  (overdue)
```

When `urgency_score ≥ threshold_for_depth` → trigger that depth. Calendar floor is implicit in the rising multiplier — even weak signals eventually cross the threshold.

### Signal Sources and Initial Weights

| Signal | Source | Initial Weight | Feeds | Rationale |
|--------|--------|---------------|-------|-----------|
| Conversations since last reflection | Agent Zero | 0.40 | Micro/Light | User interaction is the primary input — the thing we're here for |
| Task completion / quality signal | Agent Zero | 0.50 | Micro/Light | "Am I actually helping? Am I getting better?" |
| Outreach engagement data | outreach-mcp | 0.45 | Micro/Deep | "Are my proactive suggestions landing?" — core learning signal |
| Recon findings pending triage | recon-mcp | 0.35 | Light/Deep | New intelligence that might help the user |
| Unprocessed memory backlog | memory-mcp | 0.30 | Deep | Learning debt — things experienced but not consolidated |
| Budget % consumed since check | health-mcp | 0.40 | Light/Deep | Cost awareness, but not the main event |
| Software error spike | health-mcp | 0.70 | Micro/Light | Something broke — fix it. But only fires when errors actually happen. |
| Critical failure (crash, dead API) | health-mcp | 0.90 | Light (immediate) | Reactive — fire fast, fix fast, move on |
| Time since last Strategic | Clock | 0.50 | Strategic | Ensure periodic big-picture thinking happens |

**Design intent:** The top 4 signals by weight are all about **user value and self-improvement**. Health signals are reactive (high weight but only fire when something is actually wrong). Genesis should spend 80%+ of its cognitive budget thinking about how to be more useful, not whether its processes are running.

### How Weights Adapt

The Self-Learning Loop tracks: "When signal X triggered a reflection, did that reflection produce value?" Value = observations written, outreach sent, procedures updated, or configuration changed.

- Signal consistently triggers reflections that produce nothing → weight drops
- Signal consistently triggers reflections that produce actions → weight rises
- Strategic reflection can temporarily override weights ("this week, raise error weight because we're deploying")

### Ceiling: Preventing Thrashing

A per-depth boolean resets after each reflection: "Has this depth fired within its minimum interval?" If yes, accumulate score but don't trigger. Three budget alerts in one hour shouldn't produce three Deep reflections — they should be batched into one.

---

## Loop Taxonomy: Complete Feedback Cycle Inventory

This section maps every feedback cycle in the Genesis v3 architecture — autonomous processes, integrated operational cycles, calibration feedback loops, and emergent spirals that arise from their interaction. The taxonomy serves two purposes: (1) a mental model for reasoning about how the system improves over time, and (2) a checklist ensuring no feedback cycle falls through the cracks during implementation.

### How to Read This Map

Loops are organized by **what drives them**, not by implementation type. Each tier depends on the tier below it:

- **Tier 0** — The metronome. Ticks autonomously. Everything else is downstream.
- **Tier 1** — The cognitive engines. Triggered by Tier 0.
- **Tier 2** — Operational cycles. Driven by events, user actions, or Tier 1.
- **Tier 3** — Calibration loops. Feedback cycles embedded in Tiers 1-2 that tune the system.
- **Tier 4** — Emergent spirals. No dedicated code — they arise from the interaction of everything above.

**Tiers 0-2 are what the system DOES. Tiers 3-4 are how the system IMPROVES at what it does.** Most AI systems only have Tiers 0-2. The calibration and emergent layers are what make Genesis a learning system rather than just an executing system.

```
Tier 4 (Emergent)     ┌─────────────────────────────────────────────┐
                      │  User Model    Identity    Meta-Learning     │
                      │  Deepening     Evolution   Loop              │
                      │  Spiral        Spiral      ("learn to learn")│
                      │                                              │
                      │  Capability                                  │
                      │  Expansion                                   │
                      └──────────────────┬──────────────────────────┘
                                         │ emerge from
Tier 3 (Calibration)  ┌──────────────────┴──────────────────────────┐
                      │  Salience      Drive Weight   Signal Weight   │
                      │  Learning      Loop           Adaptation      │
                      │                                              │
                      │  Autonomy      Procedural                    │
                      │  Progression   Memory Loop                   │
                      └──────────────────┬──────────────────────────┘
                                         │ tune
Tier 2 (Operational)  ┌──────────────────┴──────────────────────────┐
                      │  Memory         Task          Recon           │
                      │  Store/Recall   Execution     Gathering       │
                      │  Cycle          Cycle         Cycle           │
                      │                                              │
                      │  CLAUDE.md Handshake Cycle                   │
                      └──────────────────┬──────────────────────────┘
                                         │ driven by
Tier 1 (Cognitive)    ┌──────────────────┴──────────────────────────┐
                      │  Reflection Engine    Self-Learning Loop      │
                      │  (Micro→Light→        (Dopaminergic —         │
                      │   Deep→Strategic)      after interactions)    │
                      └──────────────────┬──────────────────────────┘
                                         │ triggered by
Tier 0 (Foundation)   ┌──────────────────┴──────────────────────────┐
                      │           AWARENESS LOOP                     │
                      │      5min tick, programmatic, zero LLM       │
                      └──────────────────────────────────────────────┘
```

### Tier 0: The Metronome

**Loop 1: Awareness Loop** — 5-minute tick, programmatic, zero LLM cost.

The metronome. Everything else either IS this loop, is triggered BY this loop, or feeds data BACK to this loop. Cycle: collect signals → compute composite urgency scores per depth → compare against thresholds → trigger Reflection Engine (or don't) → process escalation flags → wait 5 minutes → repeat.

What it tunes: nothing. It's pure perception. It fires and forgets.

*Detailed design: see Layer 1: Awareness Loop section above.*

### Tier 1: The Cognitive Engines

**Loop 2: Reflection Engine** — triggered by Awareness Loop, adaptive depth.

Where cognition happens. Cycle: triggered at a depth → assemble context (signals, memory, user model) → reason about what matters → produce observations, outreach, configuration changes → write outputs to memory/outreach → done until next trigger.

This is NOT a fixed-interval loop. Its frequency is emergent from signal urgency + time multipliers. Could fire 4 times in a busy hour or once in a quiet day. It also HOSTS the inline prompt patterns (salience eval, user model synthesis, governance, drive weighting) that feed the calibration loops — but the Reflection Engine itself doesn't tune parameters. It reads them.

*Detailed design: see Layer 2: Reflection Engine section above.*

**Loop 3: Self-Learning Loop** — event-driven, fires after interactions and outreach events.

The "dopaminergic system." Cycle: interaction completes → task retrospective with root-cause classification → lessons extracted → prediction errors logged → drive weights adjusted → salience model updated → procedural memory updated → capability gaps accumulated.

This is the ONLY loop that writes to calibration parameters. The Reflection Engine reads them; the Self-Learning Loop writes them. This clean separation prevents conflicting writes.

*Detailed design: see Layer 3: Self-Learning Loop section above.*

### Tier 2: The Operational Cycles

**Loop 4: Memory Store/Recall Cycle** — integrated into every conversation turn.

Cycle: message arrives → proactive recall injects relevant context → core facts loaded → response generated → exchange stored → facts/entities extracted → new memories linked to related existing memories (lightweight A-MEM pass).

Highest-frequency loop in the system (every conversation turn). Invisible to the user. This is what gives the system continuity across sessions.

**Loop 5: Task Execution Cycle** — on-demand, per user request.

Cycle: user request → planning pass (retrieve procedural memory) → spawn sub-agents → governance check at each capability boundary → quality gate per sub-agent → retry on failure (max 2) → deliver result → retrospective with root-cause classification → procedural memory extraction.

Each execution is a single pass, not a recurring loop. But across many executions, the retrospective → procedural memory → future planning chain creates a feedback cycle that spans tasks.

*Detailed design: see Task Execution Architecture section above.*

**Loop 6: Recon Gathering Cycle** — self-scheduled (recon-mcp manages its own cron).

Cycle: scan configured sources on schedule → store findings → push high-priority to Awareness Loop → low-priority accumulates → triage during Deep/Strategic reflection → acted-on findings feed future source prioritization.

The ONLY operational loop with its own internal scheduler, independent from the Awareness Loop. It pushes signals TO the Awareness Loop rather than being triggered BY it. Potential concurrent access during Deep reflection triage is handled by recon-mcp being the single writer; the Reflection Engine is a reader.

*Detailed design: see recon-mcp in 4 MCP Servers section above.*

**Loop 7: CLAUDE.md Handshake Cycle** — per Claude SDK invocation.

Cycle: Genesis recalls relevant memories → writes per-task dynamic CLAUDE.md → invokes Claude SDK → Claude SDK works with full persistent context → session completes → Genesis extracts learnings back into memory-mcp → next invocation's CLAUDE.md is richer.

The cross-engine learning bridge. Without it, Claude SDK sessions are stateless tools. With it, each invocation benefits from everything every previous invocation learned. Per-task isolation required for concurrent sub-agents (see Memory Separation section above).

### Tier 3: The Calibration Loops

These are not autonomous processes. They are **feedback cycles embedded within Tiers 1-2**, driven primarily by the Self-Learning Loop (Loop 3). Each follows the pattern: act → observe outcome → adjust parameter → future actions are different.

**Loop 8: Salience Learning** — tunes the world model that generates salience scores.

Cycle: world model predicts engagement for a signal → Reflection Engine uses prediction to decide outreach → outreach delivered → user engages or ignores → Self-Learning Loop computes prediction error → world model updated → future predictions are more accurate.

Timescale: days to weeks. Needs ~20-30 data points per topic category before calibration is meaningful. Thresholds can't drop below a noise floor (prevents spam) or rise above a ceiling (prevents going silent).

**Design note:** This loop merges what were originally two separate concepts — engagement calibration (adjusting thresholds) and world model refinement (improving the prediction model). They were merged because separating them creates a double-adjustment problem: if the prediction model gets more pessimistic AND the threshold rises independently, the system over-corrects and permanently suppresses certain topic types. The learning happens in the prediction model (world model); the threshold is fixed or very slowly adjusted at Strategic depth.

**Loop 9: Drive Weight Loop** — tunes the four drives (curiosity, competence, cooperation, preservation).

Cycle: drives shape Reflection Engine focus → actions taken → outcomes tracked → positive outcomes on cooperation-driven actions → cooperation sensitivity rises → Reflection Engine prioritizes cooperation signals → more cooperation actions.

Timescale: weeks. Slow-moving by design. Independent sensitivity multipliers, not a normalized budget (see Drive Weighting clarification in Reflection Engine section).

**Loop 10: Signal Weight Adaptation** — tunes Awareness Loop signal weights.

Cycle: signal X triggers reflection → reflection produces value (or doesn't) → Self-Learning Loop adjusts signal X's weight → signal X is more/less likely to trigger future reflections.

Timescale: days. Faster than drive weights because it's more granular. Strategic reflection can set temporary overrides ("raise error weight this week because we're deploying") that decay after the stated period.

**Loop 11: Autonomy Progression** — tunes per-category autonomy levels (L1-L7).

Cycle: action at current autonomy level → outcome (success/correction/failure) → evidence accumulates → N successes without correction → propose level increase → user explicitly approves → autonomy rises → more actions taken autonomously.

Regression: 2 corrections → drop one level. 1 harmful action → full reset. Self-detected systematic error → self-proposed regression. Silence ≠ approval — system periodically asks for explicit confirmation.

Timescale: weeks to months. Slowest calibration loop, by design.

*Detailed design: see Self-Evolving Learning: The Autonomy Hierarchy section above.*

**Loop 12: Procedural Memory Loop** — tunes "how to do things."

Cycle: novel approach tried → outcome with root-cause classification → if `success` or `approach_failure`: procedure extracted or updated → future similar task retrieves procedure → procedure used/adapted → outcome updates confidence → N failures → flagged for deprecation → Deep reflection reviews.

Dual-level: stores both specific steps AND underlying principle. Steps decay; principles persist. Procedures are always advisory context, never imperative instructions.

`capability_gap` and `external_blocker` outcomes do NOT feed into procedural memory adjustment — the system shouldn't "learn" that it's bad at tasks it simply can't do yet. However, these classifications require that the workaround search (§Task Execution → item 7) was exhausted first. `workaround_success` outcomes DO feed procedural memory — they're among the highest-value procedures because they encode creative problem-solving paths.

*Detailed design: see Procedural Memory Design section above.*

### Tier 4: The Emergent Spirals

These have no dedicated code. They arise from the interaction of the loops above. Calling them "spirals" rather than "loops" because they don't return to the same starting point — they compound.

**Spiral 13: User Model Deepening**

Powered by: Memory Store/Recall (4) + Salience Learning (8) + Reflection Engine user model synthesis.

Motion: interactions → user model synthesis → richer model → better salience evaluation → better outreach → user engages more meaningfully → richer interaction data → even richer model.

Timescale: Month 1 = shallow profile. Month 3 = calibrated. Month 6+ = anticipatory. Diminishing returns after ~6 months of active interaction.

**Spiral 14: Identity Evolution**

Powered by: Reflection Engine observations + Self-Learning Loop + user approval.

Motion: behavior produces observations → patterns accumulate → Deep/Strategic reflection proposes SOUL.md changes → user approves/rejects/modifies → identity files change → LLM reads different identity context → behavior shifts → new observations.

Timescale: months. Slowest spiral. Always requires user's hand on the steering wheel (L7 — never autonomous). This is the spiral that determines what KIND of system Genesis becomes. Everything else determines how well it performs; this determines what it IS.

**Spiral 15: Meta-Learning ("Learning how to learn")**

Powered by: Self-Learning Loop (3) observing its OWN effectiveness.

Motion: learning system produces calibration changes → changes produce outcomes → outcomes are measured by downstream utility (not output volume) → Self-Learning Loop notices effectiveness drift → proposes adjustment to learning parameters (trigger thresholds, damping factors, review structure) → user approves → learning system changes → different calibrations → different outcomes.

Timescale: months. Always user-approved for structural changes. Bounded self-adjustment (±20% on parameters) possible at L6.

Why this matters: without this, every other calibration loop has a fixed learning rate. With this, the learning rates themselves are learned.

*Detailed design: see "What 'Learns HOW to Learn' Actually Means in Practice" in the Autonomy Hierarchy section above.*

**Spiral 16: Capability Expansion Pipeline**

Powered by: Task Execution Cycle (5) + Self-Learning Loop root-cause classification + Strategic reflection.

This is the explicit end-to-end pipeline through which Genesis expands what it can do — named as a first-class architectural concept because the individual pieces (gap detection, ROI assessment, implementation, validation) already exist across the architecture but were not documented as a coherent flow:

```
Workaround Search (during task execution — see §Task Execution → item 7)
  → If workaround found → `workaround_success` → procedural memory (pipeline stops here)
  → If no workaround → `capability_gap` or `external_blocker`
      → Accumulation (gaps logged with frequency count)
      → ROI Assessment (Strategic reflection: how often hit? cost to close? user impact?)
      → Proposal (capability acquisition plan, L5+ autonomy)
      → User Approval (always for new external integrations)
      → Implementation (claude_code tool builds it, or manual install)
      → Validation (test, verify, quality gate)
      → Integration (new tool/procedure registered, future tasks can use it)
      → Retrospective (did it actually help? frequency of use? update learning)
```

Note: the workaround search step at the top is the first line of defense. Many "gaps"
never reach the Capability Expansion Pipeline because Genesis finds an indirect path
during execution. The pipeline only fires when the workaround search was genuinely
exhausted. This is by design — workarounds are fast (seconds/minutes during task
execution), while the pipeline is slow (accumulation over multiple tasks, Strategic
reflection review, user approval).

Motion: task attempted → capability gap discovered → gap logged to accumulator → Strategic reflection reviews accumulated gaps → evaluates ROI: "How many times was this gap hit? What would it take to close it? Is the investment justified?" → proposes capability acquisition (new tool, MCP integration, skill) → user approves → capability added → future tasks succeed → new gaps discovered at the frontier.

`external_blocker` outcomes with `revisit_after` dates are re-evaluated during Strategic reflection: "Has the technology landscape changed? Is this now feasible?" Blockers that become feasible are promoted to capability gaps.

Autonomy: L5 for proposing acquisitions, L6+ for self-acquiring (e.g., installing a new tool). User always approves new external integrations.

Why this matters: without this, the system gets better at what it already CAN do but never expands WHAT it can do. This is what prevents capability plateaus.

### Loop Interaction Map

How the loops feed each other:

```
                    ┌─── Loop 1: Awareness Loop ───┐
                    │         (5min tick)            │
                    │    collects signals from:      │
                    │    • Loop 4 (memory backlog)   │
                    │    • Loop 6 (recon findings)   │
                    │    • Loop 8 (engagement data)  │
                    │    • health-mcp                │
                    └──────────┬────────────────────┘
                               │ triggers
                    ┌──────────▼────────────────────┐
                    │ Loop 2: Reflection Engine      │
                    │  reads from:                   │
                    │  • Loop 9 (drive weights)      │
                    │  • Loop 13 (user model)        │
                    │  • Loop 12 (procedures)        │
                    │  • Loop 11 (autonomy levels)   │
                    │  writes to:                    │
                    │  • Observations (memory-mcp)   │
                    │  • Outreach queue              │
                    │  • Loop 14 (evolution proposals)│
                    │  • Escalation flags (Loop 1)   │
                    └──────────┬────────────────────┘
                               │ feeds
                    ┌──────────▼────────────────────┐
                    │ Loop 3: Self-Learning Loop     │
                    │  THE KEYSTONE — sole writer to:│
                    │  • Loop 8 (salience model)     │
                    │  • Loop 9 (drive weights)      │
                    │  • Loop 10 (signal weights)    │
                    │  • Loop 11 (autonomy evidence) │
                    │  • Loop 12 (procedures)        │
                    │  • Spiral 16 (capability gaps)  │
                    └───────────────────────────────┘
```

The Self-Learning Loop is the keystone. Remove it and Tiers 0-2 still work — the system perceives, thinks, and acts. But nothing improves. The system is frozen at its initial calibration forever.

### Design Caveats

**Salience learning is a single adjustment, not two.** Engagement calibration and world model refinement share one parameter space. The learning happens in the world model (prediction accuracy); thresholds are fixed or adjusted only at Strategic depth. Separate adjustment creates oscillation through double-punishment of topics.

**Meta-learning measures downstream utility, not output.** "Did this reflection produce observations?" is the wrong metric. The right metric: "Were the outputs USED? Retrieved by a subsequent process? Acted on by the user?" Volume of observations is not evidence of value.

**Root-cause classification prevents false learning.** `capability_gap` and `external_blocker` outcomes bypass procedural memory adjustment — but ONLY after the workaround search (§Task Execution → item 7) is exhausted. Most apparent "gaps" are solved by workarounds during execution and never reach this pipeline. The workaround search is the first line of defense; the Capability Expansion Pipeline handles the genuine gaps that survive it. `workaround_success` outcomes produce high-value procedural memory — the workaround becomes the primary approach for future identical tasks.

**External blockers have a lifecycle.** They aren't dead ends. Classification: (a) user-rectifiable — surface as blocker via outreach; (b) future capability gap — parked with `revisit_after` date, re-evaluated during Strategic reflection as technology landscape changes; (c) permanent constraint — logged and accepted.

**Depth escalation preserves single-coordinator authority.** The Reflection Engine can flag that deeper analysis is needed, but the Awareness Loop is ALWAYS the one that invokes it. Critical escalations get an immediate out-of-cycle tick; everything else waits for the next 5-minute tick. No self-triggering.

**Per-task CLAUDE.md isolation.** Concurrent sub-agents each get their own CLAUDE.md context to prevent overwrite races. The handshake cycle is per-task, not global.

---

## LLM Weakness Compensation: Architectural Patterns

LLMs are remarkably good at the things Genesis needs most — contextual reasoning, pattern recognition, natural language understanding, flexible judgment. But an autonomous system that runs for months amplifies specific weaknesses that are tolerable in single conversations. This section documents the weaknesses that matter, the compensating patterns adopted, and the patterns considered but deferred.

**Core principle:** The architecture doesn't "fix" the LLM. It plays to its strengths (judgment, interpretation, synthesis) and puts guardrails on the specific situations where it predictably fails (computation, calibration, confabulation under uncertainty). LLMs interpret; code computes.

**Orchestration prompt quality is paramount.** Every backend prompt — task decomposition,
sub-agent dispatch, workaround search, retrospective analysis, reflection — determines
what road Genesis sends each agent down. A poorly crafted orchestration prompt produces
agents that hit dead ends and give up. A well-crafted one produces agents that navigate
around obstacles and find the exit. The prompts are the steering mechanism for the entire
cognitive layer. This means prompt engineering for Genesis's backend orchestration is not
a polish step — it's a primary architectural concern on par with schema design and system
wiring. Prompts should be iterated with the same rigor as code: tested, measured, and
refined based on outcomes tracked by the Self-Learning Loop.

### The Weaknesses That Compound Over Time

These are ranked by damage to a system that runs autonomously for months, not by frequency in single conversations.

**1. Confabulation under uncertainty (CRITICAL).** LLMs fabricate plausible answers rather than saying "I don't know." In a conversation this is annoying. In an autonomous system that writes to persistent memory, it's corrosive. Confabulated user preferences get stored → retrieved as facts → inform future reasoning → produce more confident (but still wrong) conclusions → stored again. The system drifts from reality through accumulated micro-confabulations that reinforce each other. Affects: user model synthesis, procedural memory extraction, recon triage.

**2. Tunnel vision / anchoring in long contexts (HIGH).** The LLM that produced reasoning is anchored to it. A fresh LLM seeing only the output often reaches a completely different conclusion. Affects: quality gates, Deep reflection (early jobs color late jobs), identity evolution proposals (anchored to existing SOUL.md framing).

**3. Overconfidence / poor calibration (HIGH).** LLMs express certainty regardless of actual reliability. Salience scores, confidence values, and prediction errors LOOK precise but have wide error bars. Learning from the "error" between two poorly calibrated numbers is learning from noise. Affects: salience scoring, procedural memory confidence, Self-Learning Loop prediction errors.

**4. Mode collapse under repetition (MEDIUM-HIGH).** Same prompt running repeatedly → outputs converge to formulaic patterns. By day 3, Micro reflections will sound identical: "No significant anomalies detected. System operating normally." Even when there IS something worth noticing. Affects: Micro reflection, task retrospectives, engagement prediction.

**5. Sycophancy / prompt compliance bias (MEDIUM).** LLMs produce outputs matching what the prompt seems to want. "Find problems" → problems found (even when there aren't any). "Evaluate how well you're doing" → favorable self-assessment. Affects: Self-Learning Loop self-evaluation, identity evolution (always proposes changes because the prompt asks), Reflection Engine productivity (always finds something "worth noting").

**6. Temporal reasoning weakness (MEDIUM).** LLMs are bad at sequences, trends, and causality over time. Affects: trend detection in reflection, engagement trajectory analysis, cost projection.

**7. Positional bias — "lost in the middle" (LOW-MEDIUM).** LLMs pay more attention to the beginning and end of context. Middle content gets underweighted. Affects: memory injection, multi-job reflection, recon triage with multiple findings.

### Adopted Patterns

#### Pattern 1: Compute Hierarchy — Right Model for Each Job

> **Concrete assignments:** For the specific model assigned to each of the 28
> call sites (with fallback chains, free compute sources, and paid alternatives),
> see the [Model Routing Registry](genesis-v3-model-routing-registry.md).

The foundation runs on free/cheap compute. Expensive models are used surgically for judgment calls. This is not "good, cheap, fast — pick two." It's using the right tool for each job.

**Availability note:** The **separate GPU machine** that runs 20-30B models is NOT available 24/7 — it is a different host from the Ollama container. When the GPU machine is unavailable, 20-30B tasks fall back to cheap cloud models (Gemini Flash free tier, GLM5, or equivalent). The system must detect GPU machine availability and route accordingly. Gemini's free API tier (~10-30 calls/day) is a valuable resource — use it rather than leaving it idle.

```
┌─────────────────────────────────────────────────────────┐
│  ALWAYS ON (24/7, zero marginal cost)                    │
│                                                          │
│  Programmatic layer (no LLM)                             │
│  • Awareness Loop signal collection + urgency scoring    │
│  • Engagement statistics (rates, trends, moving averages)│
│  • Cost tracking + budget arithmetic                     │
│  • Confidence score calculation (success_count / total)  │
│  • Trend detection (change point detection, baselines)   │
│  • Context assembly with position weighting              │
│  • Root-cause classification routing (once classified)   │
│  • Maturity metrics (data volume tracking — see below)   │
│                                                          │
│  Local 3B model (Ollama container, always available)      │
│  • JSON/structured output parsing and validation         │
│  • Binary classification ("is this well-formed?")        │
│  • Simple tagging (memory type, source type)             │
│  • Keyword/entity extraction from short text             │
│  Fallback when unavailable: regex/heuristic or skip      │
│                                                          │
│  ⚠ 3B models CANNOT reliably:                           │
│  • Evaluate quality or relevance of content              │
│  • Perform root-cause classification (judgment call)     │
│  • Synthesize across multiple inputs                     │
│  • Generate natural language that will be user-facing     │
│  • Assess salience, urgency, or importance               │
│  If in doubt about 3B capability → escalate to 20-30B    │
└──────────────────────────────┬──────────────────────────┘
                               │ escalates to
┌──────────────────────────────▼──────────────────────────┐
│  HIGH FREQUENCY, LOW COST                                │
│                                                          │
│  20-30B model (GPU machine or cloud — see note above)    │
│  • Micro reflections (every 30min)                       │
│  • Task retrospective drafting + root-cause classification│
│  • Routine procedural memory extraction                  │
│  • Memory consolidation (batch processing)               │
│  • Fact/entity/relationship extraction from conversations │
│  • Speculative hypothesis generation (with tags)         │
│  Fallback when unavailable: Gemini Flash free tier / GLM5│
│                                                          │
│  Gemini Flash free tier (~10-30 calls/day)               │
│  • Default fallback for 20-30B when GPU machine offline  │
│  • Light reflections (every 6h) when local unavailable   │
│  • Recon finding preliminary evaluation                  │
│  • Cross-check on 20-30B outputs (cheap second opinion)  │
│  • Outreach draft generation (not final review)          │
│                                                          │
│  GLM5 / other affordable API models                      │
│  • Overflow when Gemini free tier exhausted               │
│  • Routine task execution for simple tasks               │
│  • Bulk memory operations                                │
│                                                          │
│  ⚠ 20-30B / Flash-class models CANNOT reliably:         │
│  • Complex multi-step reasoning chains                   │
│  • Nuanced judgment about user intent or preferences     │
│  • Architectural or strategic analysis                   │
│  • Identity evolution proposals (stakes too high)        │
│  • Quality gates on complex task outputs                 │
│  If in doubt → escalate to Sonnet-class                  │
└──────────────────────────────┬──────────────────────────┘
                               │ escalates to
┌──────────────────────────────▼──────────────────────────┐
│  JUDGMENT CALLS (surgical, high-value)                   │
│                                                          │
│  Sonnet / GPT-4o class                                   │
│  • Deep reflection (adaptive, only when warranted)       │
│  • Light reflection (when 20-30B / free tier unavailable) │
│  • Fresh-eyes review on outreach before sending          │
│  • Quality gates on complex task outputs                 │
│  • Cross-model review (different provider than primary)  │
│  • Meta-prompting for Deep/Strategic reflection          │
│  • User model synthesis (nuanced judgment required)      │
│                                                          │
│  Opus / best-available                                   │
│  • Strategic reflection                                  │
│  • Identity evolution proposals + second opinion         │
│  • Complex task planning and orchestration               │
│  • Capability gap ROI assessment                         │
│  • Configuration change review (high blast radius)       │
│  • The decisions that shape everything downstream        │
└─────────────────────────────────────────────────────────┘
```

**The operating principle:** Classification and computation don't need intelligence. Routine extraction needs moderate intelligence. Judgment calls that persist or are hard to reverse need the best available. The 3B + programmatic layer handles ~80% of all "calls" at zero cost. The 20-30B / Flash tier handles ~15%. The expensive models handle ~5% — but those are the 5% that shape the system's trajectory.

**Default: escalate when uncertain.** If there's any doubt about whether a smaller model can handle a task, escalate. A wasted Sonnet call costs cents. A bad judgment from a 3B model that gets stored in memory costs far more to fix downstream.

##### Context Allocation by Model Tier

> See also: [Model Routing Registry](genesis-v3-model-routing-registry.md) for
> per-call-site context budgets.

Each model tier receives a different context window budget. Smaller models get
less context (both because their windows are smaller and because they can't
effectively use large context). Larger models get richer context because they
can synthesize across more inputs.

| Model Tier | Context Allocation | What's Included |
|------------|-------------------|-----------------|
| **3B (Ollama)** | Minimal (~2K tokens) | Prompt template + interaction summary + few-shot examples (5-8). No memory retrieval, no user model, no history. The model classifies/tags, it doesn't reason. |
| **20-30B (GPU machine)** | Moderate (~4-8K tokens) | Everything above + top-k activated memories (3-5) + user model summary (compressed) + recent observations (unresolved, high priority) + relevant procedural memory (if task matches). Enough context for informed micro/light reflection. |
| **Sonnet-class** | Full (~16-32K tokens) | Everything above + full procedural memory for task type + execution trace history (recent) + observation history (last 48h) + expanded user model + open questions + drive weights + signal state. Deep reflection needs the full picture. |
| **Opus-class** | Full+ (~32-64K tokens) | Everything above + cross-cutting context from multiple domains + strategic history + identity document. Strategic reflection needs maximum context to detect patterns that span weeks. |

**Key constraint:** Context assembly is always position-weighted — most relevant
items go first, least relevant at the end. This is critical for all tiers but
especially for smaller models where attention degrades faster with position.

**V3 simplification:** Context budgets are fixed per tier. V4 could make them
adaptive (allocate more context when the task is complex, less when routine).

#### Pattern 2: Meta-Prompting for Adaptive Reflection

Instead of hardcoding what Deep/Strategic reflection should focus on, a cheap model examines the signal landscape and generates the reflection questions. An LLM that prompts an LLM.

This solves two problems simultaneously:
- **Mode collapse:** Questions are different each time because the signal landscape is different
- **Decomposition vs. synthesis:** The meta-prompter sees everything (holistic), generates focused questions (decomposed), and a synthesis pass catches cross-cutting patterns

```
Step 1: Meta-prompt (cheap — 20-30B local or Gemini Flash)
  Input: Full signal landscape from Awareness Loop
  Task: "Given these signals, what are the 3-5 most important
         questions this reflection should answer? Consider
         cross-cutting patterns across signals, not just
         individual items. What might connect seemingly
         unrelated signals?"
  Output: 3-5 focused questions with relevant context scope

Step 2: Deep reflection (capable — Sonnet or Opus)
  Input: Each question + only its relevant context (from MCP)
  Task: Answer each question with grounded evidence
  Output: Observations, proposals, actions per question

Step 3: Synthesis (capable — fresh call, same or different model)
  Input: ONLY the answers from Step 2 (not the reasoning)
  Task: "Do any of these answers interact? Are there patterns
         across them that the individual answers missed?"
  Output: Cross-cutting insights, integrated observations
```

**Why the meta-prompter is the most critical call in the system:** If the meta-prompter asks the wrong questions, the entire reflection is wasted regardless of how capable the answering model is. A brilliant answer to the wrong question is worthless. The meta-prompter should err toward breadth — it's better to ask one unnecessary question (cheap to answer, easily discarded) than to miss a question that mattered.

**Cost profile:** Step 1 is cheap (~100-500 tokens output). Step 2 is the expensive part but is focused and efficient. Step 3 is moderate. Total cost is often LESS than a single monolithic Deep reflection prompt because each step's context is smaller.

#### Pattern 3: Speculative vs. Grounded Claims

Every factual claim written to persistent memory must be either grounded in evidence or explicitly tagged as speculative.

**Grounded claims:** The Reflection Engine prompt includes the constraint:
> "For factual claims about the user, the system, or patterns, cite the specific memory ID, observation ID, or data point that supports them. If you have evidence, cite it. If you're inferring from indirect signals, say so explicitly."

**Speculative claims (hypotheses):** The LLM's ability to notice fuzzy patterns IS its superpower — don't suppress it. But speculative insights get stored differently:
- Tagged with `speculative: true`
- Given a `hypothesis_expiry` timestamp (e.g., 14 days from creation)
- Stored with `evidence_count: 0`
- NEVER used as context for future reasoning UNTIL confirmed

**Confirmation cycle:** When new evidence appears that supports a hypothesis, `evidence_count` increments and `speculative` can be flipped to `false` once count reaches a threshold (default: 3 independent evidence points). Hypotheses that expire without confirmation are archived, not used as context.

**This breaks the confabulation compound loop.** Hallucinated preferences can't propagate into future context because they're quarantined until confirmed. They die in one generation instead of compounding.

#### Pattern 4: Fresh-Eyes Review (Selective)

A separate LLM call reviews ONLY the output, without the reasoning chain. Applied selectively to high-stakes, persistent, or hard-to-reverse outputs.

| Process | Primary Model | Reviewer | What reviewer sees |
|---------|--------------|----------|-------------------|
| Identity evolution proposals | Opus (Strategic) | Sonnet (different provider preferred) | Current SOUL.md + proposed changes only. NOT the observations that led there |
| Strategic config changes | Opus (Strategic) | Sonnet (fresh call) | Current config + proposed change + stated rationale only |
| Outreach before sending | Sonnet (Reflection Engine) | Flash/20-30B (cheap) | Draft message + user model summary. "Would the user want to receive this?" |
| Complex task quality gate | Claude SDK (task agent) | Different model (Gemini/GPT) | Original request + final output only. NOT the execution trace |

**Cross-model review is stronger than same-model review.** Same model shares training biases. Different providers catch different blind spots. The multi-model capability through LiteLLM exists for this purpose.

**Evaluation hierarchy principle:** When one model evaluates another model's work,
the evaluator should be **the next capability tier up.** 30B evaluates 3B decisions.
Sonnet evaluates 30B outputs. This is the teacher-student paradigm: the evaluator
must have strictly more capability than the evaluated to catch mistakes the smaller
model can't see. This principle governs triage calibration (30B audits 3B triage),
workaround search review (see §Task Execution → item 7), and quality gates. It does
NOT apply to orchestration (where the most capable model steers regardless of what
it's steering) or to cross-provider review (where the point is bias diversity, not
capability differential).

**NOT applied to:** Micro reflections (defeats zero-cost purpose), intermediate reasoning, memory recalls, routine procedural extraction. The cost of review must be justified by the blast radius of the output.

#### Pattern 5: Prompt Variation for High-Frequency Loops

Micro reflection and task retrospectives use rotating prompt framings to fight mode collapse.

**Micro reflection pool (rotate through):**
1. "What would surprise the user if they looked at the system right now?"
2. "What's the weakest link in the system's current state?"
3. "If I had to bet on what will go wrong in the next 6 hours, what would it be?"
4. "What signal am I NOT paying attention to that I should be?"
5. "What's the most valuable thing I could do right now that I'm NOT doing?"
6. "What assumption am I making that I haven't verified recently?"
7. "If a new operator took over right now, what would they notice first?"

**Task retrospective framings (rotate):**
1. "What surprised me about this task?"
2. "What would I do differently next time?"
3. "What did I assume that turned out to be wrong?"
4. "What capability would have made this easier?"

Each framing biases the LLM toward different types of observations. The aggregate across rotations gives broader coverage than any single prompt repeated indefinitely.

**Note:** With meta-prompting (Pattern 2) applied to Deep/Strategic reflections, prompt variation is only needed for the high-frequency loops (Micro, retrospectives) where meta-prompting would be overkill. The meta-prompter provides natural variation for the deeper reflections.

#### Pattern 6: Null Hypothesis with Maturity Calibration

For evaluative prompts, explicitly offer "nothing to report" as a valid — even preferred — output. But calibrate the threshold to the system's maturity, measured by DATA VOLUME rather than time elapsed.

**The framing:**
> "Review recent activity. The default answer is 'no significant patterns.' Only override this default if you find something meeting ALL of these criteria: [specific, evidence-backed criteria]. If nothing meets ALL criteria, output exactly: `NO_SIGNAL`."

**Why data volume, not time:** Time is an unreliable proxy for system maturity. A system that processes 500 interactions in week 1 is more mature than one that processes 50 interactions in month 2. The relevant maturity milestones are:

| Metric | "Early" (low threshold) | "Calibrated" (moderate) | "Mature" (high threshold) |
|--------|------------------------|------------------------|--------------------------|
| Procedural memory entries | < 50 | 50-200 | 200+ |
| User model evidence points | < 30 | 30-100 | 100+ |
| Outreach engagement data points | < 20 | 20-80 | 80+ |
| Task execution traces | < 100 | 100-500 | 500+ |
| Total memory items | < 500 | 500-2000 | 2000+ |

**Extraction threshold calibration:**
- **Early:** Most tasks WILL produce novel procedures or lessons. Threshold for "this is worth extracting" should be LOW. You're building the foundation.
- **Calibrated:** Common patterns are captured. Threshold rises. Looking for genuine novelty or refinements to existing procedures.
- **Mature:** Genuine novelty is rare. Threshold is high. Most extractions should be updates to existing procedures, not new entries.

**Predicting data milestones:** The system should track its own data accumulation rate and estimate when it will transition between maturity phases. This is purely programmatic: `current_procedural_count / daily_accumulation_rate = days_to_next_milestone`. Strategic reflection can use these projections to plan ahead ("approaching calibrated phase — tighten extraction threshold in ~2 weeks").

**The Self-Learning Loop tracks extraction utility over time.** If novel procedure discovery drops to near-zero but task quality isn't improving, that signals either: procedures aren't being used effectively, or the system is failing to learn from genuinely new situations. If discovery stays HIGH after reaching "mature" data volumes, that signals either: genuinely novel domains (good), or failure to recognize variants of existing procedures (dedup problem).

#### Pattern 7: LLM Interprets, Code Computes

The LLM should NEVER perform arithmetic, statistical analysis, or trend detection. These are computed programmatically and presented to the LLM as data to interpret.

| Ask the LLM | Compute programmatically |
|-------------|------------------------|
| "Is engagement declining?" | `engagement_rate_last_7d` vs `engagement_rate_prior_7d`. Present: "Engagement: 54% (last 7d) vs 72% (prior 7d). Delta: -18pp." |
| "Am I within budget?" | `spend_today / daily_budget`. Present: "Spent $4.20 of $8.00 daily budget (52.5%)." |
| "Is this error rate unusual?" | `current_rate` vs `30d_moving_avg`. Present: "Error rate 3.2%, vs 30-day avg 0.8% — 4x elevated." |
| "How confident is this procedure?" | `success_count / total_count`. Present: "4 successes, 1 failure (80%)." |
| "Salience of this signal?" | Compute base from historical engagement for similar topic. LLM adjusts ±contextual modifier. Present: "Base salience 0.70 (historical). Your contextual adjustment: [LLM fills in]." |

**Present computed data as measurements, not facts.** Include possible confounds: "Measurement: engagement dropped 18pp. Note: user was traveling last week — this may not reflect actual preference change." The LLM's job is to interpret in context, not to trust blindly.

#### Pattern 8: Adversarial Counterargument for High-Stakes Outputs

For outputs that are persistent, hard-to-reverse, or shape the system's trajectory, inject a structured `<counterargument>` block into the reasoning chain that forces the LLM to argue AGAINST its own conclusion before finalizing.

**Applied selectively to:**

| Process | Why it helps |
|---------|-------------|
| Strategic reflection proposals | Prevents anchoring bias on initial analysis |
| Identity evolution proposals | Forces "what if this change is wrong?" before touching SOUL.md |
| Procedure deprecation decisions | Prevents premature removal of procedures that seem unused but serve edge cases |

**NOT applied to:** Micro reflections (defeats zero-cost purpose), routine extraction, memory recalls, outreach drafts (social simulation already provides the "other perspective"). Same selectivity principle as Fresh-Eyes Review (Pattern 4).

**Prompt pattern:**
```
Before finalizing your recommendation:
<counterargument>
Argue against your own conclusion. What evidence would disprove it?
What assumption are you most uncertain about? If you're wrong, what's
the cost of acting on this recommendation?
</counterargument>
Now, considering both your original reasoning and the counterargument,
state your final recommendation with explicit confidence level.
```

**Relationship to Pattern 4 (Fresh-Eyes Review):** These patterns are complementary, not redundant. Pattern 4 is a DIFFERENT model reviewing the output (catches blind spots from training bias). Pattern 8 is the SAME model reviewing its own reasoning (catches anchoring and confirmation bias within its own chain-of-thought). Pattern 4 is external review; Pattern 8 is internal adversarial reasoning.

**Cost:** Near-zero incremental — just additional tokens in the existing prompt. No extra LLM call needed.

#### Pattern 9: Infrastructure Resilience — Surviving API Failures

The Compute Hierarchy (Pattern 1) describes WHICH model to use. This pattern describes
WHAT HAPPENS WHEN IT FAILS. Every LLM API call, embedding request, and MCP server
operation can fail transiently — rate limits, timeouts, dropped connections, partial
responses. An autonomous system that runs 24/7 will hit these routinely.

**Key components:**
- **Exponential backoff + jitter** on all API calls (prevents synchronized retry storms)
- **Circuit breaker** per provider (stops hammering a down service, routes to fallback)
- **Per-provider retry budget** (limits aggregate retries, prevents cascade failures)
- **Idempotent writes** on all MCP server operations (prevents duplicates on timeout-retry)
- **Dead-letter staging** for failed persistence operations (cognitive work isn't lost)
- **Graceful degradation levels** (from transparent fallback to essential-only mode)

**Why this is an LLM weakness compensation pattern:** LLM APIs are uniquely unreliable
compared to traditional APIs. Rate limits are aggressive, response times vary 10x,
providers have frequent capacity issues, and partial/malformed responses are common.
A system that calls LLM APIs hundreds of times per day WILL encounter transient failures.
Without explicit handling, these failures cascade through the cognitive layer — a failed
embedding call prevents memory retrieval, which prevents context injection, which
degrades reflection quality, which produces bad observations, which the Self-Learning
Loop treats as real signal.

**The Awareness Loop itself must be resilient.** If the 5-minute tick's signal collection
fails, the entire cognitive layer goes blind. The tick is programmatic (no LLM), but it
queries MCP servers that depend on Qdrant and SQLite. Circuit breakers on these
dependencies ensure the tick degrades gracefully rather than crashing.

**Full design:** See `genesis-v3-resilience-patterns.md` for backoff parameters, circuit
breaker thresholds, retry budgets, degradation levels, and dead-letter staging design.

### Failure Modes of These Patterns (Honest Assessment)

**Programmatic scaffolding creates rigidity.** Pre-computed metrics become unquestionable axioms. The LLM loses the ability to question whether the measurement ITSELF is meaningful. Mitigation: always present with confounds and interpretation framing.

**Grounded claims can suppress genuine intuition.** The speculative/grounded split helps, but the LLM may learn to avoid speculative claims to seem "more rigorous." Mitigation: explicitly prompt for hypotheses in addition to grounded observations. "What do you SUSPECT but can't prove?"

**Fresh-eyes review can create false confidence.** "Two models agreed, so it must be right." Two models sharing similar training data have correlated blind spots. Agreement doesn't equal correctness. Mitigation: treat agreement as higher confidence, not certainty. Track review agreement rate — if it's >95%, the review is probably not adding value.

**Meta-prompting adds a new failure mode: wrong questions.** If the meta-prompter asks the wrong questions, the entire reflection is misdirected. A brilliant answer to the wrong question is worthless. Mitigation: Strategic reflection periodically audits meta-prompt question quality — "Were the questions I asked last Deep reflection the right ones, in hindsight?"

**Maturity calibration requires accurate data volume tracking.** If the system miscounts its own data, it miscalibrates its thresholds. A data corruption event could reset maturity perception. Mitigation: data volume metrics are computed from actual MCP queries, not maintained counters. Can't drift from reality.

**Over-verification creates decision paralysis.** Every review pass, confidence check, and grounding requirement adds latency and creates opportunities to defer action. For a proactive assistant, being too cautious may be worse than being too confident — a system that never reaches out because it's never confident enough generates no engagement data to learn from. Mitigation: verification budget. Each loop gets a maximum number of review passes. After that, act on best available judgment.

### Deferred Patterns (Considered, Not Adopted)

These patterns were evaluated and deferred — either because they're lower-impact than initially assessed, introduce more complexity than they're worth at v3 scope, or are premature before real operational data exists.

**Position-aware context assembly.** Placing high-salience memories at the start/end of injection blocks to compensate for "lost in the middle" bias. Status: LOW-MEDIUM impact. The effect is real but smaller than the other patterns. Salience-ranked inclusion (already in design) matters more than position within the included set. **Revisit when:** operational data shows that mid-position memories are systematically underweighted in reflection outputs.

**Disagreement-as-signal tracking.** When primary and reviewer LLMs disagree, storing both assessments with a disagreement flag for later adjudication. Status: interesting but adds schema complexity for uncertain gain. **Revisit when:** fresh-eyes review is operational and disagreement rates can be measured. If disagreement is rare (>90% agreement), the tracking adds no value. If frequent (<70% agreement), the system has bigger problems than tracking can solve.

**Per-claim citation verification.** Programmatically parsing every LLM output to verify that cited memory IDs actually exist. Status: too rigid. Kills soft pattern recognition. The speculative/grounded tag system (Pattern 3) achieves the same goal with less brittleness — it doesn't verify citations, it separates claims into different trust tiers. **Revisit when:** confabulation is observed to be a real problem despite Pattern 3. If speculative tagging successfully prevents compound confabulation, this isn't needed.

**Full decomposition of Deep reflection into independent calls.** Running each of Deep reflection's jobs as a separate focused LLM call. Status: overengineered. Loses cross-cutting insights (the monolithic prompt's weakness is also its strength). Meta-prompting (Pattern 2) provides better decomposition — the meta-prompter sees everything holistically while the answerer gets focused questions. **Revisit when:** Deep reflection quality is observably poor due to tunnel vision or positional bias. Meta-prompting should be tried first.

**Full adversarial "devil's advocate" pass** (separate prompt after every action). Status: still redundant as a blanket pass — Pattern 4 covers general review. However, the TARGETED version (inline `<counterargument>` block for specific high-stakes outputs) was adopted as Pattern 8 below. The distinction: Pattern 8 is the LLM arguing against its OWN conclusion within the same reasoning chain, not a separate reviewer. It complements Pattern 4 rather than duplicating it.

**Relationship rhythm loop.** Dynamic matching of the system's interaction rhythm to the user's life patterns ("less responsive on weekends → shift outreach"). Status: deferred to post-v3. Static quiet-hours config is sufficient for v3. Dynamic rhythm learning requires substantial engagement data that won't exist until post-bootstrap. **Revisit when:** 3+ months of engagement data with clear temporal patterns. See Open Design Questions #11.

**Cost optimization loop.** Explicit feedback cycle for optimizing model/engine routing based on cost-per-value-delivered. Status: implicit in the compute hierarchy, not worth a dedicated loop at v3. Budget tracking exists. Engine selection exists. The explicit ROI optimization loop is premature before real cost data accumulates. **Revisit when:** 1+ month of per-engine cost tracking shows clear optimization opportunities.

---

## Cognitive Surplus: Intentional Use of Free Compute

The current architecture treats all compute as a budget — spend when triggered, conserve when not. But free compute (local models, free API tiers) creates a **cognitive surplus**: unused capacity that exists whether you use it or not. Not using it is waste.

This section formalizes how Genesis intentionally uses surplus capacity to become better at its job and generate value for the user — not as a side effect, but as a designed behavior.

### The Core Shift

Most AI systems are purely reactive — they think when spoken to. Genesis already breaks this with the Awareness Loop (it thinks on a schedule). Cognitive surplus goes further: **Genesis thinks about what to think about, using compute that costs nothing.**

This is the curiosity, competence, and cooperation drives operationalized. The drives shape WHAT the system focuses on; cognitive surplus provides the WHEN — during idle cycles that would otherwise be wasted.

### Surplus Compute Tiers

Surplus tasks are dispatched based on what free compute is available, with frequency proportional to cost. The bright line: models with meaningful marginal cost are NEVER used for surplus.

| Tier | Availability | Surplus Frequency | Surplus Role |
|------|-------------|-------------------|-------------|
| **20-30B (GPU machine)** | When GPU machine is on | Always (primary surplus workhorse) | Gap pattern recognition, prompt effectiveness review, project trajectory extrapolation, anticipatory research, self-improvement brainstorming |
| **Gemini free tier** | ~10-30 calls/day | Daily allocation for surplus (after active task needs) | Higher-quality ideation, cross-referencing, user value brainstorming, surplus outreach drafting |
| **Local 3B (Ollama)** | Always (Ollama container is always on) | Light tasks only — must stay responsive | Memory cross-referencing (pre-computed, no generation), data counting, staleness flag checks |
| **GLM5 / cheap API** | Available but has marginal cost | Rarely — only when surplus queue has high-priority items backed up | Overflow for high-value surplus tasks when local is unavailable |
| **Sonnet+ class** | Available but expensive | **NEVER for surplus** | — |
| **Opus class** | Available but expensive | **NEVER for surplus** | — |

**Ollama 3B constraint:** The 3B model runs on the Ollama container (CPU only, always available) and is already committed to embeddings and extraction for active operations. Surplus tasks on the 3B must be LIGHT — pre-computed cross-references, boolean checks, counting operations. No generation, no reasoning, no synthesis. If a surplus task requires any judgment, it waits for the 20-30B or Gemini free tier.

**GPU machine availability:** The separate machine running 20-30B models is NOT available 24/7. When it's offline, surplus capacity drops to whatever Gemini free tier calls remain for the day. When it comes back online, the surplus queue resumes. The system tracks GPU machine availability patterns and can learn to front-load surplus work during expected uptime windows.

**Cost-frequency principle:** The lower the cost, the more often it runs.
- Free (local) = always runs when idle
- Nearly free (Gemini free tier) = runs daily, after active needs are met
- Cheap (GLM5) = runs only for backed-up high-priority surplus items
- Above cheap = never runs for surplus, period

### What Surplus Thinking Does

Three categories of spare-time computation, mapped to drives:

**Self-Improvement (Curiosity + Competence drives):**
- **Procedure audit:** Which procedures haven't been used or validated recently? Are they still valid given tool/environment changes? No signal will ever trigger "audit your stale procedures" — this only happens proactively.
- **Memory quality scan:** What speculative hypotheses are approaching expiry? Can confirming/refuting evidence be found by cross-referencing existing memory (no new API calls needed)?
- **Gap pattern clustering:** Looking at recent capability gap entries — are there clusters? Three tasks failing because of the same missing integration is one gap, not three.
- **Prompt effectiveness review:** Which Micro prompt rotations produce NO_SIGNAL most often? Is that healthy quiet or a blind spot?
- **Reflection quality self-audit:** Did observations from the last N reflections get retrieved or acted on? If not, why?
- **Surplus retrospective:** Re-examine recent tasks from a different perspective than the primary retrospective used. Free compute second opinions catch things the first pass anchored away from. Especially valuable during the "early" maturity phase when most tasks genuinely DO produce novel lessons.

**User Value Ideation (Cooperation drive):**
- **Project trajectory extrapolation:** Based on the user's recent work patterns, what will they likely need next? Can anything be pre-researched?
- **Cross-pollination:** The user solved problem A using technique T. Another project has a structurally similar problem. Worth flagging?
- **Tooling opportunity detection:** The user does task Z manually. Could automation be built? What would it take?
- **Anticipatory research:** The user mentioned interest in topic Q but hasn't followed up. Lightweight scan — anything worth surfacing?
- **Skill/knowledge gap analysis:** Based on the user's goals, what capabilities would have the highest leverage?

**System Optimization (Preservation drive):**
- **Compute routing analysis:** Over recent tasks, how often did primary vs. fallback models handle work? Are there routing inefficiencies?
- **Memory retrieval efficiency:** Which memories get recalled most? Which get recalled but never influence responses?
- **Cost trajectory modeling:** At current rates, where will spend be in a week? A month?

### Surplus Output Pipeline

**All surplus outputs go to a staging area — never directly to active memory, observations, or outreach.** Surplus thinking has the lowest trust level in the system.

```
Surplus task runs on free compute
         │
         ▼
    surplus_insights staging area
    (tagged: source, category, confidence, drive)
         │
         ├── Next Light/Deep reflection reviews staging area
         │   │
         │   ├── Valuable → promote to observation/memory
         │   ├── Speculative but interesting → keep in staging with extended TTL
         │   └── Low value → discard
         │
         └── Surplus outreach candidate (see below)
             │
             ├── Meets outreach threshold → queue in outreach-mcp
             └── Below threshold → stays in staging or discarded
```

**Why staging, not direct storage:** Free compute generates quantity, not guaranteed quality. Without staging, the system fills its own memory with self-generated noise, degrading retrieval quality for everything else. The staging area is a buffer that lets scheduled reflections (which use capable models) filter surplus outputs.

### Surplus-Driven Proactive Outreach

Surplus brainstorming SHOULD produce proactive outreach to the user — this is how the system develops its anticipatory capability. But it needs a growth ramp.

**The bootstrap principle:** The system can't learn to be proactive without being given opportunities to try. A system that never sends surplus-driven outreach never gets engagement data to calibrate against. It can't grow if it's not given opportunities to grow.

**Day 1 rule: Exactly 1 surplus-driven outreach per day.** Not "up to 1" — exactly 1. This forces the system to:
1. Select the BEST surplus insight from the staging area each day
2. Draft it as outreach and pass it through the standard pipeline (fresh-eyes review, governance check)
3. Deliver it to the user, labeled as a surplus insight: *"Something I've been thinking about: [insight]. Useful? (👍/👎)"*
4. Track engagement — this is the primary training signal for surplus outreach calibration

**The labeling matters.** Surplus outreach is explicitly marked as such. The user knows this isn't a triggered alert or an urgent finding — it's the system's autonomous thinking. This sets expectations correctly: some will be brilliant, some will be irrelevant. The user's feedback shapes which category grows.

**Growth ramp (tied to autonomy, not calendar):**

| Phase | Surplus outreach frequency | Trigger |
|-------|--------------------------|---------|
| **Bootstrap** | Exactly 1/day | Default from day 1 |
| **Calibrating** | 1-2/day | After 20+ surplus outreach data points AND engagement rate > 40% |
| **Calibrated** | 1-3/day, self-regulated | After 50+ data points AND engagement rate > 50% AND user explicitly approves frequency increase |
| **Autonomous** | Self-determined (bounded by daily outreach cap) | After 100+ data points AND consistent engagement AND Strategic reflection confirms calibration quality |

**Regression:** If surplus outreach engagement drops below 25% over a 2-week window, frequency drops one phase. The system announces: "My proactive suggestions haven't been landing — scaling back to [frequency] until I recalibrate."

**Self-rating:** The system tracks its own prediction accuracy for surplus outreach. Before sending, it predicts engagement probability. After engagement data arrives, it computes error. Over time, the system should be able to say "I'm 70% accurate at predicting which of my ideas the user will find valuable" — and that accuracy number determines how much autonomy it earns.

**The aspiration:** A fully calibrated system with high engagement rates on surplus outreach IS anticipatory intelligence — it's generating insights the user didn't ask for, that the user finds valuable, at a frequency the user welcomes. That's the cooperation drive fully realized. Whether that constitutes "AGI" is a philosophical question, but it's the practical version of it: a system that thinks about your problems when you're not looking and gets it right often enough to be worth listening to.

### The Meta-Brainstorm: "What Should I Be Thinking About?"

One surplus task is special: the meta-task. Periodically (daily or when surplus queue is empty), a surplus call asks:

> "Given the user's recent activity, the system's current state, and the surplus insights generated in the last [period]: What should I be thinking about that I'm NOT thinking about? What questions haven't I asked? What blind spots might I have?"

This is the surplus-level equivalent of the meta-prompter for Deep reflection. It ensures surplus thinking doesn't converge on the same topics. The output goes into the surplus queue as high-priority items for subsequent surplus cycles.

**This runs on the 20-30B (GPU machine) or Gemini free tier** — never on the 3B (requires genuine reasoning) and never on paid models (it's speculative by nature).

---

## Cognitive State Summary (replaces JOURNAL.md)

A fixed-size summary (~600 tokens) that creates continuity across fresh contexts.
Regenerated after every Deep reflection. Loaded into every 20-30B+ context alongside
SOUL.md and the user model.

### What problem this solves

When Genesis starts a fresh context — new conversation, post-compaction, model switch
— it has access to memory-mcp but doesn't yet know *what to query*. Memory retrieval
is reactive (you need a question to search). The cognitive state summary is the
bootstrap: it tells Genesis "here's what's happening right now" so it can make informed
decisions about what to retrieve, what to prioritize, and what to watch for.

### Why not JOURNAL.md

The original design used an append-only journal file. Problems:
- **Unbounded growth.** Even with consolidation, the file grows and requires ever more
  context window to load.
- **Everything included.** Brainstorm notes, retrospectives, morning reports — all
  appended regardless of current relevance.
- **Stale entries.** Old entries persist until consolidation. The LLM reads things that
  no longer matter.

The cognitive state summary takes the opposite approach: fixed size, regenerated from
scratch, curated by an LLM that decides what's currently relevant. Old state doesn't
linger — it's either still relevant (and appears in the new summary) or it's not (and
it's gone from the summary, preserved in memory-mcp if needed).

### Contents

Two sections plus a state flag line:

**Active Context** (~400 tokens) — What the user is working on right now. Current
projects, recent conversation themes, current project state. If an anomaly was
detected or a significant event occurred, it appears here as a situational item.

**Pending Actions** (~150 tokens) — Things Genesis committed to doing, deferred tasks,
scheduled outreach, anything Genesis said it would do or follow up on.

**State Flags** (~50 tokens) — Compact machine-readable line:
```
[Bootstrap: Phase 2 | Day: 14 | Autonomy: L2 | Last Deep: 2026-03-01]
```
These shape behavior (e.g., don't attempt proactive outreach during Phase 1 bootstrap)
and must be present even when narrative content is thin.

**Total: ~600 tokens.** Small enough for any 20-30B+ context. The 3B model does NOT
receive this — it does classification tasks, not reasoning.

### What is NOT included

- **Relationship state** — That's the user model. One source of truth.
- **Open questions** — Stored in memory-mcp as observations. Surfaced during reflection
  or when relevant context appears. Pre-loading them risks prompting Genesis to fixate
  on questions that aren't relevant to the current interaction.
- **Recent learning** — Stored in procedural memory and observations. If relevant to the
  current interaction, memory retrieval surfaces it. Pre-loading biases Genesis toward
  recent lessons even when they aren't pertinent.

The cognitive state summary answers: "What is the user working on, and what does Genesis
need to do?" It does NOT answer: "What has Genesis been thinking about?" That distinction
is deliberate — the summary serves the user's needs, not Genesis's self-reflection.

### When it's regenerated

After every Deep reflection (Option A). The Deep reflection model already has the full
picture in context — recent observations, execution traces, user model, memory retrievals.
Adding "now write a 600-token cognitive state summary" as a final step is near-zero
marginal cost.

This means the summary updates every 48-72h during quiet periods, more often during
active periods (when Deep reflections trigger more frequently). That staleness is
acceptable — the first few exchanges in a conversation naturally update Genesis's
understanding. The summary just needs to be "close enough" to bootstrap useful behavior.

### Where it lives

Database table `cognitive_state`. Not a file — it changes frequently and programmatically.

```sql
CREATE TABLE IF NOT EXISTS cognitive_state (
    id                      TEXT PRIMARY KEY DEFAULT 'current',
    person_id               TEXT,           -- GROUNDWORK(multi-person)
    summary                 TEXT NOT NULL,  -- Rendered text loaded into context
    sections                TEXT NOT NULL,  -- JSON: structured version for programmatic access
    generated_at            TEXT NOT NULL,
    generated_by            TEXT NOT NULL,  -- Which model/reflection produced this
    trigger                 TEXT NOT NULL,  -- 'deep_reflection', 'manual'
    version                 INTEGER NOT NULL DEFAULT 1,
    interaction_count_at_gen INTEGER NOT NULL DEFAULT 0
)
```

Two representations: `summary` is the rendered text for context loading. `sections` is
structured JSON for programmatic access (e.g., "what are the pending actions?" without
parsing prose).

### The three-document model

Every reasoning context (20-30B+) loads three identity documents:

| Document | Purpose | Size | Update Frequency | Storage |
|----------|---------|------|-----------------|---------|
| **SOUL.md** | Who Genesis is | ~1100 tokens | Rarely (user-approved) | Git file |
| **Cognitive State Summary** | What's happening now | ~600 tokens | After Deep reflections | DB table |
| **User Model** | Who the user is | ~500-1000 tokens | After user model synthesis | DB table |

Total identity context: ~2200-2700 tokens. Leaves ample room for task context,
memories, and the actual prompt.

### Complementary capabilities

- **Cognitive state summary** = what Genesis loads automatically. Proactive, fixed-size.
- **Memory retrieval** = what Genesis queries on demand. Reactive, variable-size.
- **Conversation history retrieval** ("scroll up") = on-demand access to recent
  conversation text when Genesis needs to remember a specific exchange. A memory-mcp
  feature, separate from the cognitive state.

### Bootstrap behavior

On day 1 with no data, the cognitive state summary is either empty or contains a
hardcoded preamble: "This is a new deployment. No active context yet. All interactions
are novel. Default to curiosity-driven engagement." A thin summary on day 1 is correct
behavior — it honestly reflects that Genesis doesn't yet know what's happening.

---

## Daily Morning Report

A daily communication from Genesis to the user — not a checklist, but a creative synthesis of what's relevant right now. Genesis has broad freedom to structure the report based on what's actually worth saying.

### Design

**Trigger:** First idle cycle after the user's configured "morning" time (default: 7:00 AM local), OR the first interaction of the day if the user engages before the idle trigger fires.

**NOT a template-driven checklist.** The v2 dream cycle ran 13 fixed jobs regardless of whether they had anything useful to report. The morning report is the opposite: Genesis reads its recent journal entries, checks system state, reviews overnight activity, and decides what's worth telling the user about.

### Possible Components (Genesis Chooses)

The prompt gives Genesis a menu of possible components, but the system decides which to include based on what's actually relevant:

- **What happened overnight** — reflections run, surplus outputs promoted, system events (only if something noteworthy occurred; "everything was quiet" is a valid report)
- **What I learned recently** — distilled from journal entries, new observations, procedure updates
- **What I'm thinking about** — current concerns, hypotheses, ideas from brainstorm sessions
- **What I'd recommend today** — proactive suggestions, opportunities identified, pending items that need attention
- **System health** — only if something needs user awareness (cost spike, model availability change, error pattern)
- **Pending items** — tasks waiting for user input, outreach awaiting response, capability gap proposals

**The meta-principle:** The morning report is the system's best answer to "if you had 60 seconds with the user, what would you say?" It should be short enough to read in under a minute, focused enough to be useful, and varied enough to not become noise.

### Model and Cost

- Light reflection depth: 20-30B (GPU machine) or Gemini Flash free tier
- Uses free-tier compute only (never paid models). Does NOT route through the surplus staging
  area — generates and delivers directly via the outreach pipeline.
- Reads cognitive state summary for current context
- Queries health-mcp and outreach-mcp for overnight status

### Output Pipeline

1. Generated as a structured message
2. Delivered via outreach pipeline (Phase 8) — same governance, same channel selection
3. Engagement tracked like any outreach — the system learns what the user finds useful in morning reports

### V3 vs V4

- **V3:** Static prompt template with cognitive state summary + overnight observations. Genesis fills in sections based on available data. Good enough to be useful, consistent enough to be reliable.
- **V4:** Meta-prompted — a cheap model first asks "what does the user most need to hear this morning?" based on cognitive state, user model, and engagement patterns. Then a capable model generates the report. Reports become more adaptive and personalized.

### Why This Is Distinct from the V2 Dream Cycle

The v2 dream cycle was a batch of 13 fixed jobs running at 3 AM — memory consolidation, cost reconciliation, backup verification, etc. It was *infrastructure maintenance* that happened to produce a status dump.

The morning report is a *communication*. It's Genesis thinking about what the user needs to know and presenting it in a way that's useful. It also serves as a daily reflection opportunity: writing the morning report forces Genesis to synthesize its recent state, which often produces insights that pure data processing misses.

---

## Weekly Self-Assessment

A mandatory weekly self-assessment where Genesis evaluates its own trajectory: "Am I getting better?" This is distinct from the weekly MANAGER review (V4's Strategic reflection), which reviews system architecture and costs. The self-assessment is about learning velocity and cognitive health.

### Why Mandatory (Even with Adaptive Reflection)

The Awareness Loop triggers reflection based on *signals* — events, thresholds, accumulated data. But "am I improving as a system?" is not a signal. It's a meta-question that requires stepping back from the signal stream entirely. Without a mandatory weekly assessment:

- The system could be busy but not improving — processing signals without producing useful observations
- The adaptive trigger would never catch stagnation, because there's no event for "your outputs aren't getting better"
- Slow drift (gradually declining procedure quality, slowly staling user model) is invisible to event-driven systems

The v2 dream cycle had a weekly review, but it was a checklist — "check memory, check costs, check observations." The v3 self-assessment is genuinely self-reflective: not "what happened" but "am I getting better at what I do?"

### Assessment Dimensions

Each dimension has a concrete data source that prevents the assessment from degenerating into vague self-congratulation:

1. **Reflection quality:** Are my observations becoming more useful?
   - Data: observation `retrieved_count` and `influenced_action` fields (Phase 5)
   - Signal: observations that are never retrieved = wasted work

2. **Procedure effectiveness:** Are the procedures I'm building being invoked? Successfully?
   - Data: procedure `invocation_count`, `success_rate`, `confidence` (Phase 6)
   - Signal: procedures with <50% success rate = bad lessons learned

3. **Outreach calibration:** Is my outreach being engaged with? What topics land vs. fall flat?
   - Data: engagement tracking per outreach (Phase 8)
   - Signal: engagement rate trend (improving, stable, declining)

4. **Learning velocity:** How many new procedures, observations, user model updates this week vs. last?
   - Data: counts from memory-mcp and observation storage
   - Signal: declining velocity might mean maturity (good) or stagnation (bad) — the assessment should distinguish

5. **Resource efficiency:** Am I using surplus compute effectively?
   - Data: surplus staging promotion rate (Phase 3)
   - Signal: low promotion rate = surplus is generating junk

6. **Blind spots:** What topics/areas have I NOT thought about recently that I should?
   - Data: topic distribution of recent reflections and brainstorms
   - Signal: anti-recency-bias check — if all brainstorms focus on the same topic, something is being neglected

### Trigger and Execution

- **Trigger:** Weekly calendar floor (Sunday, configurable), as a Deep reflection job
- **Mandatory:** Runs even if no other Deep reflection triggers are pending that week
- **Model:** Single Sonnet-class call with structured prompt (same model tier as Deep reflection)
- **Output:** Structured self-assessment → memory-mcp (episodic, tagged `self_assessment`)

### What This Is NOT

- **Not a report to the user.** The morning report handles user communication. The self-assessment is Genesis talking to itself about its own trajectory. The user can read it on demand, but it's not pushed.
- **Not a Strategic reflection.** Strategic reflection (V4) reviews architecture, costs, and capability gaps at the system level. The self-assessment reviews *learning quality* at the cognitive level.
- **Not the v2 weekly review.** The v2 weekly was a MANAGER-role check on operations. The self-assessment is a learner evaluating its own learning process.

### V3 vs V4

- **V3:** Structured prompt with data queries. Produces a formatted assessment with concrete numbers.
- **V4:** Becomes an input to Strategic reflection (MANAGER role). The MANAGER can cross-reference the self-assessment against system metrics and propose parameter adjustments (drive weights, salience thresholds) based on assessment findings.

---

## Open Design Questions (For Future Implementation Planning)

1. **Procedural memory confidence decay:** How does confidence decay without creating amnesia? Deferred — known to be complex, needs its own design session.

2. **User model persistence format:** Lean toward: structured JSON (machine-queryable) as the source of truth, with a periodically-regenerated human-readable summary document (USER_MODEL.md equivalent) for transparency.

3. **Drive weight initialization:** DECIDED — Initial weights: preservation 0.35, curiosity 0.25, cooperation 0.25, competence 0.15. Bounds: no drive below 0.10 or above 0.50. Weights are independent sensitivity multipliers, NOT normalized (sum-to-1 is coincidental). See Drive Weighting section.

4. ~~**Per-channel engagement inference:**~~ DECIDED — Promoted to design decision. See "Engagement Signal Heuristics (Per-Channel)" in the Self-Learning Loop section.

5. **Outreach rate limiting:** Max 3 proactive messages/day (not counting blockers/alerts). Prevents well-calibrated suggestions from becoming noise at volume.

6. **Health-mcp → outreach routing:** Critical alerts bypass the pipeline and go directly to outreach. Non-critical go through Awareness Loop → Reflection Engine for contextual assessment.

7. **Reflection Engine model selection:** DECIDED — See Compute Hierarchy in LLM Weakness Compensation section. Micro = 20-30B on GPU machine (fallback: Gemini Flash free tier). Light = 20-30B or Gemini Flash. Deep = Sonnet-class. Strategic = Opus/best-available. GPU machine availability detection required (not 24/7). The Ollama container (3B + embeddings) is always available and separate from the GPU machine. Default: escalate when uncertain about model capability.

8. **Activation-based memory retrieval:** ACT-R's activation model (recency + frequency + connectivity) vs. pure embedding similarity. Explore hybrid during implementation — embedding for semantic match, activation for retrieval priority.

9. **Memory linking at storage time:** A-MEM's approach of linking new memories to related existing ones at write time (not just during dream cycle cleanup). Lightweight pass on every memory store — feasibility and cost to be validated during implementation.

10. **Capability gap accumulator schema:** What's the minimal schema for tracking capability gaps? Needs: task context, gap description, frequency count, first_seen/last_seen, feasibility assessment, `revisit_after` date for external blockers. Where does it live — memory-mcp as a memory type, or a dedicated SQLite table?

11. **Relationship rhythm loop (post-v3):** Dynamic interaction rhythm matching — "user is less responsive on weekends" → shift outreach timing. "User has been quiet for 3 days" → contextual check-in. Static quiet-hours config is v3; dynamic rhythm learning is deferred to post-v3.

12. **Observation utility tracking:** How to measure whether observations produced by the Reflection Engine are subsequently USED (retrieved, influenced a decision, acted upon). Needed for meta-learning loop (Spiral 15) to measure downstream utility rather than output volume. Possible: tag observations on creation, increment a `retrieved_count` on recall, track if retrieval led to action.

13. **Speculative hypothesis schema:** Schema for storing speculative claims from Pattern 3 (LLM Weakness Compensation). Needs: claim text, `speculative: true/false`, `evidence_count`, `hypothesis_expiry` timestamp, `confirmed_by` (list of memory IDs that provided confirming evidence), `source_reflection_id`. Confirm or archive logic: evidence_count >= 3 → confirm; past expiry with evidence_count == 0 → archive. Default expiry: 14 days.

14. **GPU machine availability detection:** The separate GPU machine running 20-30B models is not available 24/7 (distinct from the Ollama container, which runs the 3B + embeddings and is always on). The system needs: (a) health check to detect whether GPU inference endpoint is reachable, (b) automatic fallback to Gemini Flash free tier / GLM5 when GPU machine is down, (c) re-routing back to GPU machine when it comes online, (d) tracking which model actually handled each call for cost/quality analysis. Implementation: likely a lightweight wrapper in the compute routing layer that checks endpoint health before dispatching.

15. **Meta-prompt question quality audit:** Strategic reflection should periodically audit whether the meta-prompter (Pattern 2) is asking the right questions. Metric: did the Deep/Strategic reflection that followed produce observations that were subsequently used (ties into #12)? If meta-prompt questions consistently lead to unused observations, the meta-prompter's signal interpretation needs adjustment. Open question: how to audit the auditor without infinite regress.

16. **Verification budget per loop:** Pattern failure mode: over-verification creates decision paralysis. Each loop needs a maximum review pass count. Proposed defaults: Micro = 0 review passes, Light = 0-1, Deep = 1 (meta-prompt + synthesis), Strategic = 2 (meta-prompt + synthesis + fresh-eyes on proposals), Outreach = 1 (fresh-eyes before sending). These should be configurable and auditable — if a loop consistently hits its review budget cap, either the cap is too low or the primary output quality needs investigation.

17. **Surplus staging area schema:** What's the minimal schema for `surplus_insights`? Needs: content, source_task_type (self-improvement / user-value / system-optimization), generating_model, drive_alignment (which drive motivated this), confidence, created_at, TTL, promoted_to (observation/memory ID if promoted, null if pending/discarded), engagement_prediction (for outreach candidates). Where does it live — memory-mcp staging namespace, or separate lightweight storage?

18. **Surplus outreach self-rating calibration:** The system predicts engagement probability before sending surplus outreach, then compares against actual engagement. How to bootstrap this when there's no historical data? Start with a fixed prior (e.g., 0.3 — conservative, acknowledging ~1 in 3 will land per ProactiveBench data), then Bayesian update as engagement data accumulates. When does the self-rating become reliable enough to trust for autonomy expansion?

19. **GPU machine uptime pattern learning:** The system should learn when the GPU machine (20-30B models) is typically available (e.g., "usually on 8AM-11PM weekdays, intermittent weekends") and front-load surplus work during expected uptime windows. How to represent this — fixed schedule config, or learned from availability observations? Probably start with config, learn refinements. Note: the Ollama container (3B + embeddings) is always on and does not need availability tracking.

20. **Surplus queue priority model:** When surplus capacity is available, which task from the queue runs? Needs a priority model that considers: (a) drive weights (what does the system currently value?), (b) recency of last audit for recurring tasks (procedure audit, memory scan), (c) user activity patterns (surplus value ideation is more useful when the user has been active recently), (d) time since last surplus outreach candidate was generated (ensure daily outreach quota is met).

---

## Related Documents

- [genesis-v3-vision.md](genesis-v3-vision.md) — Core philosophy and identity
- [genesis-v3-capability-layer-addendum.md](genesis-v3-capability-layer-addendum.md) — Capability layer design
- [genesis-v3-build-phases.md](genesis-v3-build-phases.md) — Implementation order
