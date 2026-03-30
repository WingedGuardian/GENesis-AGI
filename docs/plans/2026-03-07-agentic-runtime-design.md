# Agentic Runtime Architecture: Claude Code as Intelligence Layer

**Status:** Design (pending implementation plan)
**Date:** 2026-03-07
**Supersedes:** Portions of `genesis-v3-dual-engine-plan.md` (three-engine model)
**Affects:** `genesis-v3-autonomous-behavior-design.md`, `genesis-agent-zero-integration.md`,
`genesis-v3-model-routing-registry.md`, `model_routing.yaml`

---

## 1. Problem Statement

Genesis requires Opus-quality LLM calls for strategic reflection, quality calibration,
and complex task orchestration. The user's Anthropic API key is stuck at Tier 1
(insufficient for Opus). OpenRouter's Opus access lacks extended thinking and has
quality limitations (max_tokens defaults to 0, no thinking budget control).
Bedrock and Vertex offer no improvement for this use case.

Claude Code (Max subscription) provides unlimited access to Opus and Sonnet with
full extended thinking support. This document redesigns Genesis's runtime architecture
to use Claude Code as the primary intelligence layer.

## 2. Architecture Model

**Core principle:** Claude Code is the brain. Agent Zero is the body.

```
User (Telegram / WhatsApp / Web UI / Terminal)
       |
       v
  AZ Relay Layer (programmatic intent parsing, no LLM)
       |
       v
  CC Foreground Session (user conversation, Sonnet medium thinking)
       |                                    |
       v                                    v
  CC Background Sessions              AZ Infrastructure
  (reflection, task execution)         (dashboard, relay, health)
       |                                    |
       +-----------> Genesis DB <-----------+
                   (shared data layer)
```

**Claude Code** owns all intelligent behavior: user conversation, reflection at all
depths above micro, task planning, task orchestration, quality assessment.

**Agent Zero** owns infrastructure: web dashboard UI, messaging channel relay,
health monitoring, signal collection (awareness loop ticks), and capabilities CC
lacks (computer use in V4+). AZ does NOT make judgment calls or LLM-driven decisions
in V3.

**Shared data layer:** Genesis SQLite database (WAL mode already enabled) accessed
by both runtimes. CC accesses via MCP tools. AZ accesses via direct Python imports.

### 2.1 Why Not Just Claude Code?

Genesis is more than "Claude Code with Telegram." This was explicitly decided when
evaluating gobot (a homemade CC-to-Telegram wrapper). Genesis needs a cognitive
infrastructure layer — awareness loops, signal collection, memory operations,
health monitoring, task dashboards — that CC alone doesn't provide. AZ is that layer.

AZ provides things CC lacks:
- **Web dashboard** for task tracking, memory browsing, system health
- **WebSocket real-time updates** to the UI
- **Messaging channel integration** (Telegram, WhatsApp adapters)
- **Computer use** (screen-level interaction, V4+)
- **Always-on background services** (APScheduler, signal collectors)
- **The existing Genesis infrastructure** (Phases 0-4 already built on AZ)

### 2.2 Why Not Just Agent Zero?

AZ's intelligence is limited to whatever API models are available. With Tier 1
Anthropic access, that means no Opus. CC provides:
- **Opus and Sonnet with extended thinking** (Max subscription, flat rate)
- **Superior tool use** (file I/O, bash, MCP — native, not through a tool-calling layer)
- **Session persistence** (`--resume` for long-running work)
- **Playwright browser automation** (via official plugin)
- **No per-token cost** for any model on the subscription

### 2.3 Known Trade-Off: Router Flexibility Loss

The compute router was originally designed to route the user's interface conversation
across ANY model from any provider. With CC as frontend, user conversations are locked
to Anthropic models (Sonnet/Opus via the Max subscription). This is a real design
principle violation — the router's model-agnostic flexibility was a deliberate feature.

**Why this is acceptable:**
- The user's actual constraint (Tier 1 API, no Opus access) makes the theoretical
  flexibility worthless — you can't route to a model you can't access
- CC provides Opus + extended thinking at flat rate, which no API routing can match
- Background call sites (#17, #20) still use cross-vendor models via API
- The router remains fully functional for all non-CC call sites (micro, light,
  embeddings, tagging, surplus, adversarial)

**What's lost:** The ability to have the user's primary conversation model be
GPT-5.2, Grok 4, or any non-Anthropic model. This is only recoverable by
switching agentic systems entirely (V5 consideration).

### 2.4 Design Principle: Swappability

This architecture is designed for full integration with Claude Code, not a
plugin-per-system abstraction. If a future switch is needed (e.g., to OpenCode),
it would be a full integration effort — not a config change. This is acceptable
because:
- The abstraction cost of supporting multiple agentic systems simultaneously is high
- The benefit of deep integration (system prompts, MCP tools, session management)
  outweighs the switching cost
- V5 is the earliest any switch would be considered
- The shared data layer (Genesis DB + MCP) means the intelligence layer is
  separable from the data layer

## 3. Cognitive Gradient

Model and effort level are both routing dimensions. "Route to Opus" is insufficient —
"Route to Opus with high thinking" specifies the actual capability.

| Depth | Runtime | Model + Effort | Session Type | Clear Frequency |
|-------|---------|---------------|-------------|-----------------|
| Micro | AZ (API) | Free API / LM Studio 30B | N/A | N/A |
| Light | AZ (API) | GLM-5 primary, DeepSeek V4 fallback, Haiku last resort | N/A | N/A |
| Deep | CC background | Sonnet, high thinking | Semi-persistent | Every 4-6 weeks |
| Strategic | CC background | Opus, high thinking | Always fresh | N/A |
| User conversation | CC foreground | Sonnet, medium thinking (default) | Fresh daily | Morning reset |
| Task orchestration | CC foreground (planning) / CC background (execution) | Opus, high thinking (planning) / task-specific (execution) | Per-task | On completion |

### 3.1 Light Reflection Routing

Light reflections stay on API, not CC. Rationale:
- High frequency (up to 1/hour), low stakes, formulaic
- Don't benefit from CC's extended thinking or tool access
- CC CLI has startup overhead (~2-5s) that's wasted on simple observe-and-note calls
- GLM-5 is stronger than Haiku and cheaper

Updated routing chain:
```yaml
4_light_reflection:
  chain: [glm5, deepseek-v4, claude-haiku]
```

### 3.2 Why Sonnet High Thinking for Deep Reflection (Not Opus)

Deep reflection (every 48-72h) involves synthesizing observations, updating cognitive
state, and journaling. It needs thoroughness (which thinking tokens provide) but
rarely needs Opus's ceiling (novel pattern recognition). High-thinking Sonnet gives
extended reasoning chains at ~1/15th the cost impact on rate limits. Opus is reserved
for strategic reflection (weekly) where the ceiling genuinely matters.

### 3.3 User Default: Sonnet Medium Thinking

Sonnet medium thinking balances quality and responsiveness for interactive conversation.
The user can escalate via `/model opus` or `/effort high` for specific conversations.
Starting conservative preserves Opus rate limit capacity for reflection and task
orchestration where it matters most.

## 4. Session Lifecycle

### 4.1 Foreground Sessions

One active foreground CC session per (user, channel) pair. Invoked via
`claude -p --model sonnet --effort medium` with Genesis system prompt and MCP config.

**Morning reset:** At a configurable day boundary (default: user's local midnight
or first interaction of the day), the foreground session starts fresh. Cognitive
state regeneration runs as the handoff mechanism — summarizing the previous day's
context into a compact briefing for the new session. "First message of the day"
is defined as the first user interaction after the configured boundary, not a
wall-clock trigger.

**Session source tagging:** All memories, observations, and execution traces created
during foreground sessions are tagged `source: "foreground"` for context assembly.

### 4.2 Background Sessions

Background CC sessions handle:
- **Deep reflection** (semi-persistent, Sonnet high thinking, clear every 4-6 weeks)
- **Strategic reflection** (always fresh, Opus high thinking, weekly)
- **Quality calibration** (fresh, Opus high thinking, weekly)
- **Weekly self-assessment** (fresh, Opus high thinking, weekly)
- **Task execution** (per-task sessions, model/effort varies by task complexity)

Background sessions are tagged `source: "background"` on all outputs.

**Sonnet 4.6's 1M context window** makes semi-persistent deep reflection sessions
viable — at ~2-4K tokens per cycle every 48-72h, the window holds months of context
before needing a clear.

### 4.3 Session Lifecycle Manager

A component (likely an AZ extension or standalone service) that:
- Tracks active foreground sessions per (user, channel)
- Manages background session scheduling (triggers CC invocations at appropriate times)
- Handles morning reset (detects day boundary, triggers cognitive state regen)
- Monitors session health (detects hung sessions, enforces timeouts)

## 5. Call Site Routing Map

Revised routing for all 28 call sites in the model routing registry.

### 5.1 Stays on API (AZ runtime)

| # | Call Site | Chain | Rationale |
|---|----------|-------|-----------|
| 2 | Triage | [ollama-3b, mistral-free, groq-free] | Classification, 3B sufficient |
| 3 | Micro reflection | [groq-free, mistral-free] | Free API, formulaic |
| 4 | Light reflection | [glm5, deepseek-v4, claude-haiku] | Stronger+cheaper than Haiku-first |
| 8 | Memory consolidation | [mistral-free, groq-free, gemini-free, openrouter-free] | Free, background |
| 9 | Fact extraction | [mistral-free, groq-free, gemini-free, openrouter-free] | Free, formulaic |
| 12 | Surplus brainstorm | [mistral-free, groq-free, gemini-free, openrouter-free] | Free only, never pays |
| 13 | Morning report | [mistral-free, groq-free, gemini-free] | Free, can fallback |
| 17 | Fresh-eyes review | [grok-4, kimi-2.5, gpt-5.2] | Cross-vendor mandatory |
| 19 | Outreach draft | [mistral-free, groq-free, gemini-free, openrouter-free] | Free |
| 20 | Adversarial counterargument | [grok-4, kimi-2.5, gpt-5.2] | Cross-vendor mandatory |
| 21 | Embeddings | [ollama-embedding] | Local, always |
| 22 | Tagging | [ollama-3b, mistral-free, groq-free] | Classification |

### 5.2 Moves to CC Background

| # | Call Site | Model + Effort | Rationale |
|---|----------|---------------|-----------|
| 5 | Deep reflection | Sonnet, high thinking | Extended reasoning, semi-persistent |
| 6 | Strategic reflection | Opus, high thinking | Frontier judgment, always fresh |
| 10 | Cognitive state | Sonnet, high thinking | Synthesis, benefits from deep context |
| 11 | User model synthesis | Opus, high thinking | Complex behavioral modeling |
| 14 | Weekly self-assessment | Opus, high thinking | Anti-sycophancy, needs ceiling |
| 16 | Quality calibration | Opus, high thinking | Anti-sycophancy, needs ceiling |

### 5.3 Absorbed into CC Foreground

| # | Call Site | How | Rationale |
|---|----------|-----|-----------|
| 27 | Pre-execution assessment | System prompt instruction | CC already reasons about requests natively |

### 5.4 Moves to CC (Foreground or Background, Context-Dependent)

| # | Call Site | Context | Rationale |
|---|----------|---------|-----------|
| 7 | Task retrospective | Background CC after task completion | Needs execution context |
| 15 | Triage calibration | Background CC during reflection | Analytical, benefits from thinking |
| 18 | Meta-prompting | V4 feature, defer | Static templates in V3 |
| 28 | Observation sweep | Background CC | Analytical sweep of recent data |

### 5.5 Cross-Vendor Integrity

Call sites #17 (fresh-eyes review) and #20 (adversarial counterargument) MUST use
non-Anthropic models. The design doc specifies:
- #17: GPT-5.2 or Kimi 2.5 (switchable), cross-vendor review of Genesis's reasoning
- #20: Grok 4 primary, Kimi 2.5 and GPT-5.2 rotatable, devil's advocate

These stay on API regardless of CC availability. Claude cannot review Claude's work
for anti-sycophancy purposes.

## 6. Relay Layer

AZ mediates between messaging channels (Telegram, WhatsApp) and CC sessions.

### 6.1 Architecture

```
Telegram/WhatsApp → AZ Channel Adapter → Intent Parser → CC CLI invocation
                                                      ↑
                                              (programmatic, no LLM)
```

The intent parser handles structured commands without an LLM in the critical path.
It supports both slash commands and natural language variations:

**Slash commands (exact match):**
- `/model opus` → `--model opus` flag on next CC invocation
- `/effort high` → `--effort high` flag
- `/resume` → `--resume <session_id>` flag
- `/task` → triggers task planning flow

**Natural language variations (keyword matching, no LLM):**
- "switch to opus" / "use opus" / "change model to opus" → same as `/model opus`
- "think harder" / "high effort" / "more thinking" → same as `/effort high`
- "go back to our last conversation" / "resume" → same as `/resume`

The parser is thorough but not overly specific — it matches common phrasings
via keyword/pattern matching, not an LLM call. The local SLM is too slow for
real-time intent parsing in the critical path. Unrecognized text passes through
as a regular prompt to CC.

**Regular text** → passed as prompt to CC session

### 6.2 Voice Messages

Voice messages follow: Telegram audio → Groq Whisper API (transcription) → relay →
CC as text. No special handling needed beyond transcription. Groq's Whisper endpoint
is fast and free-tier friendly.

### 6.3 Response Formatting

CC output is formatted for the target channel:
- **Terminal:** Raw output (no transformation)
- **Telegram/WhatsApp:** Markdown → channel-native formatting, long responses split
  into multiple messages, code blocks preserved
- **Web UI:** Full markdown rendering

## 7. Cross-Runtime Communication

### 7.1 Message Queue

New database table for CC ↔ AZ ↔ User communication:

```sql
CREATE TABLE message_queue (
    id TEXT PRIMARY KEY,
    task_id TEXT,                    -- associated task (nullable for non-task messages)
    source TEXT NOT NULL,            -- 'cc_foreground', 'cc_background', 'az', 'user'
    target TEXT NOT NULL,            -- 'user', 'cc_foreground', 'cc_background', 'az'
    message_type TEXT NOT NULL CHECK (message_type IN (
        'question', 'decision', 'error', 'finding', 'completion', 'progress'
    )),
    priority TEXT NOT NULL DEFAULT 'medium' CHECK (priority IN (
        'high', 'medium', 'low'
    )),
    content TEXT NOT NULL,           -- JSON: {text, options[], context}
    response TEXT,                   -- JSON: user's response when answered
    session_id TEXT,                 -- CC session ID for checkpoint-and-resume
    created_at TEXT NOT NULL,
    responded_at TEXT,
    expired_at TEXT
)
```

### 7.2 Message Types and Routing

| Type | Priority | Push to User? | CC Behavior |
|------|----------|--------------|-------------|
| question | High | Yes, immediately | Checkpoint + exit, resume on response |
| decision | High | Yes, with numbered options | Checkpoint + exit, resume on response |
| error | High | Yes, with recovery options | Checkpoint + exit, resume on response |
| finding | Medium | Yes, at next natural break | No checkpoint, informational only |
| completion | Medium | Yes | No checkpoint, task done |
| progress | Low | No (web UI only) | No checkpoint, status update |

**Critical rule:** Blockers are NOT progress updates. If a task encounters a blocker
that stops execution, that's an `error` type message pushed to the user immediately,
even if there's no explicit question. Silent failures are prohibited.

### 7.3 Universal Checkpoint-and-Resume

All user-facing questions use the same pattern regardless of context:

1. CC writes message to queue with `session_id`
2. CC saves state and exits
3. AZ relay detects new message, formats for channel, sends to user
4. User responds (could be 5 seconds or 5 hours)
5. AZ relay writes response to queue
6. AZ triggers `claude -p --resume <session_id>` with response injected
7. CC picks up where it left off

This eliminates the distinction between "interactive" and "async" — the system
always assumes the user might not respond immediately.

### 7.4 Telegram/WhatsApp Message Formatting

Questions and decisions are formatted for quick mobile interaction:

```
[Task: Vehicle Registration]

The DMV site is asking for your VIN number. I can see two vehicles on file:
1. 2022 Honda Civic (ending ...4829)
2. 2019 Toyota RAV4 (ending ...7712)

Which vehicle? Or type the VIN directly.
```

Users can reply with just "1" or "2" for numbered options.

Progress updates go to the web dashboard only, not pushed to chat. The user can
check the dashboard if they want status. Only blockers, errors, questions, and
completions are pushed.

## 8. Task Planning and Execution

### 8.1 Task Initiation Flow

Tasks are NOT auto-detected. The user explicitly confirms task intent:

```
User: "Can you renew my vehicle registration?"

CC (Sonnet, medium): "I can help with that. This will involve navigating
the DMV portal, filling forms, and making a payment. Want me to build a
formal task plan for this, or should I just walk you through it live?"

User: "Yeah, make a task out of it."

CC: Spawns separate Opus+high thinking invocation for intake
```

The foreground CC (Sonnet) proposes task creation. The user confirms. Only then
does the system escalate to Opus for formal planning. This prevents jarring
mode switches on simple requests.

### 8.2 Planning Phase

A separate CC invocation runs at Opus + high thinking:

1. **Structured intake interview** — asks the minimum questions needed to scope
   the task (vehicle, payment method, deadline, preferences)
2. **Produces goal + constraints plan** — NOT a rigid step-by-step, but a goal
   with decision framework for unknowns:

```
Goal: Renew vehicle registration for 2022 Honda Civic
Constraints:
  - Budget: up to $100
  - Payment: Visa ending 4829
  - Deadline: before March 15
Decision framework:
  - If multiple renewal options: choose 1-year unless 2-year saves >15%
  - If CAPTCHA or human verification: checkpoint, ask user
  - If unexpected fee or requirement: checkpoint, ask user
  - If site is down: retry in 30 min, max 3 retries, then notify user
Known unknowns:
  - Site flow may vary by state
  - May require documents we don't have
```

3. **Shows plan to user for approval**
4. **On approval:** writes plan to `execution_traces` table, hands off to
   background CC for execution
5. **Foreground CC drops back** to Sonnet medium thinking, user continues
   chatting about other things

### 8.3 Execution Phase

Background CC picks up the plan and executes. The executing session's model and
effort level are task-specific:
- Simple code task → Sonnet, low thinking
- Complex multi-step task → Sonnet, high thinking
- Task requiring frontier reasoning → Opus, medium or high thinking

The orchestrator (which planned the task at Opus) specifies the execution
model+effort in the handoff.

During execution, the background CC:
- Handles code work directly (file I/O, bash)
- Handles browser work via Playwright MCP plugin
- Handles research via web search/fetch
- Writes progress updates to message queue (web UI only)
- Checkpoints on blockers, questions, or decisions (push to user)
- Writes execution trace updates to database
- On completion, writes final result + runs quality gate

### 8.4 Fluid Execution for Unknown Situations

For tasks with significant unknowns (DMV navigation, web scraping, research),
the plan is a goal + constraints framework, not a rigid checklist. The executing
CC session is smart enough to navigate unknowns within the decision framework.
When it encounters something genuinely outside the framework, it checkpoints
and asks the user.

This works because CC with thinking enabled naturally reasons about next steps,
evaluates what it discovers, and adjusts its approach — exactly what's needed
for fluid situations.

### 8.5 AZ Delegation (V4)

In V3, CC + Playwright covers most execution needs. If CC discovers it needs a
capability only AZ has (computer use), it writes a sub-task to a delegation
queue. AZ picks it up, executes, writes result back. CC resumes with the result.

For V3, this delegation path is designed but not built. CC handles everything
directly. V4 wires the delegation when computer use and other AZ-specific
capabilities become needed.

## 9. Data Layer: Write Contention

### 9.1 SQLite WAL Mode (Already Active)

WAL mode is already enabled (`src/genesis/db/connection.py:23`):
```python
await db.execute("PRAGMA journal_mode=WAL")
```

WAL allows concurrent readers with a single writer without blocking. Reads never
block in WAL mode regardless of concurrent writes. The data being read (cognitive
state, observations, task status) is small and SQLite reads are microsecond-level.
No in-memory read cache is needed — WAL already eliminates read contention.

### 9.2 Write Queue (V4 Defense in Depth)

For the rarer case of multiple writers contending simultaneously (CC foreground +
CC background + AZ all writing at once), a write queue through MCP servers can
serialize writes. This is V4 — WAL + aiosqlite's SQLITE_BUSY retry covers V3
contention scenarios.

## 10. Scope Boundaries

### 10.1 V3 Scope (This Implementation)

- CC foreground session for user conversation (Sonnet medium default)
- CC background sessions for deep/strategic reflection
- Relay layer: Telegram/WhatsApp → AZ → CC (basic, text only)
- Message queue for cross-runtime communication
- Basic task handoff (foreground plans, background executes)
- Call site routing updates (API vs CC assignment)
- Session lifecycle manager (morning reset, session tracking)
- Cognitive state regen as session handoff mechanism
- Voice message transcription (Groq Whisper → text → relay)

### 10.2 V4 Scope (Future)

- Full task system with AZ delegation for computer use
- Complex multi-agent task execution
- Write queue for SQLite contention under heavy load
- Channel learning (which channels user prefers for which message types)
- Progress verbosity preferences (user-configurable)
- Session analytics (which model+effort combos produce best outcomes)
- AZ sub-task delegation (CC → AZ for capabilities CC lacks)

### 10.3 V5 Scope (Future)

- Agentic system swappability evaluation (CC → OpenCode if warranted)
- Advanced orchestration topologies (peer-to-peer sub-agents)
- Identity evolution across sessions

## 11. Impact on Existing Documents

Every document listed below contains references that are now outdated or need
amendment. The architectural shift is: CC replaces AZ as the intelligence layer,
Claude SDK is no longer a separate engine, OpenCode is deferred to V5, the
"two model slots" concept is replaced by the cognitive gradient, and the
orchestrator is CC (not AZ's main agent).

### 11.1 `docs/architecture/genesis-v3-dual-engine-plan.md` (SUPERSEDED)

The three-engine model (AZ + Claude SDK + OpenCode) is fully superseded by the
dual-runtime model (CC + AZ). All references to Claude SDK as a "power tool,"
OpenCode as "backup," cost-routing between engines, and AZ as "brain +
orchestrator" are replaced by this document.

**Key outdated content:**
- Line 1: "Agent Zero + Claude SDK Architecture Plan" — wrong framing
- Lines 25-27: "Agent Zero is the brain" — CC is now the brain
- Lines 59-60: `chat_model`/`utility_model` slots — replaced by cognitive gradient
- Lines 80-82: `claude_code` tool + `opencode_fallback` — CC replaces both
- Lines 120-122: Three-engine role table — superseded
- Lines 129-210: Code execution economics — CC subscription changes all cost math

**Action:** Add supersession notice at top pointing to this document. Keep as
historical record (do not delete).

### 11.2 `docs/architecture/genesis-v3-autonomous-behavior-design.md` (AMEND)

The primary design doc. Design intent remains valid but implementation details
change significantly.

**Key sections needing amendment:**
- Line 11: References "dual-engine plan" — update reference
- Line 40: "Genesis is the orchestrator" — still true, but "Genesis" is now CC,
  not AZ's main agent
- Lines 1144-1150: Task Execution section — orchestrator is CC, not AZ
- Lines 1235: `claude_code tool` — CC handles code directly, not via tool
- Lines 1257-1269: All "Claude SDK" references — CC replaces
- Lines 1288: Graceful degradation chain — rewrite for CC-first routing
- Lines 1372-1437: Multi-agent coordination — CC spawns work, not AZ sub-agents
- Lines 1456-1459: Execution trace `claude_code` type — update
- Lines 1536-1561: Code task routing table + cost estimation — CC subscription
  changes economics
- Lines 1578-1607: Memory separation section — CC sessions replace Claude SDK
  sessions. "Dynamic CLAUDE.md" concept still applies but for CC background
  sessions, not Claude SDK tool invocations
- Lines 2076-2080: "CLAUDE.md Handshake Cycle" — reframe for CC sessions
- Line 2257: "Orchestration prompt quality" — still true, applies to CC system
  prompts now
- Lines 2375+: Architecture diagram — update for CC-as-brain
- Lines 2470-2480: Anti-sycophancy table — update engine references

**Action:** Add addendum section at top noting CC-as-orchestrator per this
document. Update specific line ranges inline.

### 11.3 `docs/architecture/genesis-agent-zero-integration.md` (MAJOR REVISION)

AZ's role shifts from "brain + body" to "body only." This is the most impacted
integration doc.

**Key sections needing revision:**
- Lines 40+: "Genesis is the orchestrator" context — AZ is infrastructure now
- Lines 143-166: `unified_call()` section — still exists but AZ no longer makes
  judgment calls. `unified_call()` serves micro/light reflections and API-routed
  call sites only
- Lines 177-182: Extension hooks for intercepting `chat_model` calls — less
  relevant since CC handles the judgment calls
- Lines 193-194: Two model slots table — replaced by cognitive gradient
- Lines 198-201: "awareness loop calls utility_model" — still true for micro
- Lines 209-233: Model configuration and reflection depth table — fully
  superseded by Section 3 of this document
- Lines 515-524: Architecture diagram with `utility_model`/`chat_model` — redraw

**Action:** Add new section on revised AZ role. Update model slot references
to cognitive gradient. Keep `unified_call()` section but scope it to API-routed
call sites only.

### 11.4 `docs/architecture/genesis-v3-model-routing-registry.md` (UPDATE)

Call site routing assignments change per Section 5 of this document.

**Action:** Add "Runtime" column (API / CC-foreground / CC-background) to the
main registry table. Update light reflection chain. Note which call sites move
to CC. Add effort level as a new dimension.

### 11.5 `docs/architecture/genesis-v3-build-phases.md` (UPDATE)

Build phases reference `unified_call()`, Claude SDK, and OpenCode in several
places.

**Key updates:**
- Lines 183-197: Phase 2 routing references — scope to API-routed call sites
- Phase 8/9: Task execution phases — CC is orchestrator, not AZ
- Scope fence section: Add CC integration as V3 scope

**Action:** Update phase descriptions to reflect CC-as-brain. Add CC integration
tasks to appropriate phases.

### 11.6 `docs/architecture/genesis-v3-gap-assessment.md` (UPDATE)

References Claude SDK economics and three-engine architecture.

**Key updates:**
- Line 22: "Sequential queuing contradicts delegation" — CC handles delegation now
- Line 27: "Escape hatch frameworks" — CC is the framework, OpenCode deferred
- Lines 245-269: Claude SDK cost section (R5) — fully superseded by CC subscription
- Lines 397-400: Task routing percentages — reframe for CC

**Action:** Update gap items for CC-as-brain architecture.

### 11.7 `docs/architecture/genesis-v3-capability-layer-addendum.md` (UPDATE)

References "Hybrid Orchestrator" identity and Claude Code/OpenCode as tools.

**Key updates:**
- Line 23: "Hybrid Orchestrator" — still valid concept, but orchestrator is CC
- Line 59: "Code work → Claude Code / OpenCode" — CC handles directly
- Line 332: Cursor/OpenClaw references — defer to V5

**Action:** Minor updates to reflect CC-as-primary.

### 11.8 `docs/architecture/genesis-deferred-integrations.md` (UPDATE)

References `claude_code` tool integration as V4 feature.

**Key updates:**
- Lines 70, 158, 165: `claude_code` tool references — CC replaces this concept
- Line 315: OpenCode as code understanding alternative — defer to V5
- Line 337: CloudFormation via `claude_code` — CC handles directly

**Action:** Reframe deferred integrations for CC-as-brain.

### 11.9 `CLAUDE.md` (UPDATE)

Contains "Two model slots" reference that is now outdated.

**Key update:**
- Lines 139-140: "Two model slots — chat_model (Sonnet)... utility_model (SLM)"
  — replace with cognitive gradient reference

**Action:** Update Agent Zero Integration Rules section.

### 11.10 `config/model_routing.yaml` (UPDATE)

Light reflection chain and call site comments need updating.

**Action:**
- Change `4_light_reflection` chain to `[glm5, deepseek-v4, claude-haiku]`
- Add comments noting which call sites are CC-routed (for documentation;
  CC-routed sites don't need YAML entries but should be noted)
- Update `gpt-5-nano` placeholder comments for #17/#20 with correct models

### 11.11 Source Code (MINOR)

Three source files reference `unified_call` or orchestration concepts:
- `src/genesis/awareness/loop.py`
- `src/genesis/perception/engine.py`
- `src/genesis/surplus/scheduler.py`

These use the existing router which still handles API-routed call sites. No
immediate code changes needed — the router continues to serve micro, light,
embeddings, tagging, surplus, and cross-vendor call sites. CC-routed call sites
are new code, not modifications to existing router code.

### 11.12 `docs/architecture/genesis-v3-builder-claude-md.md` (INVERT)

Describes the CLAUDE.md handshake protocol where AZ writes dynamic CLAUDE.md
for Claude SDK to read. This is now inverted: CC is the primary writer (it
maintains CLAUDE.md as its working context), AZ reads selectively for relay
coordination and health checks.

**Action:** Reverse handshake direction. CC → AZ, not AZ → SDK.

### 11.13 `~/.claude/projects/.../memory/MEMORY.md` (UPDATE)

Auto-memory contains outdated references to "Two model slots" and Phase 4
completion notes that reference the old architecture.

**Action:** Update after all doc changes are committed.

## 12. Open Questions

1. **CC CLI rate limits under Max subscription:** What are the actual rate limits
   for background CC invocations? If background sessions (reflection + task
   execution) compete with foreground sessions for rate limits, we may need
   scheduling coordination.

2. **CC session startup latency:** The ~2-5s CLI startup overhead is acceptable
   for task execution and reflection but may be noticeable for foreground
   conversation in chat apps. Needs measurement.

3. **MCP tool availability in `claude -p` mode:** Verify that all MCP tools
   (including Playwright) are available when CC is invoked programmatically
   via `claude -p --mcp-config <path>`.

4. **Checkpoint-and-resume reliability:** The `--resume` flag needs testing for
   long-duration pauses (hours between checkpoint and resume). Does session
   state persist reliably?

5. **AZ extension for CC invocation:** How exactly does AZ invoke CC? Options:
   subprocess (`claude -p`), or a dedicated extension that manages CC lifecycle.
   Subprocess is simpler; dedicated extension gives better lifecycle control.

6. **Voice message latency:** Groq Whisper transcription + relay + CC invocation
   adds latency to voice interactions. Acceptable for async chat, may be
   noticeable for rapid voice exchanges.

7. **CC session environment configuration:** Each session type (foreground,
   reflection, task, surplus) needs its own environment: MCP servers, hooks,
   skills, CLAUDE.md content. A `session_config.py` module should generate
   per-session-type configs. See `docs/plans/2026-03-08-research-insights-and-followups.md`
   Section 13 for full gap analysis. Blocks GL-3.

8. **Sub-agent memory harvesting:** CC sessions should return not just explicit
   results but incidental learnings. Three mechanisms: structured debrief
   (prompt engineering), auto-memory harvesting (read CC's auto-memory after
   session), cross-run context injection (inject relevant past learnings).
   See research doc Section 11. Phase 6 work.

9. **CC `/loop` for long-running tasks:** Background CC sessions doing multi-step
   work should use CC's `/loop` skill internally for self-monitoring. Genesis's
   awareness loop is the outer supervisor; CC's `/loop` is the inner task-level
   monitor. See research doc Section 12.
