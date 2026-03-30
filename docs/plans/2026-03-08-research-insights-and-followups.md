# Research Insights & Follow-Up Items — 2026-03-08

Living document capturing research findings, competitive analysis, and action
items from ongoing technology evaluation. Updated as conversations progress.

---

## 1. Xiaomi Miclaw — Competitive Architecture Analysis

### What Miclaw Is

Xiaomi's system-level AI agent for smartphones, announced March 2026 (closed
beta on Xiaomi 17 series). Powered by Xiaomi's proprietary MiMo LLM. Integrates
at the HyperOS level — not an app, but an OS-native capability with direct
access to system functions, apps, and IoT ecosystem.

### Architecture Comparison: Miclaw vs Genesis

| Capability | Miclaw | Genesis v3 | Assessment |
|-----------|--------|------------|------------|
| **LLM backbone** | MiMo-7B-RL (proprietary, 7B params) | Multi-model routed (Claude, Gemini, free APIs, local SLMs) | Genesis: vastly more capable. MiMo is a 7B model — good at intent classification and tool selection, but fundamentally limited in working memory and generalization. Claims to match o1-mini on math/code benchmarks, but benchmark performance ≠ real-world agent reasoning. Miclaw's impressive demos are scaffolding-driven (50+ tools, inference loop), not model-driven. Genesis routes to Opus/Sonnet (orders of magnitude more capable) for deep reasoning. |
| **System access** | OS-level (50+ tools, expandable) | Tool-level via MCP (24 tools across 4 servers) | Miclaw: deeper. See "OS-Level Access" section below. |
| **Memory** | Three-tier: decision points, compressed conversations, cached instructions. Token optimization 50-90%. | ACT-R activation scoring, Qdrant vector store, FTS5 full-text, auto-linking, hybrid retrieval (RRF). | Genesis: more sophisticated retrieval. Miclaw: better compression/efficiency focus. We should study their token optimization — 50-90% reduction in extended interactions is significant. |
| **Sub-agents** | Specialized sub-agents for different task types | CC session dispatch (deep reflection, task execution, surplus) | Similar architecture. Miclaw's sub-agents are specialized by domain (schedule, news, etc.); ours are specialized by cognitive depth. |
| **Context/perception** | On-screen context, user habits, history | Depth-scoped context assembly (Micro→Light→Deep), cognitive state tracking | Genesis: more nuanced depth model. Miclaw: richer real-time context (screen state, physical environment via IoT). |
| **Self-evolution** | Adapts to user habits, compresses learnings locally | User model evolver (Phase 5), procedure learning (Phase 6) | Comparable goals. Miclaw ships it; we're building it. |
| **Privacy model** | On-device processing, runtime auth, 60s auto-reject for risky actions, no training on user data | Local SLM for micro-reflection, user confirmation for outreach (Phase 8) | Miclaw: more mature. Their per-action confirmation with auto-reject timeout is a good pattern we should adopt. |
| **Developer ecosystem** | MCP support, SDK for third-party capability declaration | MCP-native (4 servers), plugin architecture (via AZ) | Both use MCP. Miclaw's SDK for apps to "declare capabilities" is interesting — could inspire how Genesis discovers new tools. |
| **Inference loop** | Reasoning-execution loop: analyze → select tool → execute → monitor results (async) | Awareness tick → signal collection → depth classification → reflection → action | Similar continuous loops. Miclaw's is more reactive (command-driven); ours is more proactive (tick-driven, self-initiated). |
| **IoT/environment** | Mi Home (1B+ devices), "human-car-home" ecosystem | None (server-only) | Miclaw: clear advantage. See "Environmental Awareness" below. |

### What Miclaw Does Better

1. **Token efficiency.** 50-90% reduction in extended interactions via
   compressed conversation caching. Our memory system focuses on retrieval
   quality (RRF, activation scoring) but doesn't explicitly compress for token
   efficiency. This matters for cost and context window management.

2. **Per-action confirmation UX.** 60-second auto-reject timeouts for risky
   actions. Clean, predictable governance. Our Phase 8 governance design should
   adopt this pattern — timed confirmation with safe defaults.

3. **Tool discovery via SDK.** Third-party apps declare capabilities that Miclaw
   can discover and invoke. Our MCP tools are statically defined. A dynamic
   tool discovery protocol (apps/services registering capabilities at runtime)
   would make Genesis more adaptable.

4. **Environmental awareness.** Physical environment data (temperature, lights,
   occupancy) from IoT devices gives Miclaw context we completely lack. See
   "Environmental Awareness" section.

### What Genesis Does Better

1. **Multi-model routing.** Miclaw is locked to MiMo. Genesis routes across
   providers (Claude, Gemini, Groq, Mistral, local SLMs) with circuit breaking,
   degradation tracking, and budget gating. If MiMo goes down or hits a
   capability limit, Miclaw has no fallback. We do.

2. **Cognitive depth model.** Miclaw's inference loop is flat — one reasoning
   depth for all tasks. Genesis has explicit depth classification
   (Micro→Light→Deep→Strategic) with different compute budgets and context
   scopes per depth. This means Genesis can think harder about hard problems
   and stay cheap on easy ones.

3. **Proactive intelligence.** Miclaw is primarily reactive (responds to
   commands, though it can suggest). Genesis's awareness loop runs autonomously
   on a tick schedule, detecting situations worth acting on even when the user
   hasn't asked. The surplus system compounds intelligence during idle time.
   This is a fundamental architectural difference.

4. **Memory retrieval sophistication.** ACT-R activation scoring + hybrid
   retrieval (vector + FTS5 + RRF fusion) is more theoretically grounded than
   Miclaw's three-tier cache. Our memory gets *more relevant* over time through
   activation decay and reinforcement. Theirs gets *more compressed*.

5. **Transparent identity files.** CAPS markdown convention (SOUL.md, USER.md)
   means every aspect of Genesis's judgment is auditable and editable by the
   user. Miclaw's decision-making is opaque (proprietary model, no user-visible
   prompt engineering surface).

### OS-Level Access — Should Genesis Have It?

**Current state:** Genesis runs in an unprivileged Incus container on Ubuntu
24.04. It interacts with the world through MCP tools, AZ's code execution
environment, and CLI commands. No direct OS-level access.

**What OS-level access would enable:**
- System monitoring (CPU, memory, disk, network) as signal inputs for awareness
- Process management (start/stop services, manage daemons)
- File system watching (react to file changes across the server)
- Network management (firewall rules, port forwarding, DNS)
- Package management (install/update software autonomously)
- Cron/systemd integration (schedule its own recurring tasks natively)
- Direct hardware access (USB devices, serial ports if connected)

**What it would NOT require:**
- We don't need to modify the host OS kernel or boot process
- We don't need a custom OS (unlike Xiaomi building into HyperOS)
- We already have a dedicated server — Genesis IS the primary workload

**Advantages:**
- Richer signal collection for awareness loop (system metrics, service health)
- Self-maintenance capability (restart services, rotate logs, manage storage)
- Deeper integration with server infrastructure (Qdrant, Ollama, AZ processes)
- Could manage its own container (memory limits, network, storage)
- Home automation potential (Home Assistant integration, see below)

**Risks:**
- Security: an autonomous agent with root access is a significant attack surface
- Stability: a misconfigured system command could brick the container
- Blast radius: OS-level mistakes are harder to undo than tool-level mistakes
- Privilege escalation: if Genesis is compromised, attacker gets OS access

**Recommended approach (V3→V4):**
- V3: Read-only system awareness via signal collectors (CPU, memory, disk,
  service status). No write access to system config.
- V4: Graduated privilege model. Genesis can *propose* system changes (restart
  service, adjust config) but user must approve. Similar to Miclaw's per-action
  confirmation with timeout.
- V5: Full OS-level autonomy with safety constraints (whitelist of allowed
  operations, rollback capability, change logging).

### Environmental Awareness — Home Automation Potential

Genesis runs on a home server (Proxmox). The user has a home network with IoT
potential. Miclaw integrates with Mi Home (1B+ devices).

**Opportunity:** An MCP server for Home Assistant would give Genesis
environmental context:
- Temperature, humidity, light levels in rooms
- Occupancy (motion sensors, device presence)
- Energy usage patterns
- Door/window state
- Weather data

**Why this matters for Genesis (not just novelty):**
- Morning report could include: "House was 18°C overnight, heating came on 3x.
  Your energy bill is tracking 15% higher than last month."
- Proactive: "You have a meeting in 30 minutes. Office light is off and temp
  is 16°C. Want me to warm it up?"
- Signal input: Physical environment data feeds the awareness loop, making
  Genesis more situationally aware.

**Status:** V4 discussion item. Architecture supports it (MCP adapter pattern).
No V3 work required.

---

## 2. Harness Engineering — Preparing for AI Agents

Concept from OpenAI's Symphony: codebases must be structured for machine
readability for AI agents to work effectively.

### What Harness Engineering Means for Genesis

Genesis itself must be "harness-engineered" — not just for our own development
convenience, but because Genesis will eventually work on its own codebase
autonomously (self-improvement, V5).

**Current state — what we do well:**
- Strong test coverage (743 tests, comprehensive per-module)
- Clean module boundaries (genesis.routing, genesis.perception, genesis.memory, etc.)
- Type annotations throughout (frozen dataclasses, enums, protocols)
- CAPS markdown for all LLM-facing content (machine-readable behavior config)
- CLAUDE.md with explicit conventions and rules
- Git hooks for safety (pre-commit blocks secrets, pre-push blocks force to main)

**Gaps to address:**
- [ ] Machine-readable task definitions — our build phases are in prose markdown.
  Symphony uses `workflow.md` in-repo with structured task specs. We should
  consider a structured format for phase/step definitions that an agent could
  parse and execute.
- [x] Proof of Work for autonomous tasks — when Genesis completes a task in
  Phase 9, what constitutes proof? Currently: tests pass. Should also include:
  lint clean, type check clean, diff review, explanation of changes.
  (done — src/genesis/autonomy/verification.py VerificationRunner)
- [x] Self-describing tool capabilities — our MCP tools have docstrings, but
  no structured capability manifest. Miclaw's SDK pattern (apps declare
  capabilities) suggests we should have a machine-readable tool registry.
  (done — src/genesis/learning/tool_discovery.py)
- [x] Error classification — when a tool fails, is it a transient error
  (retry), a capability gap (route differently), or a permanent failure
  (escalate to user)? This taxonomy should be explicit.
  (done — src/genesis/routing/types.py ErrorCategory + retry.py)
- [ ] Documentation as code — architecture docs should be versioned and
  machine-parseable alongside the code they describe, not just human-readable
  prose.

---

## 3. Cross-Model Tool Routing

### The YouTube Lesson

Claude Code cannot access YouTube video content. Gemini can (native YouTube URL
support, processes audio + visual frames). This gap was discovered empirically
when attempting to summarize a video.

**Pattern:** When a content type is inaccessible to the current model, route to
a model that CAN access it before falling back to asking the user.

### Implementation Plan

Add to `config/model_routing.yaml`:
```yaml
youtube_summarize:
  description: "Summarize/analyze YouTube video content"
  tier: utility
  chain:
    - provider: google-gemini
      model: gemini-2.5-flash
      reason: "Native YouTube URL support, processes audio + visual frames"
  notes: "Free tier: 250 RPD, 8h video/day. Cost: ~$0.001/video."
```

**Broader principle — capability-based routing:**

| Content type | Primary model | Fallback | Notes |
|-------------|---------------|----------|-------|
| YouTube video | Gemini Flash | Transcript API + any LLM | Gemini processes audio+visual natively |
| Web pages | Firecrawl → any LLM | WebFetch | Firecrawl handles JS rendering, paywalls |
| Images | Claude (native vision) | Gemini, Phi-4 | Claude's vision is strong |
| PDFs | Claude (native) | Gemini | Both handle PDFs well |
| Code repos | Claude Code (native) | — | CC has filesystem access |

### Obstacle Resolution Protocol (Phase 6 Design Input)

When Genesis encounters an inaccessible resource:

1. **Identify** the content type and access barrier
2. **Check** available tools/models that can handle this content type
3. **Route** to capable tool/model autonomously
4. **If no tool works**, check if a tool COULD work with user action (auth,
   config) — note this for proactive mention but continue trying alternatives
5. **If all autonomous options exhausted**, ask user with specific options:
   "I can't access X. Options: (a) you paste the content, (b) you auth
   Firecrawl, (c) you provide a Gemini key."
6. **After resolution**, note the successful path for future routing

**Proactive improvement surfacing:** Even when a workaround succeeds, note
the faster/better path for the morning report. Example: "I summarized your
YouTube video via Gemini, but authorizing Firecrawl would let me handle web
content more broadly — want to set that up?"

---

## 4. Multi-Agent Terminal Orchestration

### Research Findings

CMUX (Mac-only, GUI) demonstrates multi-agent UX patterns. tmux-based tools
provide headless Linux equivalents. Genesis has both a server backend AND a
web UI (AZ dashboard).

### Recommended Stack

| Layer | Tool | Purpose |
|-------|------|---------|
| Session orchestration | **Maniple** (MCP-native) | Spawn/manage CC sessions with worktree isolation |
| Low-level control | **libtmux** (Python library) | Programmatic tmux session management |
| Browser automation | **Playwright MCP** | Headless browser for agent research tasks |
| Dashboard UX | **AZ web UI** (custom views) | CMUX-inspired multi-pane agent monitoring |
| Mobile monitoring | **Amux PWA** (evaluate) | Phone-accessible agent status dashboard |

### Dashboard UX Patterns from CMUX

For AZ dashboard multi-agent view (Phase 9):
- [ ] Live pane view showing concurrent CC sessions and their current activity
- [ ] Severity-colored borders for session status (idle/working/blocked/error)
- [ ] Inter-session message flow visualization (message_queue in real-time)
- [ ] Embedded browser view for agent web research tasks
- [ ] Agent lifecycle controls (spawn, pause, terminate, reassign)

---

## 5. Microsoft Phi-4 Reasoning-Vision 15B

### Surplus Model Candidate

15B parameter multimodal model. Competitive with larger models on vision tasks.
Mixed-reasoning approach (structured reasoning only when needed).

### Evaluation Items
- [x] Check if Phi-4-reasoning-vision runs on LM Studio (${LM_STUDIO_HOST:-localhost})
  (stale — model landscape has moved on; newer models available)
- [x] VRAM requirements for 15B + vision encoder
  (stale — superseded by newer model options)
- [x] Benchmark against current surplus models for document/chart analysis
  (stale — superseded by newer model options)
- [x] Potential use: visual analysis of AZ dashboard state, document OCR,
  diagram understanding
  (stale — V4 multi-modal scope, not Phi-4 specific)

### Design Principle: Perception Before Reasoning

Microsoft's finding: "Multimodal AI fails due to perception, not reasoning."

Application to Genesis: When Genesis misunderstands a situation, first ask
"did it perceive the right information?" before "did it reason correctly?"
This validates investing in better context assembly (Phase 4) before throwing
more compute at reflection depth.

---

## 6. OpenAI Symphony — Proof of Work Pattern

### Task Verification Gate (Phase 9 Design Input)

When Genesis autonomously completes a task, verification must include:
- [x] All tests pass (existing) (done — VerificationRunner in src/genesis/autonomy/verification.py)
- [x] Lint clean (ruff check) (done — VerificationRunner)
- [x] Type check clean (if applicable) (done — VerificationRunner)
- [x] Diff review — Genesis explains what changed and why (done — VerificationRunner)
- [x] Before/after comparison — what was the state before, what is it now (done — VerificationRunner)
- [x] Regression check — nothing previously working is now broken (done — VerificationRunner)

This maps to our existing `verification-before-completion` skill but should be
architecturally enforced, not just a skill prompt.

### Genesis vs Symphony — Full Comparison

| Symphony Pattern | Genesis Equivalent | Status | Gap |
|---|---|---|---|
| Tasks from issue tracker | Awareness loop → CC dispatch | Phase 4 done, Phase 9 planned | No external tracker integration (V4) |
| Isolated workspace | Git worktrees per CC session | Active convention | Maniple could formalize |
| Proof of Work — tests pass | `verification-before-completion` skill | Active (skill) | Not a hard gate — needs arch enforcement |
| Proof of Work — lint clean | `ruff check .` in pre-commit | Active | Only on commit, not on task completion |
| Proof of Work — explain changes | Not formalized | **Gap** | CC sessions should produce structured change summaries |
| Harness eng — reliable tests | 743 tests, pytest suite | Active | No framework for evaluating OTHER codebases |
| Harness eng — machine-readable docs | CAPS markdown, CLAUDE.md | Active | Build phases are prose, not structured task specs |
| Concurrent agent reliability | Python + tmux | Planned (Phase 9) | No process-level fault isolation (Erlang/BEAM) |

### Where Genesis Upgrades on Symphony

1. **Proactive task discovery.** Symphony waits for tickets. Genesis discovers
   situations worth acting on autonomously via the awareness loop.
2. **Cognitive depth.** Symphony applies same agent to every task. Genesis
   classifies complexity → routes to appropriate compute depth.
3. **Memory continuity.** Symphony treats tasks as isolated. Genesis accumulates
   knowledge across tasks (activation scoring, user model, procedures).

### Adoption Items

1. **Hard verification gate** — task completion MUST include: tests pass + lint
   clean + diff review + structured explanation. Architectural enforcement, not
   skill suggestion. Phase 9 design input.
2. **Machine-readable task specs** — build phases should have structured format:
   ```yaml
   phase: 9
   step: 1
   description: "Basic autonomy L1-L2"
   preconditions: [phase_6_complete, phase_8_complete]
   verification:
     - pytest -v (all pass)
     - ruff check . (clean)
     - diff summary (generated)
   ```
3. **External tracker integration (V4)** — pull tasks from GitHub Issues, push
   completion evidence back.

---

## 7. MiMo 7B — Honest Assessment

Xiaomi's MiMo-7B-RL: 7B parameter model, trained on 25T tokens, RL-tuned on
130K verifiable math/code problems. Claims to match o1-mini on benchmarks.
Open-weight on HuggingFace. Vision variant: MiMo-VL-7B-RL.

### The Reality Check

Benchmark performance ≠ real-world agent reasoning. A 7B model is fundamentally
constrained in working memory and generalization. Matching o1-mini on structured
math/code is different from understanding vague intent, planning 20+ step goals,
and making judgment calls.

When Miclaw demos look impressive, the heavy lifting is in the **scaffolding**
(50+ tools, inference-execution loop, memory system), not the model's reasoning.
The model does intent classification and tool selection — not deep reasoning.

### Implication for Genesis Comparison

Miclaw's "autonomy" = well-orchestrated tool execution with a small model doing
intent parsing. Genesis's autonomy = genuine reasoning at multiple depths with
model selection matched to complexity. Our intelligence ceiling is fundamentally
higher.

### What to Learn from MiMo's Efficiency

If Miclaw gets useful behavior from 7B, their scaffolding engineering
(tool orchestration, memory compression, 50-90% token reduction) must be
excellent. Study their efficiency patterns even though the model isn't
competitive with our routing targets.

---

## 8. Claude Code Loop Tasks & Scheduled Tasks

### Source

Video: "Claude Code Loop & Scheduled Tasks" by Nathan Smith
(YouTube ID: OUyfxhFtGCo, summarized via Gemini API)

### What's New

CC now has two scheduling mechanisms:

**Loop Tasks (`/loop` skill):**
- Recurring prompts at intervals (default 10m, customizable)
- Session-scoped — dies when CC session closes
- 3-day auto-expiry safety limit
- No catch-up for missed fires
- Uses `CronCreate`/`CronList`/`CronDelete` tools
- Can be disabled: `CLAUDE_CODE_DISABLE_CRON=1`

**Scheduled Tasks:**
- Disk-stored, long-lived (indefinite)
- 7-day catch-up for missed runs
- Currently desktop app only (terminal/extension support coming)
- Daily, weekly, monthly cadence

### Genesis Relevance — DIRECTLY OVERLAPPING

This is CC implementing its own awareness loop. Let that sink in.

**Where CC's scheduling overlaps with Genesis:**

| CC Feature | Genesis Equivalent |
|---|---|
| `/loop` every N minutes | Awareness loop (5m tick) |
| CronCreate recurring tasks | AwarenessLoop.start() with APScheduler |
| Session-scoped tasks | Our loop is process-scoped too |
| "Watch logs for errors" use case | ErrorSpikeCollector signal |
| "Poll deployments" use case | Signal collectors (stubs) |
| "Urgent alerts" use case | CriticalFailureCollector |

**The critical question:** Do we build our own scheduling, or leverage CC's?

**Answer: We keep ours, but should be able to USE CC's when appropriate.**

Reasons to keep Genesis's own scheduling:
- Our awareness loop is more sophisticated (depth classification, signal
  fusion, urgency scoring) — CC's cron is just "run this prompt every N min"
- Our loop persists across sessions — CC's loop tasks die on session close
- Our loop is observable (awareness_ticks table, observability events)
- We need scheduling even when CC isn't running (AZ-level infrastructure)

Reasons to also use CC's scheduling:
- `/loop` is perfect for short-term operational monitoring during active work
- Scheduled Tasks (when terminal-supported) could handle daily/weekly rituals
  like morning reports — without Genesis needing its own scheduler for those

**Recommended integration:** Genesis's awareness loop remains the core scheduling
engine. CC's `/loop` is used tactically by Genesis when it needs CC to monitor
something during an active session (e.g., "watch this deployment while I work
on something else"). CC's Scheduled Tasks (when available in terminal) could
be evaluated as a delivery mechanism for morning reports.

---

## 9. Google Workspace CLI (`gws`)

### Source

VentureBeat: "Google Workspace CLI brings Gmail, Docs, Sheets and more into
a common interface for AI agents" (2026-03-06)

### What It Is

Open-source CLI (`npm install -g @googleworkspace/cli`) providing unified
access to ALL Google Workspace services. Dynamically built from Google's
Discovery Service — when Google adds an API endpoint, gws picks it up
automatically.

Key features:
- **MCP server mode** (`gws mcp`) — exposes Workspace APIs as MCP tools for
  Claude Desktop, Gemini CLI, VS Code
- **100+ Agent Skills** (SKILL.md files) — one per API + 50 curated recipes
  for Gmail, Drive, Docs, Calendar, Sheets
- **Gemini CLI extension** — gives Gemini agents direct Workspace access
- **OAuth authentication** — terminal-based auth flow

### Genesis Relevance — HIGH (Phase 8+)

This is a ready-made MCP server for Google Workspace. Genesis's Phase 8
(Basic Outreach) needs communication channels. `gws` provides:

1. **Gmail integration** — Genesis can draft/send emails, read inbox, organize.
   Morning report could be delivered via email. User can reply to Genesis via
   email (async communication pattern).

2. **Calendar awareness** — Genesis reads the user's calendar as a signal input
   for the awareness loop. "User has a meeting in 30 minutes" changes urgency
   scoring. "User is free all afternoon" enables surplus work.

3. **Docs/Sheets as workspace** — Genesis could write reports to Google Docs,
   track data in Sheets. Morning report as a live Google Doc that updates daily.

4. **Drive as knowledge base** — user drops documents in a Drive folder, Genesis
   ingests them into memory.

**The MCP angle is perfect for us.** We don't need to build Google Workspace
integration — we add `gws` as an MCP server in our configuration and Genesis
gets 100+ Workspace tools immediately. Zero Genesis code needed.

### Integration Plan

```json
{
  "mcpServers": {
    "gws": {
      "command": "gws",
      "args": ["mcp"],
      "env": {}
    }
  }
}
```

### Honest Caveats

- Requires Google OAuth — user must authenticate, and the auth token management
  needs to be reliable for autonomous operation
- Privacy: Genesis reading Gmail/Calendar is a significant trust boundary. Must
  be opt-in, transparent, and auditable.
- Rate limits: Google APIs have quotas that could throttle aggressive usage
- New project (March 2026) — may have rough edges, breaking changes

---

## 10. Cursor Automations — Always-On Coding Agents

### Source

Cursor blog: "Build agents that run automatically" (2026-03-05)
TechCrunch, Dataconomy, Help Net Security coverage.

### What It Is

Event-driven and scheduled AI agents running in cloud sandboxes.

**Triggers:** Slack message, Linear issue created, GitHub PR merged, PagerDuty
incident, cron schedules, custom webhooks.

**Architecture:** Each trigger spins up an ephemeral cloud sandbox. Agent follows
configured instructions using MCPs and models, verifies its own output, has a
memory tool that persists across runs.

**Integrations:** Slack, Linear, GitHub, PagerDuty, Datadog, Notion, Confluence.

**Scale:** Bugbot (their poster child) runs thousands of triggers/day, "caught
millions of bugs."

### Competitive Assessment

**Where Cursor Automations is ahead:**
- Cloud sandboxes — ephemeral, isolated, horizontally scalable
- Event-driven triggers — push-based, not poll-based
- Production scale — thousands of triggers/day proven
- Memory across runs — agents learn from past executions of same automation

**Where Genesis is better:**
- Cognitive depth — Cursor agents do one thing per trigger; Genesis classifies
  complexity and routes to appropriate compute depth
- Proactive intelligence — Cursor is purely reactive (trigger → action); Genesis
  initiates action without triggers via awareness loop
- Unified persistent identity — Cursor spins up disposable agents; Genesis is
  ONE agent with accumulated knowledge and evolving user model
- Cost sophistication — routing hierarchy with budget gating, circuit breaking,
  degradation tracking

### Design Discussion: Event-Driven vs Tick-Based

**Decision: Keep tick-based as backbone, add lightweight event wake-up.**

The 5-minute tick is fine for background autonomous operation. Nobody is waiting.
Event-driven adds complexity (race conditions, event storms, missed webhooks)
without proportional benefit for background tasks.

The right approach is a **lightweight hybrid**:
- Tick remains the reliable 5-minute heartbeat
- `force_tick()` (already implemented) can be called by specific event handlers
- A webhook listener or message queue consumer calls `force_tick()` for urgent
  events (service crash, user message waiting)
- No re-architecture needed — just glue code

**When sub-minute response matters:**
- Genesis's own health (DB down, service crash) → `force_tick()`
- User is actively waiting for a response → foreground CC session, not tick
- Everything else → next tick is fine

### Patterns to Adopt

1. **Bugbot pattern** — automation that reviews every commit/PR. Genesis should
   run lint + test + semantic review on pushes to its own repo. Dogfood
   autonomous quality control. (Phase 9)
2. **Webhook triggers** — external systems (Telegram, GitHub, monitoring) should
   be able to poke Genesis with `force_tick()`. Not a re-architecture — a thin
   HTTP endpoint that calls the existing method. (Phase 8)
3. **Memory across runs** — see Section 11.

### What NOT to Copy
- Ephemeral cloud sandboxes — opposite of our persistence-first design
- Platform lock-in — Genesis is platform-independent

---

## 11. Sub-Agent Memory Harvesting

### The Problem

When Genesis dispatches a CC sub-agent session (deep reflection, task execution,
surplus), the session's incidental learnings are lost. If a sub-agent discovers
"this API returns 429 after 10 requests," that knowledge dies with the session.

Cursor's agents have memory that persists across runs of the same automation.
Genesis should do the same, but better — cross-task, not just same-task.

### Design Approach

Three mechanisms, in order of implementation priority:

1. **Structured debrief** — CC session's final output includes a `learnings`
   section. Genesis parses this and feeds it into memory operations (store
   as observations, update procedures). This is the simplest — just a prompt
   engineering addition to the system prompt for CC sessions.

2. **Auto-memory harvesting** — CC has auto-memory that saves useful context.
   When a sub-agent CC session ends, Genesis reads its auto-memory directory
   and ingests relevant items into the Genesis memory store. Richer than
   structured debrief because it captures things the LLM found worth
   remembering even if not in the explicit output.

3. **Cross-run context injection** — when launching a CC session for a task,
   include relevant memories from previous runs of similar tasks. Uses
   existing hybrid retrieval (query by task description, inject top-k
   relevant memories into system prompt).

### Phase Mapping (confirmed 2026-03-09)

- Mechanism 1 (structured debrief): **Phase 6** (Learning Fundamentals)
- Mechanism 2 (auto-memory harvest): **Phase 6** (Learning Fundamentals)
- Mechanism 3 (cross-run injection): **Phase 7** (requires Phase 5 retrieval + Phase 6 stored learnings)

---

## 12. CC Inner Loops for Long-Running Tasks

### The Pattern

**Outer loop:** Genesis awareness loop (5-min tick, AZ-level, persistent)
**Inner loop:** CC `/loop` within a dispatched session (task-level, session-scoped)

When Genesis dispatches a CC background session for a long-running task (e.g.,
multi-step implementation), that session should use CC's `/loop` internally to:
- Monitor its own progress at intervals
- Verify intermediate results
- Self-correct if something goes wrong
- Detect when it's stuck and escalate

The two loops nest naturally:
```
Genesis awareness tick (5m)
  → detects task needs work
  → dispatches CC background session
    → CC session uses /loop internally (e.g., every 2m)
    → monitors own progress, self-corrects
    → completes and returns results
  → next tick harvests results + learnings
```

### Relevance to Ralph Loop

The "Ralph Loop" (autonomous self-healing overnight loop) IS this pattern.
Genesis's CC sessions should be able to run Ralph-style loops for long tasks,
with the awareness loop as the supervisory outer layer.

Key difference from bare Ralph: Genesis adds governance. The awareness loop
checks on CC sessions, can terminate runaway loops, and harvests learnings.
Bare Ralph has no supervisor.

---

## 13. Genesis CC Session Environment — Gap Analysis

### Current State

CCInvoker infrastructure exists (`src/genesis/cc/invoker.py`) and accepts
`mcp_config`, system_prompt, model, effort. But spawned CC sessions lack:

| Component | Status | Gap |
|-----------|--------|-----|
| MCP servers | 4 stubs built, not wired to CC sessions | Need config generator |
| Hooks/safety | Only in current dev session | Need inheritable hooks |
| Skills | Not configured for Genesis sessions | Need session-specific skills |
| CLAUDE.md | Inherits repo CLAUDE.md only | Need session-type-specific instructions |
| Auth | Not implemented | Need service-to-service model |
| Worktree setup | Convention only | Need auto-creation per task session |
| Memory harvest | Not implemented | See Section 11 |
| `/loop` integration | Not used | See Section 12 |

### What Each Session Type Needs

**Reflection sessions** (deep/strategic):
- MCP: genesis-memory (read context), genesis-health (system state)
- Skills: none (reflection is prompt-driven)
- Hooks: block file modification, block git operations (read-only)
- CLAUDE.md: reflection-specific instructions + CAPS identity files

**Task execution sessions:**
- MCP: genesis-memory, genesis-health, genesis-recon, possibly gws
- Skills: verification-before-completion, debugging
- Hooks: safety (no force push, no rm -rf), audit logging
- CLAUDE.md: task-specific instructions + constraints + worktree setup
- `/loop`: enabled for self-monitoring during long tasks

**Surplus sessions:**
- MCP: genesis-memory (store findings), genesis-recon (research tools)
- Skills: none (surplus is exploratory)
- Hooks: strict budget limits, read-only filesystem
- CLAUDE.md: surplus-specific instructions, brainstorm templates

### Implementation Needed

1. `src/genesis/cc/session_config.py` — generates MCP config, hook config,
   and CLAUDE.md content per session type
2. CCInvoker enhanced to apply session config before launch
3. Post-session harvesting hook (reads auto-memory, parses learnings)

### Phase Mapping (confirmed 2026-03-09)

- MCP config generation: **Phase 7** (via `session_config.py`)
- Hook inheritance: **Phase 7** (before any untrusted workloads)
- Session-specific CLAUDE.md: **Phase 7** (skill injection per session type)
- Skill loading for CC sessions: **Phase 7** (research-evaluation, verification, etc.)
- Auth model: **Phase 7** (single-user V3, multi-user V4)

---

## 14. Programmatic Signals — Trackio Pattern

### Source

Video: "Hugging Face — The AI Learns to Train Models from Scratch" by
Abubakar Abid. Claude Code + HF Jobs + Trackio for autonomous ML training.

### The Pattern

Instead of making the AI parse raw logs/data (token-expensive, error-prone),
embed domain knowledge into code as programmatic alerts. The AI receives
clean, pre-interpreted signals.

Example: `trackio.alert("Val loss increasing")` fires when validation loss
trends up. The AI sees one clean signal instead of parsing thousands of log
lines.

### Application to Genesis Signal Collectors

Our signal collectors (Phase 1, currently stubs) should follow this pattern.
**We are past Phase 4. Real collectors should produce real data in Phase 6.**

Priority collectors to make real:

| Collector | Programmatic signal | Raw alternative (avoid) |
|-----------|-------------------|----------------------|
| BudgetCollector | `budget_used: 73%, daily_remaining: $2.14, status: WARNING` | Parse cost tracker logs |
| ErrorSpikeCollector | `errors_last_hour: 12, baseline: 2, spike: true` | Parse error logs |
| CriticalFailureCollector | `service: qdrant, status: DOWN, since: 5m ago` | Ping services and parse responses |
| TaskQualityCollector | `last_task: FAILED, reason: tests_failed, count: 3` | Parse test output |
| MemoryBacklogCollector | `unprocessed: 47, threshold: 20, status: WARNING` | Query DB and interpret |

The collector code does the computation. The LLM gets the interpreted result.
This is cheaper, more reliable, and more testable.

---

## 15. Autonomous ML Training — Future Capability

The Trackio/HF Jobs demo shows CC managing remote compute jobs. Genesis could
do the same for surplus tasks — launch model training/fine-tuning on LM Studio
or cloud, monitor via programmatic alerts, terminate if unstable.

**Status:** V4/V5 capability. Architecture supports it (surplus scheduler +
compute availability + routing). Not V3 scope.

---

## Follow-Up Items (Pending)

*Items requiring further research, discussion, or implementation:*

### Infrastructure (Near-Term)
- [ ] **Gemini YouTube routing** — add call site to model_routing.yaml, test
  with real video summarization workflow
- [x] **Firecrawl auth** — set up for broader web access capability
  (done — firecrawl CLI installed and authenticated, actively used)
- [ ] **Google Workspace CLI** — install `gws`, test MCP server mode, evaluate
  for Phase 8 outreach (Gmail, Calendar awareness)
- [x] **Maniple evaluation** — clone, test MCP integration, assess fit for
  Genesis multi-session orchestration
  (stale — CCSessionManager solves this differently)
- [x] **CC compatibility tracking document** — Create `docs/reference/cc-compatibility.md`
  mapping CC features to Genesis components, version requirements, unused capabilities.
  Phase: immediate (cross-cutting). Priority: medium. (build-phases.md updated 2026-03-09)
  **DONE:** Document created at `docs/reference/cc-compatibility.md` (2026-03-09).
- [x] **Gemini YouTube routing fix** — Must use types.Part(file_data=...) approach, NOT
  text URL injection. Text approach causes hallucination (fabricates plausible video
  summaries). Phase: immediate. Priority: high — affects research reliability.
  **DONE:** Reference doc created at `docs/reference/gemini-routing.md` (2026-03-09).
  Evaluate skill (`src/genesis/skills/RESEARCH_EVALUATION.md`) updated with routing details.
- [x] **Gemini quota rotation** — Per-model daily quotas on free tier. Rotate models when
  one is exhausted (2.0-flash, 2.5-flash, 3-flash-preview have separate quotas).
  Phase: immediate. Priority: medium.
  **DONE:** Model chain and rotation pattern documented in `docs/reference/gemini-routing.md`.

### Architecture (Phase-Specific)
- [x] **Tool capability discovery** — populate `tool_registry` with real
  capability metadata, content-type routing function, cross-model routing table.
  **Phase 6.** (done — src/genesis/learning/tool_discovery.py)
- [x] **Sub-agent memory harvesting** — structured debrief (mechanism 1) +
  auto-memory ingestion (mechanism 2) after CC session completion.
  **Phase 6.** Cross-run context injection (mechanism 3) in **Phase 7.**
  (done — src/genesis/learning/harvesting/)
- [x] **Skill wiring into AZ plugin** — Genesis skills discoverable via
  AZ `skills_tool:load`, skill files updatable by procedural learning.
  **Phase 6.** (done — genesis plugin skill system)
- [x] **Real signal collectors** — BudgetCollector, ErrorSpikeCollector,
  CriticalFailureCollector producing real data using Trackio pattern.
  **Phase 6.** (done — src/genesis/learning/signals/)
- [ ] **Research output documentation structure** — how Genesis organizes
  its own evaluation/research findings (file system, memory, or hybrid).
  Design question to resolve during **Phase 7** implementation.
  (build-phases.md updated 2026-03-09)
- [x] **CC session config generator** — `session_config.py` producing MCP config,
  hooks, CLAUDE.md, skill injection per session type (reflection/task/surplus).
  **Phase 7.** (done — src/genesis/cc/session_config.py)
- [x] **Hard verification gate** — architectural enforcement for task
  completion proof (tests + lint + diff + explanation).
  **Phase 9.** (done — src/genesis/autonomy/verification.py)
- [ ] **Bugbot self-review** — auto-review on push to Genesis's own repo.
  **Phase 9.** (build-phases.md updated 2026-03-09)
- [x] **Autonomous obstacle escalation** — autonomy-level-gated decision
  on when to try alternatives vs escalate to user.
  **Phase 9.** (done — src/genesis/autonomy/escalation.py)
- [ ] **Machine-readable task specs** — structured YAML format for build phases
  (Symphony workflow.md pattern). **Phase 9** (companion to hard verification).
- [ ] **Webhook endpoint for force_tick()** — thin HTTP handler allowing
  external systems to wake the awareness loop. **Phase 8.**
- [x] **Per-action confirmation UX** — timed confirmation with auto-reject
  for Phase 8 governance (Miclaw pattern: 60s timeout). **Phase 8.**
  (done — approval gates in src/genesis/autonomy/)
- [x] **Tool capability registry** — machine-readable manifest for MCP tools
  (Miclaw SDK pattern). **Phase 6** (done — src/genesis/learning/tool_discovery.py)
- [x] **Explicit memory consolidation in Deep reflection** — Phase 7 Deep reflection prompt
  should include explicit consolidation jobs (dedup, merge, restructure) not just pattern
  recognition. Phase: 7. (done — REFLECTION_DEEP.md + context_gatherer)
- [x] **Learning stability monitoring** — procedure quarantine, contradiction detection,
  learning regression signal, procedure effectiveness tracking in weekly quality
  calibration. Phase: 7. (done — src/genesis/reflection/stability.py)
- [x] **Skill evolution system** — SkillEffectivenessAnalyzer (per-skill metrics from
  cc_sessions), SkillRefiner (LLM-driven proposals), SkillApplicator (autonomy-gated:
  MINOR auto-apply, MODERATE+ staged). Skills as learning artifacts, not static config.
  Uplift/workflow typing, progressive disclosure enforcement, baseline comparison,
  tool usage tracking. Phase: 7. (done — src/genesis/learning/skills/)
- [ ] **Self-directed memory editing** — tools for Genesis to reorganize/merge its own
  memories and procedures (MemGPT pattern). Phase: V4. Priority: medium.
- [x] **Intent validation logging for autonomous actions** — Before Genesis executes an
  autonomous action, log reasoning chain and verify against policy. Phase: 9.
  (done — src/genesis/autonomy/trace_verification.py)
- [ ] **tmux as CCInvoker session backend** — Evaluate tmux-managed sessions vs
  subprocess.Popen for GL-2/GL-3. Phase: GL-2/GL-3. Priority: medium.
- [x] **Evaluate skill → Genesis plugin skill** — Restructure evaluate command as a
  Genesis SKILL.md-format skill for CC background session use. Current CC command stays
  for interactive use. (stale — subsumed by current skills/plugin system)
- [ ] **External memory-mcp exposure** — Make memory-mcp available to tools beyond AZ
  plugins. Phase: V4. Priority: low.
- [ ] **Memory scoping for multi-agent access** — When Genesis spawns CC sessions, control
  what memory domains each session can access (Node Set pattern from Cognee). Phase: V4.
  Priority: medium.
- [ ] **Graph layer for memory system** — Add Knowledge Graph (relationship/ontology) layer
  on top of existing vector + FTS5 retrieval. Typed edges on memory_links, entity extraction,
  graph traversal helpers (recursive CTEs), Pydantic ontology schemas (Cognee pattern).
  Phase: ~~V5~~ V4. Priority: medium. (Upgraded 2026-03-12 — AGI gap analysis item 7-10,
  infrastructure exists in memory_links table)
- [ ] **Skill performance benchmarking** — Measure Genesis task performance with and without
  skills loaded. Quantify the impact. Phase: ~~6~~ 7 (now part of SkillEffectivenessAnalyzer
  baseline_success_rate comparison). Priority: high.
  (build-phases.md updated 2026-03-10 — skill evolution system)
- [x] **Skills inventory and design exercise** — Systematic enumeration of all skills Genesis
  needs across all consumers (background sessions, foreground, autonomous, inbox). Phase: 6.
  Priority: high. **DONE** (2026-03-10 — 11 skills authored, inventory.py updated).

### Inbox Monitor (Post-Phase 6 — Parallel Implementation)
- [x] **InboxMonitor service** — APScheduler-based folder watcher, hash tracking,
  item classification. (done — src/genesis/inbox/monitor.py)
- [x] **Research task dispatch** — links → surplus queue → CC session with evaluate
  skill. (done — inbox dispatches CC sessions via CCInvoker)
- [x] **Response file writer** — Obsidian-compatible markdown output to _genesis/
  subfolder. (done — src/genesis/inbox/writer.py)
- [x] **User configuration** — path, interval, on/off via config file or dashboard.
  (done — config/inbox_monitor.yaml)
- [x] **Foreground context bridge** — inbox activity visible in cognitive state
  summary for Telegram/WhatsApp follow-up.
  (done — message_queue in src/genesis/db/crud/message_queue.py)

### Research
- [x] **Miclaw token compression** — research 50-90% token reduction technique,
  evaluate for memory/context system (stale — no longer prioritized)
- [x] **Phi-4 LM Studio test** — VRAM requirements, benchmark document analysis
  (stale — model landscape moved on)
- [x] **MiMo scaffolding patterns** — study tool orchestration efficiency from 7B
  (stale — no longer prioritized)
- [ ] **Home Assistant MCP** — evaluate for environmental signal collection (V4)
- [x] **Cursor Automations memory tool** — study their cross-run learning
  mechanism, compare to our Phase 6 procedure learning
  (stale — overtaken by our own memory/procedure system)
- [ ] **Multi-modal inbox ingestion impact assessment** — V4 multi-modal adds content
  extractors upstream (PDF→text, image→caption, audio→transcribe). No V3 schema/embedding
  conflicts. Document extraction pipeline design. Phase: V4. Priority: low.
- [ ] **CC deterministic vs agentic execution toggle** — Surplus tasks could benefit from
  mode selection. Phase: V4. Priority: low.

### UX
- [ ] **Dashboard multi-agent view** — wireframe CMUX-inspired pane layout
  for AZ dashboard. **Phase 9.**
- [ ] **External tracker integration** — GitHub Issues as task source. **V4.**
- [ ] **Bugbot pattern for Genesis** — moved to Architecture section.
  **Phase 9.** (build-phases.md updated 2026-03-09)

---

---

## 16. Obsidian Async Inbox — Peripheral Research Service

### Source

6 sources evaluated (2026-03-09):
- Video: "Claude Code + Obsidian = Second Brain" (YouTube eRr2rTKriDM, via Gemini)
- Video: "AI-Powered Second Brain" (YouTube 2mAGV7MQd04, via Gemini)
- Forum: Obsidian Agent Client plugin (forum.obsidian.md)
- GitHub: deivid11/obsidian-claude-code-plugin
- Article: Eleanor Konik — "Claude + Obsidian Got a Level Up"
- Article: XDA — "Claude Code Inside Obsidian Was Eye-Opening"

### What We're Building (NOT What These Sources Build)

Every source uses Obsidian + CC interactively (user triggers, AI responds).
Genesis does something fundamentally different: **autonomous monitoring of a
user-configured folder with proactive research and async markdown responses.**

This is NOT:
- The Genesis knowledge base (that's memory-mcp, Qdrant, Phase 5)
- A full Obsidian integration (no Obsidian plugin needed)
- Interactive conversation (that's Telegram/WhatsApp foreground)
- Part of the awareness loop (that's cognition; this is a peripheral service)

This IS:
- An async drop box — user leaves links/notes, Genesis picks them up on schedule
- A peripheral service with its own scheduler (like surplus scheduler)
- Filesystem-based — works with Obsidian, Logseq, plain folders, anything
- Feeds into existing infrastructure (surplus queue, outreach pipeline, message_queue)

### Architecture Decision: NOT a Signal Collector

The inbox monitor is a **peripheral service**, not an awareness loop signal
collector. The awareness loop is Genesis's cognitive cycle — perception, urgency,
depth classification, reflection. Checking an external folder is an external
task, not an internal cognitive function. Like the surplus scheduler, it has its
own APScheduler instance, its own cadence, and dispatches work items into existing
queues. The awareness loop never knows about Obsidian.

### Architecture Decision: NOT a Cron Job

A system-level cron job is too disconnected from Genesis. The inbox monitor is
a Genesis-internal service (like `SurplusScheduler`) that the user configures
through Genesis's config surface (dashboard or config file). It runs inside
Genesis's process, has access to Genesis's infrastructure (surplus queue,
memory, message_queue), and participates in Genesis's lifecycle (start/stop
with Genesis, observable via health probes).

### The Flow

```
User drops links/notes in configured folder
  → InboxMonitor checks on schedule (configurable interval)
  → Classifies items: link (research), note (store), ambiguous (ask user)
  → Links dispatched as surplus tasks → CC session runs evaluate skill
  → Results written to _genesis/ subfolder as Obsidian-compatible markdown
  → Ambiguous items queued as outreach question (Phase 8 pipeline)
  → User reads responses in Obsidian at their convenience
  → User follows up via Telegram/WhatsApp voice → foreground Genesis has context
```

### Response Pattern

```
watched-folder/
├── interesting-links.md          ← user drops this
├── random-thought.md             ← user drops this
└── _genesis/
    ├── 2026-03-09-evaluation.md  ← Genesis writes research findings
    └── 2026-03-09-questions.md   ← Genesis asks about ambiguous items
```

Response files use Obsidian-compatible markdown (wiki links, tags, YAML
frontmatter). Written atomically (temp + rename) to avoid sync conflicts.

### Voice Response Path

User reads Genesis's Obsidian responses → opens Telegram/WhatsApp → sends
voice note discussing the findings → foreground Genesis (via relay, Phase 8)
has full context from message_queue and cognitive state. No special integration
needed — existing foreground channel handles it.

### Phase Mapping

| Component | Version | Phase | Notes |
|-----------|---------|-------|-------|
| InboxMonitor service (scheduler, folder scanning, hash tracking) | V3 | Phase 6 | Follows SurplusScheduler pattern |
| Item classification (link/note/ambiguous) | V3 | Phase 6 | Uses learning fundamentals |
| Research task dispatch (links → CC evaluate) | V3 | Phase 6+ | Needs CC session dispatch |
| Response file writing (markdown to _genesis/) | V3 | Phase 6+ | Minimal dependency |
| Pending clarification queue | V3 | Phase 8 | Needs outreach pipeline |
| User config (path, interval, on/off) | V3 | Phase 6 | Dashboard or config file |
| Tag-based routing (#genesis/* tags) | V4 | — | Needs vault-wide scanning |
| Proactive research (Genesis-initiated) | V4 | — | Needs calibrated user model |
| Graph-connected outputs (wiki links) | V4 | — | Needs vault structure understanding |

### Implementation Plan

See `docs/plans/2026-03-09-inbox-monitor-plan.md` for the implementation plan
targeting post-Phase 6 delivery. Designed to be implementable in a parallel
session once Phase 6 learning fundamentals are available.

### What We Learned from Sources

1. **Inbox pattern is universal** — every methodology has one (PARA inbox,
   brain dumps, unprocessed captures). Genesis's design is inbox-agnostic.
2. **Filesystem-based is robust** — Eleanor Konik found MCP servers "less
   seamless" than direct file access. Agree: no Obsidian plugin needed.
3. **CLAUDE.md as persistent context is validated** — every source relies on it.
   Genesis already has SOUL.md + USER.md. No new context mechanism needed.
4. **Nobody does autonomous monitoring** — all sources are interactive.
   This is a Genesis-unique capability.
5. **Per-note session tracking** (deivid11 plugin) is a pattern worth noting
   for tracking which inbox items Genesis has processed.

---

## 17. Claude Code Ecosystem & Memory Architecture Patterns (2026-03-09 Batch 2)

Five sources evaluated covering CC updates, memory architecture patterns, agentic storage,
skill frameworks, and terminal orchestration.

### Source 1: Google PM Open-Sources "Always On Memory Agent" (VentureBeat)

**What it is:** A Google PM released an agent built with Google ADK + Gemini 3.1 Flash-Lite
that eliminates vector databases entirely: "No vector database. No embeddings. Just an LLM
that reads, thinks, and writes structured memory." The LLM organizes/updates memory directly.

**Four-lens evaluation:**
- **Helps:** Validates our LLM-first principle. "Dreaming" consolidation pattern (background
  LLM pass reorganizing memories) maps to our Deep reflection (Phase 7). Multi-modal
  ingestion (text/image/audio/video/PDF) relevant to inbox monitor.
- **Doesn't help:** We already have Qdrant + FTS5 + RRF hybrid retrieval — more sophisticated
  than pure LLM-driven memory. Pure LLM memory won't scale for Genesis's indefinite growth.
  Gemini-specific (ADK framework).
- **Could help:** Separation of retrieval vs organization. We should keep vectors for retrieval
  but add explicit LLM consolidation passes for organization/dedup/restructuring. Memory
  consolidation should be an explicit Deep reflection output, not just pattern recognition.
- **Learn from it:** HN discussion shows skepticism is warranted — this is a simplification
  that works for bounded agents, not production-grade systems. Our hybrid approach is right.

**Architecture impact:** Extends. Add explicit memory consolidation to Phase 7 Deep
reflection prompt. Multi-modal ingestion is V4 (additive — no conflict with V3 embeddings,
content gets transcribed to text before embedding, no Qdrant migration needed).

### Source 2: Claude Code 2.0/2.1 — Scheduled Tasks & Self-Improving Workflows

**What it is:** CC added native scheduled tasks (hourly/daily/weekly) in desktop app.
Self-improving workflows where agents edit own code/prompts. Log file for cross-run memory.
CC 2.1 added: hooks in frontmatter, forked skill context, hot reload, wildcard permissions.

**Four-lens evaluation:**
- **Helps:** CC is our intelligence layer — every upgrade IS a Genesis upgrade. Scheduled
  tasks validate our surplus/inbox designs. Self-improving workflows validate procedural
  learning (Phase 6). Hooks in frontmatter directly relevant to Phase 7 session_config.py.
  Forked skill context + hot reload relevant to Phase 6 skill wiring.
- **Doesn't help:** Desktop-only (we run server-side). Stateless by default (we have real
  persistent state). 7-day missed window inadequate. No cognitive architecture.
- **Could help:** CC scheduled tasks as inner loop mechanism (Genesis outer loop 5m →
  CC inner loop for monitoring within dispatched sessions). Deterministic vs agentic
  execution mode toggle for surplus system (V4).
- **Learn from it:** CC is building toward always-on agents without the cognitive backbone.
  Genesis's advantage is perception, memory, reflection, governed autonomy — not scheduling.

**Critical insight — CC Update Lifecycle:**
CC is as much a dependency as Agent Zero. Genesis needs a **CC compatibility layer**:
- Map CC features → Genesis components using them
- Map CC version requirements → Genesis minimums
- Track CC capabilities we're NOT using → evaluation queue
- Track CC deprecations → migration plans
- When CC updates: what changed? Affects our wrappers? Unlocks workarounds? Obsoletes builds?

**Architecture impact:** Validates existing designs. New follow-up: CC compatibility
tracking document (`docs/reference/cc-compatibility.md`).

### Source 3: Agentic Storage — Solving AI's Limits with LLMs & MCP (FranksWorld)

**What it is:** High-level think piece defining "agentic storage" — persistent storage
for AI agents across sessions. MCP as "universal translator" between agents and storage.
Three security layers: immutable versioning, sandboxing, intent validation.

**Four-lens evaluation:**
- **Helps:** Conceptual validation of our memory architecture. MCP as storage interface
  validates our 4-MCP-server design. Immutable versioning aligns with our append-only
  memory model.
- **Doesn't help:** High-level think piece, no code or architecture. Generic best practices.
- **Could help:** Intent validation pattern — before autonomous action, log reasoning chain
  and verify against policy. Maps to Phase 9 autonomy verification. MCP as external storage
  interface — expose memory-mcp to tools beyond AZ (V4).
- **Learn from it:** Industry converging on "agents need persistent memory + MCP as interface."
  We're already there. "Agentic storage" as vocabulary for external documentation.

**Architecture impact:** Extends. Intent validation logging → Phase 9 follow-up item.
External memory-mcp exposure → V4 follow-up item.

### Source 4: Superpowers — Claude Code Plugin (obra/superpowers)

**What it is:** CC plugin enforcing structured development: brainstorming gates (design
before code), TDD red-green-refactor, systematic 4-phase debugging, subagent-driven
development with code review. Auto-activates based on context.

**Four-lens evaluation:**
- **Helps:** Already installed in our environment — actively used during Genesis development.
  Validates Phase 9 hard verification gate. Skill auto-activation pattern relevant to
  Phase 6 skill wiring.
- **Doesn't help:** CC developer tool, not a Genesis component. Optimized for interactive
  coding, not autonomous agent workflows.
- **Could help:** SKILL.md structure (progressive disclosure: metadata → body → references)
  should be the template for Genesis's own skills. Auto-activation triggers worth studying
  for Phase 6. Brainstorming gate concept could inspire Genesis pre-task validation.
- **Learn from it:** Discipline enforcement through tooling works. The plugin doesn't add
  capabilities — it enforces practices. Same philosophy as our autonomy levels +
  verification gates.

**Architecture impact:** Extends. Adopt SKILL.md structure for Genesis skills (Phase 6).
Evaluate skill should be restructured as a Genesis plugin skill following this pattern.

### Source 5: Nested Claude Code with Tmux (Geeky Gadgets)

**What it is:** Using tmux as orchestration layer for multiple parallel CC sessions.
Central controller allocates tasks to tmux terminals by complexity/priority. Session
persistence through tmux survives disconnects.

**Four-lens evaluation:**
- **Helps:** Directly relevant to GL-2/GL-3 CC session dispatch. tmux provides session
  persistence, monitoring, debuggability. "Spec folder + task manager folder" pattern
  maps to our CC session dispatch architecture.
- **Doesn't help:** macOS-optimized orchestrator (we're Ubuntu, but tmux is universal).
  "Skip all permissions" is a security anti-pattern. Simple task distributor without
  cognitive architecture.
- **Could help:** CCInvoker could use tmux as session backend instead of subprocess.Popen.
  Benefits: persistence, easy monitoring, can attach for debugging, survives invoker
  crashes. Multiple parallel CC sessions for surplus dispatch.
- **Learn from it:** tmux as session substrate is battle-tested. 15-minute check-in
  pattern independently validates our 5-min awareness cadence.

**Architecture impact:** Extends. tmux-backed CC sessions → evaluate for GL-2/GL-3
CCInvoker implementation.

#### Source 6: n8n — Open-Source Workflow Automation (XDA Developers)

**What it is:** Open-source visual workflow automation. Drag-and-drop node-based system
with 400+ integrations. Replaces scattered automation (webhooks, cron, scripts) with a
centralized workflow canvas. JavaScript/Python inside nodes. AI/LLM integration.

**Four-lens evaluation:**
- **Helps:** Could serve as event trigger layer for Genesis force_tick(). Visual workflow
  canvas pattern worth studying for Phase 8 dashboard. Execution logging with node-level
  failure tracking maps to our observability layer.
- **Doesn't help:** Automation orchestrator, not cognitive architecture. Does "if X then Y"
  — no perception, reflection, memory. Adds infrastructure complexity.
- **Could help:** Webhook receiver for force_tick() — normalizes events from GitHub, email,
  calendar into Genesis-compatible triggers (V4). Non-cognitive automation offloading.
  Phase 8 outreach delivery pipeline (Genesis produces content, n8n delivers).
- **Learn from it:** "One canvas, all logic visible" debuggability principle. Node-level
  failure retry with branching more sophisticated than current "log and move on."

**Architecture impact:** Could extend. n8n as webhook/event receiver → V4. Dashboard
visualization patterns → Phase 8.

#### Source 7: Google Antigravity — Agent-First IDE (Multiple Sources)

**What it is:** Google's agent-first IDE (Nov 2025), built on Gemini 3. Multi-agent
orchestration via "Agent Manager" / "Mission Control." Supports Claude models (Opus 4.6
free). Adopted Anthropic's skill standard — CC skills portable. MCP support.

**Four-lens evaluation:**
- **Helps:** Skills portability validated — Antigravity adopted Anthropic's SKILL.md
  standard, confirming it as industry format. Multi-agent orchestration patterns
  (Agent Manager, centralized approval inbox) worth studying for dashboard UX.
- **Doesn't help:** IDE, not server-side runtime. Genesis needs headless 24/7 operation.
  Free tier is temporary (user acquisition play). Vendor lock-in risk.
- **Could help:** Alternative compute routing if Antigravity exposes an API (free Opus 4.6).
  BLAST initialization pattern for task planning prompts. Approval inbox UX for dashboard.
  "Context rot" prevention (fresh conversation per task) aligns with our session isolation.
- **Learn from it:** IDE vs runtime is a real industry split. Genesis is runtime-focused —
  runs when no human is watching. This is our moat.

**Corrected competitive framing:**
Genesis is NOT the opposite of agent-first. Genesis is **autonomy-first** — proactive
agent behavior (awareness loop, morning reports, surplus research, inbox monitoring) that
goes BEYOND agent-first. The L1→L7 autonomy hierarchy is the PATH to full autonomous
operation with earned trust, not an alternative to it. Antigravity is agent-first (reactive
agent, human triggers). Genesis is autonomy-first (proactive + reactive, self-triggered +
human-triggered, governed escalation). Proactive behavior is a step ABOVE agent-first —
even agent-first systems don't initiate their own work.

**Architecture impact:** Validates skills standard. Dashboard UX patterns → Phase 8.
Compute routing → V4 (evaluate if API exists).

---

## 18. Skills Industry Convergence & Graph Memory (2026-03-09 Batch 3)

Three sources evaluated covering knowledge graph memory, skill performance evidence,
and industry skill format convergence.

#### Source 8: Cognee — Shareable Domain-Specific Agentic Memory (YouTube/Mastra)

**What it is:** Vasilije Markovic (CEO, Cognee) presents Knowledge Graph-based agent
memory using Neo4j on top of vector embeddings. Multi-agent memory sharing via isolated
"Node Sets" with access control. 10-15 specialized retriever types. Enterprise
deployments at Bayer, finance, healthcare. Built on LangChain/LangGraph.

**Four-lens evaluation:**
- **Helps:** Knowledge Graphs ON TOP of embeddings (not instead of) validates our hybrid
  retrieval approach. Multi-agent memory sharing with access isolation maps to CC session
  dispatch. Feedback loops (agent discoveries update graph) = our Phase 6 memory harvesting.
  Session-based reasoning + permanent memory = our CC sessions + memory-mcp separation.
- **Doesn't help:** Neo4j adds infrastructure complexity we don't need for V3. Built on
  LangChain/LangGraph (we don't use). Enterprise use cases (billing, pharma) far from
  our domain.
- **Could help:** Pydantic-based ontology framework for relationship rules (V4 memory
  evolution). Time-aware retrievers for event sequence understanding. Memory domains with
  access control for multi-agent scoping. Supervisor Agent pattern validates our awareness
  loop as signal synthesizer.
- **Learn from it:** Graph + Vector is the mature end state for agent memory. Pure vector
  (our V3) is good enough to start. Pure LLM (Google Source 1) is too simple. Our V4/V5
  memory evolution should add a graph layer.

**Architecture impact:** Extends (V4+). Validates Phase 6 memory harvesting. Memory
scoping → V4. Graph layer → V5.

#### Source 9: LangChain Skills — 25% → 95% Performance Jump

**What it is:** LangChain released skills for CC that boost performance on LangChain-specific
tasks from 25% to 95%. 11 skills across LangChain, LangGraph, and Deep Agents. Progressive
disclosure. Install via npm.

**Four-lens evaluation:**
- **Helps:** 25%→95% is hard evidence that skills dramatically improve agent performance.
  Validates our Phase 6 skill wiring plan. Same SKILL.md format with progressive disclosure.
- **Doesn't help:** LangChain-specific — we don't use LangChain/LangGraph. npm install
  path assumes CC desktop/CLI, not our background sessions.
- **Could help:** Benchmark methodology — measure Genesis with/without skills for
  quantitative justification. LangGraph "durable execution" skill could inform CC session
  persistence.
- **Learn from it:** Skills are NOT optional. 4x performance jump = difference between
  usable and broken. Phase 6 skill wiring elevated from "important" to "critical."

**Architecture impact:** Validates and elevates. Skill performance benchmarking added
as Phase 6 verification item.

#### Source 10: Microsoft Agent Framework — Agent Skills

**What it is:** Microsoft's open format for agent skills. SKILL.md with YAML frontmatter,
three-stage progressive disclosure (advertise ~100 tokens → load <5000 tokens → read
resources on demand), load_skill and read_skill_resource tools. .NET and Python support.

**Four-lens evaluation:**
- **Helps:** Three major players (Anthropic, Microsoft, LangChain) converged on same
  SKILL.md pattern. Industry standard confirmed. Multi-directory support for team libraries
  matches our multi-source skill needs.
- **Doesn't help:** Microsoft ecosystem (.NET/Semantic Kernel). Implementation not portable.
- **Could help:** Advertise/load/read three-stage model is the most explicit progressive
  disclosure articulation. Adopt this terminology. Skills as auditable multi-step tasks
  connects to Phase 9 verification gates.
- **Learn from it:** The skill format debate is over. Three independent implementations
  converged. Just adopt it.

**Architecture impact:** Validates. Confirms Phase 6 skill wiring direction.

---

## 19. OpenFang — Agent OS Competitive Analysis (2026-03-10)

### What OpenFang Is

Open-source Rust-based "Agent Operating System" by RightNow AI. Single 32MB
binary, 180ms cold start, 40MB idle memory. 137K lines of Rust across 14 crates.
Open-sourced 2026-03-01 (13K+ stars). Pre-v1.0 (v0.3.30), breaking changes
between minors. Dual MIT + Apache 2.0.

Agents are treated as OS processes (spawn/suspend/reclaim), not library
abstractions. 53 built-in tools, 40 channel adapters, 27 LLM providers (123+
models), WASM sandboxing, MCP client/server support, 16 security systems.

### Key Components

**7 "Hands" (autonomous specialist agents):**
- **Clip** — video → vertical short clips (FFmpeg, yt-dlp, 5 transcription paths, 4 TTS)
- **Lead** — prospect discovery, ICP matching, scoring 0-100, dedup, scheduled reports
- **Collector** — OSINT monitoring, change detection, knowledge graph, 5-tier source reliability
- **Predictor** — superforecasting, Brier score tracking, calibrated reasoning, contrarian mode
- **Researcher** — multi-source research, CRAAP credibility evaluation, 4 verification levels
- **Twitter** — autonomous X management, 7 content formats, mandatory approval queue
- **Browser** — Playwright automation, session persistence, mandatory purchase approval gate

**30 general-purpose agents:** role-based system prompts (analyst, architect,
coder, debugger, devops, doc-writer, recruiter, orchestrator, etc.). Each is a
`agent.toml` manifest with a tailored system prompt and tool access list.

### Architecture Comparison

| Dimension | OpenFang | Genesis v3 | Assessment |
|-----------|----------|------------|------------|
| **Agent model** | Many specialized agents (30+7) | Single agent with skills + earned autonomy | Genesis: deeper. One agent that learns across domains > 37 isolated agents. |
| **Runtime** | Rust binary, WASM sandbox | Python on AZ, CC subprocess | OpenFang: lighter, more secure sandboxing. Genesis: more flexible, LLM-native. |
| **Channel breadth** | 40 adapters (Telegram, Discord, Slack, WhatsApp, Signal, Matrix, etc.) | 1 (Telegram via GL-3) | OpenFang: far ahead. V4 should expand. |
| **Search** | Built-in web search tool | SearXNG (native AZ) + Brave fallback | Comparable. |
| **Memory** | SQLite + vector embeddings, LLM compaction | SQLite + Qdrant + FTS5 + ACT-R activation + auto-linking + RRF retrieval | Genesis: significantly more sophisticated. |
| **Learning** | None documented | Outcome tracking, procedural memory, triage, calibration (Phase 6) | Genesis: unique advantage. |
| **Reflection** | None documented | 4-depth reflection engine (micro/light/deep/strategic) | Genesis: unique advantage. |
| **Autonomy model** | Full autonomy with approval gates per-Hand | Earned autonomy L1-L9 with governance | Genesis: more nuanced. |
| **Forecasting** | Predictor Hand (Brier scores, calibration) | Nothing equivalent | **Gap** — worth building. |
| **Content creation** | Clip Hand (video processing pipeline) | None | Domain-specific; build if needed. |
| **Security** | 16 systems (WASM, Ed25519, Merkle audit, taint tracking, SSRF block, secret zeroize) | Safety hooks, confirmation gates, pre-commit blocks | OpenFang: more comprehensive. V5 should address. |

### Key Takeaways

1. **Genesis's single-agent-with-skills architecture is superior to OpenFang's
   many-agents model.** Our agent learns across domains; theirs can't. Our
   reflection engine has no equivalent in OpenFang. Their 30 "agents" are
   effectively prompt templates — our skill system does the same thing with
   better infrastructure underneath.

2. **OpenFang ships breadth; Genesis ships depth.** The gap isn't capability —
   it's that we haven't written the skill files for specific domains yet. The
   infrastructure to support any of OpenFang's Hands already exists in Phases
   0-6. The path to parity is writing skills, not building new systems.

3. **Worth stealing:**
   - **Predictor pattern** — superforecasting with Brier score tracking for
     strategic reflection. Phase 7/8 skill.
   - **Channel adapter abstraction** — per-channel model overrides, DM/group
     policies, rate limiting. V4 post-GL-3.
   - **WASM sandboxing for tool execution** — V5 autonomy (L5+).
   - **Agent-as-process semantics** — spawn/suspend/reclaim lifecycle states
     for CCSessionManager. V4.
   - **HAND.toml manifest pattern** — explicit tool/permission/metric
     declarations per capability. Evolution of SKILL.md. V4.

4. **Not worth adopting as platform:** Pre-v1.0, Rust-only extensibility (our
   entire codebase is Python), no Claude Code integration, would require
   abandoning 6 completed phases + CC integration stack.

### Implementation Details (2026-03-10 Deep Dive)

**Key revelation:** All OpenFang Hands are 100% prompt-engineered. Zero code in
any Hand. Every capability is two files: HAND.toml (manifest + system prompt
pipeline) + SKILL.md (domain knowledge injected as LLM context). The Predictor
doesn't calculate Brier scores in code — the LLM reads/writes a JSON file.
The Clip Hand runs FFmpeg via `shell_exec`. This is exactly our LLM-first
architecture philosophy.

**Predictor architecture:** 10 Tetlock principles, 8-type signal taxonomy with
strength weights, Bayesian confidence calibration (5%-95% scale), 5-step
reasoning chain template (reference class → evidence → synthesis → assumptions →
resolution criteria), prediction ledger as JSON file, 8-bias cognitive checklist,
domain-specific source guides (Tech/Finance/Geopolitics/Climate). All prompt-driven.

**Collector/OSINT architecture:** 7-phase pipeline (state recovery → target init →
source discovery → collection sweep → knowledge graph → change detection → report).
5-tier source reliability scoring. Entity extraction with typed relationships.
Change significance scoring (critical/important/minor). Sentiment tracking.

**Lead architecture:** ICP construction → 5-10 discovery queries → 3-tier enrichment
(basic/standard/deep) → dedup (normalized matching) → 0-100 scoring rubric (ICP
match 30pts, growth signals 20pts, enrichment quality 20pts, recency 15pts,
accessibility 15pts). LinkedIn handled via `site:linkedin.com` Google searches —
no scraping, fully compliant.

**Browser architecture:** Higher-level `browser_*` tools (not raw Playwright).
CSS selector priority hierarchy, error recovery strategies (8 failure types with
recovery steps), purchase safety gates (mandatory approval before any financial
transaction), session persistence across messages.

**Clip architecture:** 8-phase pipeline. FFmpeg + yt-dlp + 6 transcription paths
(YouTube auto-subs shortcut avoids STT entirely). LLM-driven "viral segment
selection" as core value step. Vertical crop (9:16), SRT caption burn-in, thumbnail
generation. All via shell commands.

**LinkedIn integration:** Three mechanisms across the platform: (1) Channel adapter
using official Organization Messaging API (OAuth2), (2) Lead Hand uses
`site:linkedin.com` search via Google (public profiles only), (3) Social media agent
references LinkedIn content but no API integration. No scraping — fully compliant.

### Follow-Up Items

| Item | Scope | Phase | Priority | Status |
|------|-------|-------|----------|--------|
| Forecasting skill (Brier score tracking) | V3 | Phase 7 | High | **DONE** — `skills/forecasting/SKILL.md` |
| OSINT investigation skill | V3 | Phase 7 | High | **DONE** — `skills/osint/SKILL.md` |
| Lead generation skill | V3 | Phase 7 | Medium | **DONE** — `skills/lead-generation/SKILL.md` |
| Video processing skill | V3 | Phase 7 | Medium | **DONE** — `skills/video-processing/SKILL.md` |
| Browser automation skill | V3 | Phase 7 | Medium | **DONE** — `skills/browser-automation/SKILL.md` |
| Skills registered in inventory | V3 | Phase 7 | High | **DONE** — `learning/skills/inventory.py` |
| Channel adapter abstraction (post-GL-3) | V4 | Post-Phase 8 | Medium | Planned |
| WASM sandboxing for autonomous tool execution | V5 | Phase 9+ | Low | Planned |
| Agent-as-process lifecycle for CC sessions | V4 | Post-GL-4 | Low | Planned |
| Structured capability manifests (HAND.toml → SKILL.toml) | V4 | Post-Phase 9 | Low | Planned — GROUNDWORK(skill-manifest) seeded in Phase 7 skill evolution |

---

## 20. Infrastructure: Container Architecture Decision (2026-03-10)

### Context

Genesis runs in an unprivileged Incus container (Ubuntu 24.04) on Proxmox.
Cannot run Docker natively. Services like SearXNG (AZ's native search) need
a deployment strategy.

### Options Evaluated

**Option A: Nested Docker (Docker inside Incus)**
- Technically possible via `security.nesting=true`
- **REJECTED.** Ubuntu 24.04 + AppArmor 4 actively breaks this (documented in
  Incus issues #791, #2623, #2757). Docker version churn constantly re-breaks
  nested configurations. Nested containers bypass parent CPU limits (kernel bug).
  Every kernel/Docker/AppArmor/Incus update is a potential landmine.

**Option B: Sibling Containers**
- New Incus container per complex service, communicating via bridge network.
- Already proven: Ollama at `${OLLAMA_URL:-localhost:11434}`.
- Best isolation, proper resource control, independently updatable.

**Option C: Bare Metal in Existing Container**
- Install service directly alongside Genesis.
- Already proven: Qdrant on `localhost:6333`.
- Lowest overhead, but increases blast radius and dependency conflicts over time.

### Decision

**Hybrid B+C based on service characteristics:**

| Service Characteristic | Strategy | Example |
|----------------------|----------|---------|
| Simple Python/Go/binary, small deps | Bare metal (C) | SearXNG, Qdrant |
| Complex, conflicting deps, multi-process | Sibling container (B) | Future heavy services |
| Distributed only as Docker image | Sibling container running Docker (B) | Unlikely for our stack |
| Nested Docker | **Never** | — |

### SearXNG Specifically

Install bare-metal in existing container. It's a Python app with its own venv
and system user. AZ's install scripts (`docker/base/fs/ins/install_searxng.sh`)
are the reference. Requirements: ~256MB RAM, ~300MB disk, Python 3.11+, no GPU.
Negligible overhead on our 24GB machine.

Steps: create `searxng` system user, clone repo, create venv, pip install,
configure `/etc/searxng/settings.yml` (port 55510, JSON format), run via
uwsgi or systemd unit.

---

## 21. SearXNG Installation Plan (2026-03-10)

### Status: COMPLETE (2026-03-10)

Installed bare-metal in Genesis container. Service running on `localhost:55510`
via systemd (`searxng.service`). Verified returning results from Brave,
Startpage, and DuckDuckGo engines.

### What It Enables

- AZ's native search tool (`python/tools/search_engine.py`) works without
  falling back to Brave Search API
- Free, unlimited, private metasearch — no API keys, no rate limits
- Already the expected default in AZ's architecture

### Implementation

1. Install system packages: `git build-essential libxslt-dev zlib1g-dev libffi-dev libssl-dev`
2. Create system user: `useradd -r -s /bin/bash -d /usr/local/searxng searxng`
3. Clone: `git clone https://github.com/searxng/searxng /usr/local/searxng/searxng-src`
4. Create venv: `python3 -m venv /usr/local/searxng/searx-pyenv`
5. Install: `pip install setuptools wheel pyyaml && pip install --use-pep517 --no-build-isolation .`
6. Configure: `/etc/searxng/settings.yml` (secret key, port 55510, JSON format)
7. Create systemd unit for auto-start
8. Verify: `curl -s 'http://localhost:55510/search?q=test&format=json' | jq .`

### Verification

- AZ search tool returns results from SearXNG (not Brave fallback)
- Service survives container restart (systemd)
- Response format matches AZ's expected JSON structure

---

## 22. MemGPT / Letta — Memory Architecture Evaluation (2026-03-10)

### What It Is

MemGPT (paper: "Towards LLMs as Operating Systems", UC Berkeley, Oct 2023) treats
the LLM like a CPU and applies OS-style virtual memory management to the context
window. The LLM gets function-calling tools to manage its own memory across three
tiers: Main Context (RAM — system instructions + working context + FIFO message
queue), Recall Memory (searchable vector DB of past conversations), Archival Memory
(long-term storage for facts and compressed summaries).

The LLM autonomously calls functions (`conversation_search`, `archival_memory_search`,
`core_memory_append`, `core_memory_replace`) to page information in/out. Memory
pressure warnings trigger at ~70% context capacity.

Project rebranded to **Letta** ($10M raised). MemGPT = research pattern; Letta =
commercial framework. Letta V1 (Oct 2025) rearchitected the agent loop — dropped
heartbeat/send_message mechanism for native model reasoning (optimized for GPT-5,
Claude 4.5 Sonnet). Added "Context Repositories" (git-based memory versioning)
Feb 2026.

### Four-Lens Evaluation

**Helps:**
- Validates our memory architecture (MemoryStore + HybridRetriever + ActivationScore).
  Independent convergence on multi-tier memory is a good signal.
- Validates depth-scoped context assembly (same idea as selective paging).
- "Memory pressure" concept — proactive warning when context budget is tight.
  Useful for Phase 7 context management.

**Doesn't Help:**
- Architecture mismatch: MemGPT is single-agent with tight inference loop; Genesis
  is multi-runtime orchestrated (CC sessions + AZ + awareness ticks + reflections).
- Core problem largely solved by 2026 context windows (200K+ Claude, 1M+ Gemini).
  The 4K/8K crisis that motivated MemGPT in 2023 is gone.
- Letta V1 itself is moving away from the pattern — even the creators acknowledge
  frontier reasoning models have internalized context management.
- No multi-agent coordination.

**Could Help:**
- Strategic forgetting as first-class concept: summarization-then-archive as memory
  compaction (V4 memory maintenance).
- Self-directed memory editing: giving Genesis tools to reorganize its own cognitive
  state, not just write to it (V4).
- Context Repositories (git-based memory versioning): tracking user model evolution,
  procedure changes over time, rollback on drift (V5).

**Learn From It:**
- MemGPT→Letta V1 evolution: complex mechanism simplified toward trusting the LLM.
  Mirrors our "LLM-first solutions" design principle.
- Memory pressure warning is good UX independent of paging.
- Commercial trajectory validates stateful agent infrastructure market.

### Competitive Position

| Dimension | MemGPT/Letta | Genesis | Assessment |
|-----------|-------------|---------|------------|
| Memory tiers | 3-tier with LLM-managed paging | 2-collection + hybrid retrieval (Qdrant + FTS5 + activation) | Different trade-offs. Neither strictly better. |
| Context management | FIFO queue + pressure warnings | Depth-scoped assembly (Micro/Light/Deep/Strategic) | Genesis: architecturally superior (intention-driven vs recency-driven) |
| Multi-session coherence | Single agent loop (Conversations API catch-up) | Multi-runtime, checkpoint-and-resume, message queue | Genesis ahead |
| Learning | Memory stays static unless LLM edits | Procedural memory, triage, calibration, outcome attribution | Genesis substantially ahead |
| Memory search | Vector + keyword | Hybrid RRF (vector + FTS5 + activation scoring) | Genesis ahead |
| Production maturity | Letta v1 shipping, $10M funded | Pre-production (Phase 6 complete) | Letta ahead on deployment |

### Additional Findings from Deep Research

**Sleep-Time Compute** (Letta 0.7.0): Dual-agent architecture separating conversation
from memory management. A "Sleep-Time Agent" runs asynchronously during idle periods
with a different (slower, more capable) LLM, managing both its own memory and the
primary agent's context blocks. Produces "clean, concise, and detailed memories" vs
MemGPT's incrementally messy ones. Pareto improvement on AIME and GSM benchmarks.
**Directly validates our Deep reflection design** — we arrived at the same pattern
(background Sonnet/Opus session consolidating memory while the system is idle).

**Filesystem benchmark** (Letta's own research): Simple `grep`/`search_files`/`open`/
`close` with GPT-4o-mini achieved 74.0% on LoCoMo, outperforming Mem0's graph variant
(68.5%). Letta's conclusion: "simpler tools are more likely to be in the training data
and therefore more likely to be used effectively." Validates our pragmatic hybrid
retrieval approach over complex graph architectures.

**Memory messiness acknowledged by creators**: "Memory formation in MemGPT is
incremental, so memories may become messy and disorganized over time." This is exactly
the drift/pollution problem our Phase 7 learning stability monitoring now defends
against (procedure quarantine, contradiction detection, consolidation-as-defense).

### Architecture Impact: **Validates** existing design

### Scope Tags

| Item | Scope | Phase | Priority |
|------|-------|-------|----------|
| Memory pressure warnings | V4 | Memory maintenance subsystem | Medium |
| Self-directed memory editing tools | V4 | Procedure management (hundreds+ procedures) | Medium |
| Context Repositories (git-based versioning) | V5 | Memory evolution tracking | Low |
| Sleep-Time Compute pattern | V3 | Already designed (Deep reflection) | — (validated) |
| Core MemGPT paging pattern | Never | Context windows solved this | — |

---

## 23. Test-Time Training (TTT) — Architecture Evaluation (2026-03-10)

### What It Is

TTT (paper: "Learning to (Learn at Test Time): RNNs with Expressive Hidden States",
Sun et al., July 2024) is a new type of neural network layer that replaces the
fixed hidden state of an RNN with a small neural network whose **weights ARE the
hidden state**, updated via self-supervised learning on each new token during
inference. Published at ICLR 2026 ("Test-Time Training Done Right").

Two variants: TTT-Linear (hidden state = linear model, fast) and TTT-MLP (hidden
state = two-layer MLP, more expressive). Each new token triggers a gradient step
on a self-supervised loss — conceptually a tiny fine-tuning step per token.

Results: TTT-Linear/MLP keep reducing perplexity with longer context (like
Transformers), while Mamba flatlines after ~16K. Linear time/space complexity.

**Distinct from "test-time compute scaling"** (o1, R1, chain-of-thought): TTT is
an architecture component (layer type). Test-time compute scaling is an inference
strategy on existing architectures. Complementary, not competing.

Current state: research implementations (PyTorch + JAX, `test-time-training` GitHub
org). Applied to video generation and 3D reconstruction. No production LLM deployment.

### Four-Lens Evaluation

**Helps:**
- Almost nothing directly applicable. TTT is about building LLMs, not using them.
  Genesis consumes LLMs via API.

**Doesn't Help:**
- Wrong layer of the stack. Like evaluating transistor design for a web application.
- No API provider offers TTT-based models.
- Computational cost (gradient descent per token) conflicts with cost-conscious routing.
- Can't adopt even if wanted — would require training our own models.

**Could Help:**
- If model providers adopt TTT, long-context performance of models we consume improves
  (indirect benefit — better models = better reflections, triage, task execution).
- If TTT-enhanced small models ship (3B-7B with TTT layers), could be interesting for
  awareness-loop SLM level (Ollama). TTT makes small models punch above their weight
  on long sequences. **Speculative — no such model exists yet.**
- End-to-End TTT for long context (E2E variant) would benefit deep/strategic
  reflection sessions if it matures into production models.

**Learn From It:**
- "Hidden state as learned model" principle: adapting compression to specific
  sequences. Philosophically aligned with Genesis's procedural memory — building
  structure that adapts to specific user/environment, not just storing data.
- Transformer vs TTT trade-off (process everything vs compress into adaptive
  state) parallels Genesis's awareness loop design (maintain adaptive cognitive
  state rather than re-process all history).
- **Critical: TTT stability warnings directly apply to Genesis.** Unconstrained
  updates from self-supervised learning can degrade performance — catastrophic
  forgetting of original knowledge. Genesis's Self-Learning Loop faces the same
  risk. See "Learning Stability Monitoring" below.

### Additional Findings from Deep Research

**In-Place TTT** (ICLR 2026): Drop-in enhancement for existing LLMs without retraining.
Treats the final projection matrix of MLP blocks as fast weights, updating them in-place
during inference. Demonstrated on Qwen3-4B-Base to handle 128K context. If this matures
into production models from providers we consume, it's the most likely path for TTT to
indirectly benefit Genesis.

**Needle-in-haystack failure**: Full attention "dramatically outperforms" TTT-E2E on
pinpoint fact retrieval. Compression-based memory loses specific random details. Key
architectural insight: **retrieval (our approach) beats compression for specific facts.**
This validates our hybrid retrieval design (Qdrant + FTS5 + activation) over approaches
that compress history into fixed-size representations.

**TTT-E2E** (Dec 2025): 3B models matching full-attention Transformer scaling across
8K-128K context. 35x faster than full attention at 2M context. Reframes long-context
as continual learning, not architecture design. Uses standard Transformer with
sliding-window attention — the TTT is the adaptation mechanism. Only the final 25% of
MLP layers adapt; remaining layers preserve pre-training knowledge (a direct defense
against catastrophic forgetting).

**Key theoretical result** (NVIDIA, Feb 2026): TTT with KV Binding is provably
equivalent to a form of learned linear attention. The inner loop doesn't perform
"meta-learning" in the conventional sense but induces structured, history-dependent
mixing of query/key/value vectors. This demystifies the mechanism considerably.

### Architecture Impact: **Irrelevant** to Genesis design (but stability lessons are actionable)

### Learning Stability Monitoring — Concrete Mechanism (added to Phase 7)

The TTT stability problem — where learning degrades rather than improves the
system — maps directly to a risk in Genesis's Self-Learning Loop. "Keep monitoring"
is not a plan. The following concrete mechanisms were added to **Phase 7
(build-phases.md)** in response:

1. **Procedure effectiveness tracking** — weekly quality calibration now includes
   per-procedure success rate trends. Are tasks where learned procedures were
   applied succeeding more than baseline?

2. **Procedure quarantine** — procedures with declining success rates (applied 3+
   times, rate below 40%) get a `quarantined` flag excluding them from retrieval.
   Still stored, can be rehabilitated. Deep reflection decision.

3. **Learning velocity sanity check** — deep reflection checks whether the rate
   of new procedures/observations is reasonable. High contradiction rates or
   volume spikes indicate learning instability.

4. **Contradiction detection** — deep reflection memory consolidation explicitly
   identifies and resolves contradictory observations (resolve, merge, or flag
   for user review). Contradictions don't accumulate silently.

5. **Learning regression signal** — if procedure effectiveness trends downward
   for 2+ consecutive weeks, emit `learning.regression` event via event bus AND
   include in cognitive state. Makes the regression visible to all subsequent
   reasoning.

6. **Consolidation-as-defense** — memory consolidation reframed from housekeeping
   to safety operation. Dedup, merge, and prune prevent memory pollution.

These mechanisms turn "watch for drift" into "detect, quarantine, signal, and
correct drift automatically."

### Scope Tags

| Item | Scope | Phase | Priority |
|------|-------|-------|----------|
| TTT as architecture component | Never | We don't train models | — |
| TTT-enhanced SLMs for awareness loop | Future | If such models ship | Low |
| Stability lessons (learning regression) | V3 | Phase 7 (quality calibration) | **High** |
| Procedure quarantine mechanism | V3 | Phase 7 | **High** |
| Contradiction detection in consolidation | V3 | Phase 7 | **High** |
| Test-time compute scaling (broad trend) | V3 | Already active (reasoning models, extended thinking) | — |

---

## 24. n8n → Claude Code Agentic Workflows — Integration Pattern (2026-03-12)

**Source:** [YouTube — 9x: "I switched from n8n workflows to agentic workflows (Claude Code)"](https://www.youtube.com/watch?v=Vc3I-8eQ7rA)
**Supporting:** [ability.ai — Claude Code n8n workflows](https://www.ability.ai/blog/claude-code-n8n-workflows)

### What It Is

A practitioner's case study on replacing hand-built n8n workflows with Claude
Code as the *architect* of those workflows. The key insight: rather than choosing
between code-based agents and low-code workflow tools, use the AI agent to BUILD
the low-code infrastructure. Claude Code handles API connections, JavaScript
transformations, and iterative logic — then exports deterministic, auditable
workflows to n8n for ongoing operation.

### Four-Lens Evaluation

**Lens 1 — How It Helps:**
- Validates Genesis's dual-runtime architecture. Genesis already separates
  "intelligence" (CC sessions) from "infrastructure" (Agent Zero). This is
  the same pattern: LLM does the thinking, deterministic system handles
  repetitive execution.
- The "agent builds the workflow" pattern is directly relevant to Phase 8
  (outreach) where Genesis will construct notification delivery pipelines.
  Instead of hardcoding email/Slack/webhook formats, Genesis could construct
  and iterate on delivery workflows.

**Lens 2 — How It Doesn't Help:**
- Genesis doesn't use n8n. The video's n8n-specific advice (MCP Server Trigger
  integration, visual debugging) doesn't apply.
- The "speed" claims (60-second workflow creation) are content-creator hype.
  The real value is in iterative debugging, not raw generation speed.
- Genesis's outreach needs are much simpler than enterprise workflow automation.
  We don't need a visual workflow builder for "send a morning report."

**Lens 3 — How It COULD Help:**
- The MCP-as-bridge pattern is worth noting: n8n workflows started via MCP
  Server Trigger become tools that any MCP client can invoke. Genesis's 4 MCP
  servers could similarly expose genesis capabilities as tools callable by
  external systems — making Genesis a service other agents consume.
- "AI architect of deterministic pipelines" is a pattern for V4 self-tuning:
  Genesis could construct and refine its own automation workflows rather than
  using static configurations.

**Lens 4 — What to Learn:**
- The symbiotic architecture (LLM for construction, low-code for operation)
  is a principled design pattern. It's "build once with intelligence, run many
  times without it" — which is precisely what Genesis's procedural memory does
  at the cognitive level.
- Governance advantage of visual/declarative systems for ongoing ops is real.
  Agent Zero's dashboard serves this function for Genesis.

### Architecture Impact

**Validates** our dual-runtime split. The industry is converging on "smart
builder + dumb runner" as the right separation. We're already there.

### Scope Tags

| Item | Scope | Phase | Priority |
|------|-------|-------|----------|
| MCP-as-service pattern (expose Genesis as callable tools) | V4 | Post-V3 | Low |
| AI-constructed delivery pipelines for outreach | V4 | Phase 8 could benefit, but overkill for V3 | Low |

---

## 25. MASFactory — Graph-Centric Multi-Agent Orchestration with Vibe Graphing (2026-03-12)

**Source:** [YouTube — Discover AI: "Vibe Graphing: 10x More Affordable than Vibe Coding"](https://www.youtube.com/watch?v=QFlQuX_cddk)
**Paper:** [arxiv:2603.06007 — MASFactory: A Graph-centric Framework for Orchestrating LLM-Based Multi-Agent Systems with Vibe Graphing](https://arxiv.org/abs/2603.06007)
**Code:** [GitHub — BUPT-GAMMA/MASFactory](https://github.com/BUPT-GAMMA/MASFactory)

### What It Is

An academic framework (BUPT-GAMMA lab) for orchestrating LLM multi-agent
systems using directed computation graphs. The core innovation is "Vibe Graphing"
— a three-stage pipeline that compiles natural language task descriptions into
executable workflow graphs:

1. **Role Assignment** — maps intent to candidate agents with defined responsibilities
2. **Structure Design** — generates graph topology (nodes = agents/subworkflows,
   edges = dependencies/message passing)
3. **Semantic Completion** — parameterizes with prompts and tools, produces
   executable workflow

Architecture has three layers:
- **Foundation:** Nodes and Edges forming directed graphs (DAG and cyclic)
- **Component:** Specialized node types — Graph, Loop, Agent, Switch, Interaction
- **Integration:** Pluggable Message Adapters and Context Adapters (Mem0,
  LlamaIndex), decoupled from topology

Uses GPT-5.2 for workflow construction, GPT-4o-mini for execution. Claims
~$0.26 per workflow generation vs $3.02-$3.49 for direct "vibe coding"
(~10x cheaper).

Evaluated on 7 benchmarks (HumanEval, MBPP, BigCodeBench, SRDD, MMLU-Pro,
GAIA, GPQA). Successfully reproduced ChatDev, MetaGPT, AgentVerse, CAMEL,
HuggingGPT workflows.

### Four-Lens Evaluation

**Lens 1 — How It Helps:**
- The three-layer architecture (foundation → component → integration) maps
  cleanly to how Genesis structures its own internal orchestration. Our MCP
  servers are the integration layer, our subsystems are components, our event
  bus + awareness loop is the foundation.
- The NodeTemplate/ComposedGraph concept (reusable, parameterizable subgraphs)
  is exactly what V4 meta-prompting would need — Genesis constructing its own
  reflection pipelines from composable templates.
- Context Adapter pattern (abstracting Mem0, LlamaIndex, MCP behind unified
  interface) validates our MCP-as-primary-boundary decision. Same idea, same
  rationale.

**Lens 2 — How It Doesn't Help:**
- Genesis is NOT a multi-agent framework. Genesis is a single intelligent agent
  that dispatches CC sessions for deep work. MASFactory solves a different
  problem: orchestrating swarms of specialized agents with explicit message
  passing. Genesis's orchestration is implicit (awareness loop → depth
  classification → appropriate action) not explicit (graph topology).
- The "Vibe Graphing" workflow construction uses GPT-5.2 — Genesis doesn't
  have that model and wouldn't use it for this purpose anyway.
- Academic benchmark performance (HumanEval, MBPP) doesn't tell us anything
  about real-world agent orchestration quality. These are code-generation
  benchmarks, not agent-architecture benchmarks.
- Lacks checkpoint/resume — a critical gap for real production use. Genesis
  already has deferred work queues and staleness policies.

**Lens 3 — How It COULD Help:**
- The "compile natural language to executable workflow" pipeline is interesting
  for V4 meta-prompting. Instead of static prompt templates, Genesis could
  compile high-level intent ("investigate why the API is slow") into a
  structured execution graph — similar to how MASFactory compiles task
  descriptions into agent topologies.
- The VS Code visualizer (topology preview, runtime tracing, human-in-the-loop)
  is a UX concept our neural monitor dashboard could adopt — showing Genesis's
  cognitive flow as a live graph rather than just status cards.
- The cost reduction through staged compilation (intent → structure → params →
  execution) rather than direct generation is a valid optimization principle.
  Genesis's compute routing could adopt a similar staged approach for complex
  task decomposition.

**Lens 4 — What to Learn:**
- The Graph abstraction as the universal orchestration primitive is gaining
  traction (LangGraph, MASFactory, Microsoft AutoGen). Genesis chose a
  different primitive (awareness loop + depth classification + event bus).
  Our choice is better for a single persistent agent; theirs is better for
  swarm orchestration. These aren't competing — they solve different problems.
- The "10x cheaper" claim is comparing apples and oranges (structured
  compilation vs. free-form code generation). But the underlying principle —
  staged decomposition reduces total LLM calls — is sound and applies to
  Genesis's compute routing.
- Framework maturity: MASFactory is academic (March 2026 paper, no production
  deployments). It's a research artifact, not production infrastructure.

### Competitive Position

| Dimension | MASFactory | Genesis | Honest Assessment |
|-----------|-----------|---------|-------------------|
| **Orchestration model** | Explicit graph (nodes/edges) | Implicit (awareness loop, depth classification) | Different problems. MASFactory: swarm coordination. Genesis: single agent cognition. Both valid. |
| **Workflow construction** | NL → graph compilation (Vibe Graphing) | Static prompt templates (V3), meta-prompting planned (V4) | MASFactory ahead on NL→workflow. But their "workflows" are agent graphs; ours are reflection/task pipelines. |
| **Memory integration** | Context Adapters (pluggable) | MCP servers (4 dedicated) | Similar abstraction level. Our implementation is more mature. |
| **Production readiness** | Academic paper, no production use | 1684 tests passing, resilience layer, runtime wiring | Genesis: significantly more production-ready |
| **Reusability** | NodeTemplate, ComposedGraph | Procedures, stored reflections | Similar intent (learn and reuse), different mechanism |

### Architecture Impact

**Validates** our MCP-as-integration-boundary and pluggable adapter patterns.
**Extends** (V4) — the NL→workflow compilation concept is relevant to
meta-prompting design.

### Scope Tags

| Item | Scope | Phase | Priority |
|------|-------|-------|----------|
| NL → execution graph compilation | V4 | Meta-prompting design | Medium |
| Cognitive flow visualization in dashboard | V4 | Neural monitor extension | Low |
| Staged decomposition for compute routing | V3 | Phase 2 enhancement (already built) | Low |

---

## 26. Perplexity Personal Computer — Always-On Agent Architecture (2026-03-12)

**Source:** [YouTube — Perplexity: "Personal Computer, by Perplexity"](https://www.youtube.com/watch?v=f9mjOnznkNA)
**Supporting:** [9to5Mac](https://9to5mac.com/2026/03/11/perplexitys-personal-computer-is-a-cloud-based-ai-agent-running-on-mac-mini/),
[Perplexity Blog](https://www.perplexity.ai/hub/blog/introducing-perplexity-computer),
[Digital Trends](https://www.digitaltrends.com/computing/perplexitys-personal-computer-what-is-it-what-can-it-do-and-what-does-it-cost/)

### What It Is

Perplexity's "Personal Computer" is a cloud-based persistent AI agent running
on a dedicated Mac mini. Announced at Perplexity's Ask 2026 conference
(2026-03-11). It extends their earlier "Perplexity Computer" (Feb 2026) into
an always-on, local-hybrid architecture:

- **Runtime:** Mac mini running continuously, merging local apps with
  Perplexity's cloud services
- **Access:** Controllable from any device, anywhere
- **Security:** Runs in "secure environment," sensitive actions require user
  approval, action logging, kill switch available
- **AI processing:** Runs on Perplexity's servers, not the local hardware
  (Mac mini handles app access, file system, sessions)
- **Pricing:** $200/month (Perplexity Max), 10,000 monthly credits, Mac-only,
  waitlist
- **Capabilities:** Works across files, apps, and sessions; persistent state
  between interactions

### Four-Lens Evaluation

**Lens 1 — How It Helps:**
- Direct competitive validation. Perplexity is building the SAME thing Genesis
  is: an always-on, persistent AI agent that maintains state, accesses tools,
  and acts autonomously. The market is moving toward persistent autonomous
  agents, not one-shot assistants.
- Their security model (user approval for sensitive actions, action logging,
  kill switch) validates Genesis's governance architecture (Phase 8:
  confirmation gates, audit trails, escalation hierarchy).
- The "controllable from any device" model is something Genesis should
  consider. Currently Genesis is only accessible via the AZ web UI on
  localhost. Remote access via tunnel works but isn't first-class.

**Lens 2 — How It Doesn't Help:**
- Mac-only. Genesis runs on Linux (Incus container). No hardware overlap.
- $200/month for 10,000 credits is a consumption model Genesis explicitly
  rejects. Genesis's philosophy is "quality over cost — always" with the
  USER controlling tradeoffs, not a credit system.
- Perplexity's architecture is split-brain: local hardware for app access,
  cloud for AI processing. This creates latency and privacy concerns that
  Genesis avoids by running everything in one container.
- Extremely thin on technical details. "Works across your files, apps, and
  sessions" tells us nothing about HOW. No architecture docs, no API surface,
  no extension model. It's a product announcement, not a technical contribution.

**Lens 3 — How It COULD Help:**
- The "persistent session" concept — agent maintains state across interactions,
  doesn't start cold each time — is exactly what Genesis's memory activation
  system does. But Perplexity runs on a dedicated Mac mini, giving it actual
  OS-level persistence (running apps, open browser tabs, file system state).
  This is conceptually closer to what Genesis would need for V5 full autonomy.
- Their "10,000 monthly credits" model, while philosophically opposed to
  Genesis's approach, is interesting as a REPORTING mechanism. Genesis could
  track "equivalent credits consumed" for user visibility without using it
  as a control mechanism.
- If Perplexity publishes an MCP integration or API, Genesis could potentially
  consume Perplexity's search/research capabilities as a tool — similar to
  how we use SearXNG.

**Lens 4 — What to Learn:**
- The market is pricing persistent AI agents at $200/month. This sets user
  expectations. Genesis needs to deliver significantly more value than
  "search agent on a Mac mini" to justify the infrastructure complexity.
- Perplexity's bet is "AI needs its own computer" — giving the agent a
  persistent environment rather than ephemeral sessions. Genesis already has
  this (the container IS the persistent environment). We're architecturally
  ahead here but haven't shipped the user-facing product.
- The announcement is heavy on vision, light on substance. No benchmarks,
  no architecture, no API. This is marketing-first, not engineering-first.
  Genesis should NOT emulate this approach. Ship working code, not announcements.

### Competitive Position

| Dimension | Perplexity Personal Computer | Genesis | Honest Assessment |
|-----------|----------------------------|---------|-------------------|
| **Persistence** | Mac mini running 24/7 | Incus container running 24/7 | Comparable. Both always-on. |
| **Model access** | Multi-model (per TechCrunch) | Multi-model routed (Claude, Gemini, free APIs, SLMs) | Comparable. |
| **Tool ecosystem** | Mac apps, files, browser | MCP servers, CLI tools, web, AZ tools | Genesis: broader programmatic access. Perplexity: GUI app access. |
| **Memory** | "Persistent sessions" (no details) | ACT-R activation, Qdrant, FTS5, hybrid retrieval, procedures | Genesis: vastly more sophisticated (as far as we can tell). |
| **Autonomy model** | User approval for sensitive actions | Governance hierarchy, confirmation gates, autonomy levels | Genesis: more nuanced (L1-L4 autonomy levels, not binary). |
| **Search/research** | Perplexity's core competency | SearXNG + Brave search, web research orchestrator | Perplexity: stronger (it's their entire business). |
| **Shipping status** | Waitlist, Mac-only, $200/month | 1684 tests, runtime wired, Phase 7 complete | Genesis: more built, less shipped. |

### Architecture Impact

**Validates** our persistent-agent-in-container approach. The market is moving
our direction.

### Scope Tags

| Item | Scope | Phase | Priority |
|------|-------|-------|----------|
| Remote access beyond SSH tunnel | V4 | UX improvement | Medium |
| Credit-equivalent tracking (observability, not control) | V3 | Phase 2 (cost tracker already built) | Low |
| Perplexity as research tool (if API available) | Future | Integration | Low |

---

## 27. Claude Code Auto Mode & Free Interruptions (2026-03-12)

**Source:** [YouTube — Ray Amjad: "Anthropic Just Made Claude Code Interruptions Free"](https://www.youtube.com/watch?v=DqjBbAr3oTo)
**Supporting:** [VKTR — Claude Code Gets Auto Mode](https://www.vktr.com/ai-technology/claude-code-gets-auto-mode-for-longer-coding-sessions/)

### What It Is

Anthropic announced "Auto Mode" for Claude Code (March 6, 2026), targeting
enterprise developers. Auto mode lets Claude autonomously decide whether an
action requires user approval, rather than prompting at every step. This enables
longer, less-interrupted coding sessions.

Additionally, the Max plan tiers (5x at $100/month, 20x at $200/month) deliver
substantially fewer interruptions and longer sessions. The video title's claim
about "interruptions free" appears to refer to the reduced friction on Max plans,
not literally zero-cost interruptions.

### Four-Lens Evaluation

**Lens 1 — How It Helps:**
- Genesis uses CC Max as its primary intelligence layer. Auto mode directly
  benefits Genesis's CC session dispatch — background sessions (reflection,
  surplus, task execution) can now run with fewer permission interruptions,
  making autonomous operation smoother.
- The CC session infrastructure Genesis built (CCInvocation, budget tracker,
  allowed_tools/disallowed_tools) is designed for exactly this kind of
  autonomous operation. Auto mode is Anthropic catching up to what we need.
- Confirms our architectural bet on Claude Code as the intelligence runtime.
  Anthropic is investing heavily in making CC suitable for autonomous agent
  use — aligning with Genesis's dual-runtime model.

**Lens 2 — How It Doesn't Help:**
- Genesis already manages CC session permissions through its own infrastructure
  (allowed_tools, disallowed_tools on CCInvocation). Auto mode may conflict
  with Genesis's explicit permission management.
- "Enterprise" targeting suggests this might be gated behind enterprise
  contracts, not available on individual Max subscriptions. Need to verify
  availability.
- The "free interruptions" framing is misleading — interruptions still consume
  tokens and time, they just don't require manual approval. The cost doesn't
  change; the friction does.

**Lens 3 — How It COULD Help:**
- Auto mode's permission-decision model (Claude decides what needs approval)
  is conceptually similar to Genesis's autonomy levels (L1-L4). If Anthropic
  exposes the decision criteria, Genesis could potentially INFORM those
  criteria based on its own governance model — "allow all file reads, prompt
  for any outreach."
- If auto mode becomes configurable per-session, Genesis's CC dispatch could
  set different auto-mode profiles for different session types: aggressive
  auto for deep reflection (low risk), conservative for task execution
  (user-facing).
- The pricing trend ($100-200/month for premium agent capabilities) establishes
  a market ceiling for Genesis's operating costs. Genesis should track
  CC costs against this benchmark.

**Lens 4 — What to Learn:**
- Anthropic is explicitly designing Claude Code for autonomous agent use cases.
  This is strategic alignment — the platform we chose is evolving toward our
  use case.
- The "auto mode" vs "manual approval" distinction maps directly to Genesis's
  L1-L4 autonomy levels. Anthropic is discovering the same design space we
  designed for.
- Content creator coverage of CC features ("just made it free!") shows growing
  public awareness of agentic coding tools. Genesis is building on a platform
  with momentum.

### Architecture Impact

**Validates** our CC-as-intelligence-layer bet. Anthropic is actively improving
CC for exactly our use case.

### Scope Tags

| Item | Scope | Phase | Priority |
|------|-------|-------|----------|
| Verify auto mode availability on Max (not enterprise-only) | V3 | Immediate | Medium |
| Map auto mode config to CCInvocation dispatch | V3 | CC session infrastructure | Medium |
| Track CC pricing trends vs Genesis operating costs | V3 | Cost tracker (Phase 2) | Low |

---

## 28. NVIDIA Nemotron 3 Super — Hybrid Mamba-Transformer MoE for Agentic Reasoning (2026-03-12)

**Source:** [NVIDIA Developer Blog — Introducing Nemotron 3 Super](https://developer.nvidia.com/blog/introducing-nemotron-3-super-an-open-hybrid-mamba-transformer-moe-for-agentic-reasoning/)

### What It Is

NVIDIA's open-weights 120B total parameter / 12B active parameter model,
specifically designed for agentic AI applications. Key architecture innovations:

**Hybrid Mamba-Transformer MoE backbone:**
- Mamba-2 layers: linear-time sequence processing (enables 1M context)
- Transformer attention layers: precise associative recall
- MoE layers: scale parameters without dense compute costs

**Latent MoE:** Compresses token embeddings into low-rank latent space before
routing to experts. Claims 4x as many expert specialists for same inference cost.

**Multi-Token Prediction (MTP):** Predicts multiple future tokens simultaneously.
Stronger reasoning during training, built-in speculative decoding for up to
3x wall-clock speedups on structured generation.

**Native NVFP4 pretraining:** Trained natively in 4-bit floating point for
Blackwell GPUs. 4x memory/compute efficiency vs FP8 on H100.

**Training:** 25T total tokens (10T unique), 7M SFT samples from 40M corpus,
1.2M RL environment rollouts across 21 configurations.

**Performance:** 85.6% on PinchBench (autonomous agent benchmark), 5x throughput
vs previous Nemotron Super. Open weights, training recipes, and deployment
cookbooks.

### Four-Lens Evaluation

**Lens 1 — How It Helps:**
- **Awareness loop candidate.** 12B active parameters with 1M context window
  and agentic-reasoning training is EXACTLY what Genesis needs for the
  utility_model role. Currently we use Ollama SLMs (qwen3-embedding:0.6b for
  embeddings, small models for awareness ticks). Nemotron 3 Super could be a
  massive upgrade for micro/light reflection quality.
- **Open weights = self-hostable.** Genesis's design principle is "every model
  provider should be swappable." Nemotron 3 Super on local/cloud GPU via
  vLLM/TensorRT-LLM gives us a high-quality model we fully control.
- **1M context window** solves the "context explosion" problem the paper
  identifies — multi-agent systems generating 15x more tokens through
  re-sent history. Genesis's awareness tick context assembly currently works
  within small context windows. 1M opens up richer ticks.
- **Structured generation speedup** (3x via MTP speculative decoding) directly
  benefits Genesis's tool calling and JSON-structured outputs.

**Lens 2 — How It Doesn't Help:**
- **12B active params is still small.** For deep reflection, Sonnet/Opus are
  vastly more capable. Nemotron is a utility model, not a thinking model.
  Don't confuse "designed for agentic reasoning" with "good at reasoning."
  It's good at following agent patterns (tool calls, structured output,
  multi-step execution) — that's different from deep analytical reasoning.
- **Requires GPU.** Our Ollama sibling container is at ${OLLAMA_HOST:-localhost} and
  handles embeddings + SLM. Running Nemotron 3 Super requires a GPU capable
  of hosting 12B active params. Our current setup may not have the hardware.
  The NVFP4 optimization specifically targets Blackwell (B200) — we'd need
  FP8 or FP16 on older hardware.
- **85.6% on PinchBench** sounds good, but PinchBench is relatively new and
  we don't know how it correlates with real-world agent performance. Benchmark
  hype is rampant in the model space.
- **Training data concerns:** 25T tokens of training data means it's seen most
  of the internet. But agentic reasoning quality depends heavily on the RL
  phase (1.2M rollouts across 21 environments). Those environments may not
  match Genesis's specific tool-use patterns.

**Lens 3 — How It COULD Help:**
- **Compute routing hierarchy.** Genesis's compute router currently chains
  through cloud providers and falls back to local SLMs. Nemotron 3 Super
  could sit between "expensive cloud API" and "tiny local SLM" as a
  middle tier — good enough for light reflection, too expensive for awareness
  ticks, but much cheaper than Sonnet for tasks that need more than a 3B model.
- **Fine-tuning potential.** Open training recipes + open weights = Genesis
  could fine-tune Nemotron for its specific task patterns (awareness ticks,
  signal classification, memory activation scoring). This is V5/Future scope
  but the capability exists.
- **Edge deployment.** If Genesis ever runs on user hardware (not just a cloud
  container), a 12B model with 4-bit quantization is feasible on consumer
  GPUs (RTX 4090, Apple M-series). This opens Genesis beyond the current
  container deployment model.

**Lens 4 — What to Learn:**
- **Architecture innovation matters more than scale.** Nemotron achieves
  competitive performance with 12B active params (out of 120B total) through
  architectural innovation (Mamba + attention + MoE + latent routing + MTP).
  This is the opposite of "throw more parameters at it." Genesis's design
  philosophy (LLM-first, quality over cost) should appreciate that smart
  architecture beats brute force.
- **The "agentic reasoning" model category is emerging.** NVIDIA specifically
  trained for agent patterns: tool calling, multi-step execution, structured
  output, long-context analysis. This is a signal that the model market is
  specializing around agent use cases. Genesis benefits from this trend.
- **Mamba-Transformer hybrid is the architecture to watch.** Linear-time
  sequence processing (Mamba) + precise recall (Transformer) + parameter
  efficiency (MoE) — this combination may become the standard architecture
  for agent-oriented models. Worth tracking for compute routing decisions.
- **Open everything is NVIDIA's play.** Weights, recipes, datasets, deployment
  guides — NVIDIA is making the model ecosystem, not just the model. This is
  strategic positioning against closed-model providers. Aligns with Genesis's
  "flexibility > lock-in" principle.

### Competitive Position

| Dimension | Nemotron 3 Super | Genesis's current models | Honest Assessment |
|-----------|-----------------|------------------------|-------------------|
| **Agent benchmarks** | 85.6% PinchBench | N/A (we use frontier models) | Apples/oranges. Nemotron benchmarks its own agent capability; Genesis benchmarks its SYSTEM capability. |
| **Context window** | 1M tokens (native) | Model-dependent (200K for Claude, 1M for Gemini) | Nemotron competitive for long-context. But Genesis typically manages context carefully, not maxing windows. |
| **Cost/throughput** | 5x throughput vs prior, NVFP4 on Blackwell | API pricing (Claude, Gemini) | Self-hosted Nemotron potentially cheaper at scale. Depends on hardware availability. |
| **Reasoning depth** | Good for structured agent tasks, limited for deep analysis | Opus/Sonnet for deep work, SLM for ticks | Complementary. Nemotron fills the gap between SLM and frontier. |
| **Control** | Open weights, full stack | API dependency on Anthropic/Google | Nemotron: more control. But "control" at the cost of hardware management. |

### Architecture Impact

**Extends** our compute routing hierarchy. Nemotron 3 Super is a strong
candidate for the middle tier between SLM (awareness ticks) and frontier API
(deep reflection). Requires hardware assessment before adopting.

### Scope Tags

| Item | Scope | Phase | Priority |
|------|-------|-------|----------|
| Evaluate GPU availability for 12B model hosting | V3 | Infrastructure assessment | Medium |
| Add Nemotron to compute routing as middle-tier option | V3 | Phase 2 (compute routing) | Medium |
| Benchmark Nemotron on Genesis-specific tasks (signal classification, memory activation) | V3 | After hosting assessment | Medium |
| Fine-tune for Genesis task patterns | V5/Future | Post-V5 | Low |
| Track Mamba-Transformer hybrid architecture evolution | Ongoing | Research | Low |

---

## 26. AGI Gap Analysis & Bayesian Calibration (2026-03-12)

### Context

Systematic gap analysis comparing Claude Code's AGI-like capabilities against
Genesis's architecture. Eight gaps identified, scored for Genesis coverage,
and 21 actionable items categorized across V3/post-V3/post-V5.

### Source: Google Bayesian Teaching Research (YouTube — AI news compilation)

**What it is:** Google research on "Bayesian Teaching" — fine-tuning LLMs to
imitate the behavior of a symbolic Bayesian system that properly updates beliefs
from sequential evidence. Results: fine-tuned models (Gemma 2, Llama 3) achieved
~80% alignment with optimal Bayesian strategy, generalized cross-domain.

**Key insight — "One-and-Done Plateau":** LLMs fail to update beliefs from
sequential interactions. They hit a plateau after the first round of evidence.
This is a named, researched phenomenon that directly justifies Genesis's
architectural approach to calibration.

**Four-lens evaluation:**

- **Helps:** Validates Genesis's calibration system design (items 1-4). Confirms
  that LLMs need external architecture to compensate for belief-updating failure.
  The "symbolic + neural" synergy thesis matches Genesis's "code computes, LLMs
  interpret" philosophy.
- **Doesn't help:** Requires fine-tuning to fully implement. Can't be applied to
  Opus/Sonnet (closed models). Structured preference domains (flights, hotels)
  don't map cleanly to Genesis's messier decision domains.
- **Could help:** Bayesian Teaching should be the specific methodology for item
  #20 (RAFT) — generate training data from a mathematical Bayesian model fitted
  to Genesis's operational calibration data, then fine-tune 7B on that. Also:
  the Bayesian Assistant concept is exactly what items 1-3 implement at the
  architecture level (prediction logging + calibration curves = symbolic system
  that tracks belief accuracy).
- **Learn from it:** Symbolic systems (precise, rigid) + neural networks
  (flexible, imprecise) = better than either alone. This is empirical validation
  of Genesis's core architectural split.

### Source: ByteDance DeerFlow 2.0

**What it is:** Open-source multi-agent framework for autonomous task execution.
Main agent → sub-agents → recombine. Persistent memory. Model-agnostic.

- **Validates:** Genesis's centralized orchestration topology, persistent memory
  as differentiator, model-agnostic routing.
- **Could help:** Sandboxed execution pattern for Phase 9 autonomous tasks.

### Source: NVIDIA NemoClaw

**What it is:** Enterprise AI agent platform. Security-focused, hardware-agnostic.

- **Validates:** Governance checks and autonomy levels approach.
- **Low direct relevance** — enterprise focus, different problem space.

### AGI Gap Scorecard

| Gap | Genesis Coverage | Key Missing Pieces |
|-----|-----------------|-------------------|
| 1. Goal persistence | ~70% | Phase 8-9 task execution |
| 2. Learning from experience | ~60% | Skill evolution (Ph7), stability monitoring |
| 3. Self-evaluation | ~50% | Calibration system (Ph8-9), disagreement gates |
| 4. Unbounded memory | ~40% | Knowledge graph layer (V4) |
| 5. Proactive behavior | ~75% | Outreach pipeline (Ph8), autonomous tasks (Ph9) |
| 6. Multi-agent collaboration | ~30% | Topology selection (V4) |
| 7. Calibrated uncertainty | ~40% | Bayesian calibration (Ph8-9) |
| 8. Generalization | ~55% | More domain skills, recon pipeline |

### Actionable Items

#### V3 Phase 8 (instrumentation — start logging early for data accumulation):

- [ ] **Prediction logging table** — `{action_id, prediction, confidence,
  confidence_bucket, domain, reasoning, outcome, correct}` on every non-trivial
  decision. Zero LLM cost, pure instrumentation. Priority: high.
- [ ] **Prediction-outcome reconciliation batch job** — surplus compute job that
  matches predictions to outcomes from execution traces, user engagement, procedure
  success/failure. Priority: high.
- [ ] **Calibration curve computation** — group predictions by confidence bucket,
  compute actual success rate vs predicted confidence per domain. Pure programmatic.
  Priority: high.

#### V3 Phase 9 (use calibration data for autonomy decisions):

- [ ] **Calibration feedback injection** — include calibration history in context
  assembly: "when you report 80% confidence on outreach, you're right ~60% of
  the time." Priority: high.
- [ ] **Hard disagreement gates** — when cross-vendor review (call sites #17/#20)
  disagrees with primary, block action until resolved (third model or user).
  Promote from deferred pattern (design doc line 2640) to core infrastructure.
  Priority: high.
- [ ] **Decision trace verification** — for structured decisions (routing, triage,
  procedure selection), mechanically verify stated reasons match actual data.
  No LLM needed. Priority: medium.

#### Post-V3 Design Document (items 7-16):

- [ ] **Typed edges on memory_links** — richer link types (caused_by, relates_to,
  contradicts, supersedes, part_of, instance_of). Phase: V4. Priority: medium.
- [ ] **Entity extraction on memory store** — extract people, projects, tools,
  concepts and auto-link. Phase: V4. Priority: medium.
- [ ] **Graph traversal helpers** — recursive CTEs in SQLite, 2-3 hop walks.
  Phase: V4. Priority: medium.
- [ ] **Pydantic ontology schemas** — Cognee pattern for domain relationship
  rules. Phase: V4. Priority: low.
- [ ] **Monte Carlo sampling** — run same prompt 5x with temperature > 0 for
  critical decisions. Phase: V4. Priority: medium.
- [ ] **Ensemble uncertainty scoring** — cross-vendor agreement rate as calibrated
  confidence proxy. Phase: V4. Priority: medium.
- [ ] **Shared scratchpad for multi-agent coordination** — shared document for
  agents on related sub-tasks. Phase: V4. Priority: low.
- [ ] **Event bus integration** — IMAP IDLE, GitHub webhooks, RSS for real-time
  event-driven awareness. Phase: V4. Priority: medium.
- [ ] **Hierarchical memory summarization** — individual → topic → domain →
  cross-domain summaries. Phase: V4. Priority: low.
- [ ] **Debate protocol** — two agents argue, third adjudicates. For high-stakes
  decisions. Phase: V4. Priority: low.

#### Post-V5 Design Document (items 17-21):

- [ ] **LoRA fine-tune 7B on Genesis operational data** — personal model on
  (situation, action, outcome) tuples after 6 months. **High value.** Priority: high.
- [ ] **Train reward model on engagement signals** — small model predicting user
  satisfaction from engagement data. Priority: medium.
- [ ] **Train critic/verifier model** — fine-tune on (reasoning_chain, outcome)
  pairs to predict outcome quality. Priority: medium.
- [ ] **RAFT with Bayesian Teaching** — fine-tune 7B using Bayesian Teaching
  methodology (Google research). Generate training data from mathematical Bayesian
  model fitted to Genesis calibration data. ~80% alignment with optimal strategy,
  cross-domain generalization proven. **Highest value.** Priority: high.
- [ ] **Conformal prediction sets** — formal coverage guarantees on prediction
  sets. Priority: low.

### Scope Tags

| Item | Scope | Phase | Priority |
|------|-------|-------|----------|
| Prediction logging (1-3) | V3 | Phase 8 | **High** |
| Calibration feedback + gates (4-6) | V3 | Phase 9 | **High** |
| Knowledge graph layer (7-10) | V4 | Post-V3 design | Medium |
| Monte Carlo / ensemble (11-12) | V4 | Post-V3 design | Medium |
| Multi-agent patterns (13, 16) | V4 | Post-V3 design | Low |
| Event bus (14) | V4 | Post-V3 design | Medium |
| Hierarchical memory (15) | V4 | Post-V3 design | Low |
| LoRA fine-tuning (17) | Post-V5 | Post-V5 design | High |
| Reward model (18) | Post-V5 | Post-V5 design | Medium |
| Critic model (19) | Post-V5 | Post-V5 design | Medium |
| RAFT + Bayesian Teaching (20) | Post-V5 | Post-V5 design | **High** |
| Conformal prediction (21) | Post-V5 | Post-V5 design | Low |
| DeerFlow sandboxed execution | V3 | Phase 9 | Low |

---

---

## 27. Daniel Miessler's PAI (Personal AI Infrastructure) — Deep Dive

### Source

GitHub: `danielmiessler/Personal_AI_Infrastructure` (open-source, active development)
DeepWiki documentation pages (architecture, hooks, voice system, memory)

### What PAI Is

An agentic AI infrastructure built entirely on Claude Code hooks. TypeScript
lifecycle hooks intercept CC events (SessionStart, UserPromptSubmit, PreToolUse,
Stop, SessionEnd) to create a stateful, goal-centric overlay. Includes The
Algorithm (7-phase mandatory workflow), TELOS identity files, SIGNALS rating
system, voice notifications via ElevenLabs, ISC (Ideal State Criteria) for task
verification, and a file-based memory system.

### 8-Dimension Comparison (Genesis vs PAI)

#### Dimension 1: Persistent Identity

| Aspect | PAI | Genesis | Assessment |
|--------|-----|---------|------------|
| Identity depth | TELOS (10 markdown docs) + OPINIONS.md + AISTEERINGRULES.md | SOUL.md (deep philosophy) + Four Drives + User Model | Genesis is more principled; PAI is more operationally granular |
| Opinions | Explicit OPINIONS.md — deterministic stances on recurring topics | No explicit mechanism — opinions reconstructed from memory | **Gap: add OPINIONS.md equivalent in V4** |
| Adaptability | TELOS manually edited; steering rules auto-generated from low ratings | SOUL.md protected (L7); drives adapt via Self-Learning Loop with bounds | Genesis's adaptation is architecturally richer |

**Action item:** V4 — OPINIONS.md equivalent. Self-Learning Loop proposes opinion
updates based on consistent patterns; user approves.

#### Dimension 2: Skills & Workflows

| Aspect | PAI | Genesis | Assessment |
|--------|-----|---------|------------|
| Day-one utility | 63 pre-authored skills | Zero — must learn from scratch | PAI wins cold start |
| Learning | Static unless manually updated | Procedural memory evolves from outcomes | Genesis wins long-term |
| Format | Fabric patterns with input/output contracts | Flat procedure records | Fabric's contract format is more structured |

**Action items:**
- Study PAI's 63 skills for Genesis-applicable patterns (research task, not implementation)
- V4: Procedure graduation system — after 3+ validated similar successes, generalize
  into structured workflow with input/output contracts and documented variations
- V4: Consider starter-pack seeding for procedural memory (not pre-authored skills,
  but high-confidence default procedures at low confidence scores)

#### Dimension 3: SIGNALS / Learning from Ratings

| Aspect | PAI | Genesis | Assessment |
|--------|-----|---------|------------|
| Explicit ratings | 1-10 prompted at session end (3,540+ entries) | None designed | Different philosophy — we prefer implicit |
| Implicit sentiment | ImplicitSentimentCapture from conversation | Signal weight tiers (strong/moderate/weak) | Similar intent, different mechanism |
| Speed of reaction | Low rating → auto-generate steering rule (immediate) | Signal → calibration cycle → gradual adjustment | PAI faster; Genesis less prone to overfitting |

**Action items:**
- V3 Phase 9 or V4: Natural language sentiment extraction in triage pass. Classify
  user messages into strong_positive/positive/neutral/negative/strong_negative.
  3B SLM handles alongside depth assignment. Maps to 1-5 without asking.
- V3 Phase 9: Fast-path steering rules from explicit strong negative feedback.
  When user clearly says "never do X," create immediate observation tagged
  `steering_rule` that gets injected into future context. Don't wait for
  calibration cycles on unambiguous corrections.

#### Dimension 4: Proactive Behavior & Task Structure

| Aspect | PAI | Genesis | Assessment |
|--------|-----|---------|------------|
| Work detection | AutoWorkCreation hook detects items from conversation | No equivalent — Self-Learning Loop is post-interaction | **Gap: work item detection during/after conversation** |
| Structured execution | The Algorithm: 7 mandatory phases, every task | Proportionate effort — LLM decides depth | PAI is more reliable; Genesis is more flexible but prone to cutting corners |
| Background processing | None — only active during CC sessions | Awareness Loop + surplus compute between sessions | Genesis is genuinely proactive |

**Revised recommendation (per user direction):** For ANY task entering the task
execution pipeline (not foreground conversation), planning, verification, and
learning are **mandatory — not optional.** The LLM decides HOW MUCH of each,
not WHETHER they happen. This is a minimum structural floor:

1. **Planning**: Always. Define what you're doing and how.
2. **Verification**: Always. Confirm output matches plan.
3. **Learning**: Always. Extract what worked and what didn't.

This applies to background sessions, surplus tasks, and any dispatched CC work.
Foreground conversation is exempt (it has its own retrospective via Self-Learning Loop).

**Action items:**
- V3 Phase 9: Work item detector — runs during or immediately after conversation,
  identifies actionable items, stages them in surplus queue with conversation context
- V3 Phase 9: Mandatory planning/verification/learning for all task pipeline items
- Connect work detection → surplus staging → background CC execution → outreach delivery

#### Dimension 5: Determinism Patterns

| Aspect | PAI | Genesis | Assessment |
|--------|-----|---------|------------|
| Steering rules | AISTEERINGRULES.md — cross-cutting behavioral directives, 3 levels | No equivalent first-class concept | **Gap: STEERING.md mechanism** |
| ISC | Binary testable criteria, quality gates QG1-QG7 | Quality gates + Pre-Execution Assessment | ISC is cleaner for autonomous verification |
| Hook enforcement | TypeScript hooks enforce invariants programmatically | Mix of hooks + LLM assessment | PAI's enforcement is stronger for what it covers |

**Action items:**
- V3 (current): STEERING.md mechanism — file where Self-Learning Loop writes
  cross-cutting behavioral rules from detected patterns. Injected into context.
- V4: ISC gates on autonomous tasks (already scoped in build phases doc)
- V4: Expanded hook coverage for cognitive invariants

#### Dimension 6: Memory Architecture

| Aspect | PAI | Genesis | Assessment |
|--------|-----|---------|------------|
| Complexity | Flat files in directories | Vector DB + hybrid search + FTS5 | Genesis has more infrastructure and more failure modes |
| Scale | Limited by context window | Scales to thousands of items | Genesis wins at scale |
| Consolidation | None — accumulates forever | Consolidation during Deep reflection | Genesis maintains quality better but risks destroying nuance |

**Action items:**
- V3 Phase 9: Test embedding-fallback retrieval quality explicitly
- V5 start: Monitor consolidation quality — are Deep reflections destroying useful
  nuance? Establish quality metrics before V5 ramps up operational volume.

#### Dimension 7: Claude Code Integration / Hooks

| Aspect | PAI | Genesis | Assessment |
|--------|-----|---------|------------|
| Lifecycle hooks | 5 hooks covering full CC lifecycle | Partial — security hooks + dynamic CLAUDE.md | **Gap: adopt hooks as primary infrastructure** |
| Context loading | LoadContext hook — guaranteed, every session | Dynamic CLAUDE.md — flexible but brittle | Hook-based is more reliable |
| Session state | AutoWorkCreation + WorkCompletionLearning | No CC-lifecycle-level state management | **Gap: session state hooks** |

**Revised recommendation (per user direction):** Adopt PAI's hook lifecycle as
**primary infrastructure**, not fallback. Implement CC hooks for:

- **SessionStart**: Load identity, steering rules, critical memory, task context
- **UserPromptSubmit**: Work item detection, session state capture
- **PreToolUse**: Security validation + cognitive invariant enforcement
- **Stop**: Post-response learning trigger, state persistence
- **SessionEnd**: Full retrospective trigger, session cleanup

These hooks don't need to be complex TypeScript — shell scripts calling into
Genesis Python infrastructure. The lifecycle guarantees are what matter.
Phase 9 scope (requires task execution + learning infrastructure to be operational).

#### Dimension 8: Context Pollution Management

| Pattern | PAI | Genesis | Assessment |
|---------|-----|---------|------------|
| `/btw` | Quick note without derailing current task | No equivalent | **Implement in Phase 9** |
| `/fork` | Fork conversation into side thread | No equivalent | V4 — investigate feasibility |
| `/rewind` | Roll back to previous conversation state | No equivalent | V4 — investigate feasibility |

**Action item:** Phase 9 — implement `/btw` as a skill (store note to memory,
continue current task).

### Voice System Technical Details

PAI runs a standalone voice server (`VoiceServer/server.ts`) on localhost:8888:

- **Endpoints**: `/notify`, `/notify/personality`, `/pai`, `/health`, `/shutdown`
- **TTS**: ElevenLabs with 3-tier voice settings resolution (caller override → profile → default)
- **Prosody**: 13 emotional presets via emoji markers; pronunciation rules from `pronunciations.json`
- **Multi-voice**: Per-identity voice profiles in settings.json
- **Fallback**: macOS built-in voices when no API key
- **Rate limiting**: 10 requests/60s per IP
- **Subagent gate**: VoiceGate hook prevents child agents from triggering TTS
- **Integration**: StopOrchestrator hook detects Algorithm phase headers, POSTs to voice server

**Relevance to Genesis TTS work:**
- Our TTS is tightly coupled to Telegram handler — needs decoupling for outreach/background use
- 3-tier voice settings resolution worth adopting (caller override → profile → env default)
- VoiceGate pattern needed for background session safety
- Standalone notification server pattern useful for multi-channel delivery

### Consolidated PAI Action Items

| Item | Scope | Phase/Version | Priority |
|------|-------|---------------|----------|
| STEERING.md mechanism (cross-cutting behavioral rules) | V3 | Current | **High** |
| Study PAI's 63 skills for Genesis-applicable patterns | Research | Current | Medium |
| Natural language sentiment extraction in triage | V3/V4 | Phase 9 | **High** |
| Fast-path steering rules from explicit negative feedback | V3 | Phase 9 | **High** |
| Work item detector (conversation → surplus staging) | V4 | — | Medium |
| Mandatory planning/verification/learning for task pipeline | V3 | Phase 9 | **High** |
| CC lifecycle hooks as primary infrastructure | V3 | Phase 9 | **High** |
| `/btw` skill | V3 | Phase 9 | Medium |
| TTS decoupling — voice notification service pattern | V3 | Current (TTS work) | Medium |
| TTS 3-tier voice settings + VoiceGate | V3 | Current (TTS work) | Medium |
| OPINIONS.md equivalent | V4 | — | Medium |
| Procedure graduation system (memory → workflow) | V4 | — | Medium |
| ISC gates on autonomous tasks | V4 | — | Medium (already scoped) |
| Expanded hook coverage for cognitive invariants | V4 | — | Medium |
| `/fork` and `/rewind` feasibility investigation | V4 | — | Medium |
| Starter-pack procedure seeding | V4 | — | Low |
| Monitor consolidation quality | V5 | V5 start | Medium |

---

---

## 7. Competitive Landscape Scan — 2026-03-14

Research session evaluating 6 external developments against Genesis architecture.
Full evaluations below; actionable items routed to specific documents.

### 7.1 Claude Code Free Compute Sources

**Finding:** Various workarounds exist for CC usage without Max subscription
(GitHub Student Pack, API credit fallback, educational programs). None provide
the autonomous session dispatch capability Genesis requires. However, the
**surplus system** can leverage free compute APIs (already cataloged in models.md
Free Tier Terms section) as alternatives when CC is rate-limited.

**Action:** Tracked in `docs/plans/post-v5-horizon.md` §7. The existing
router + free tier infrastructure already supports routing surplus work to
free APIs. Implementation is Phase 9+ (surplus system must be operational first).

### 7.2 Replit Agent 4 — Canvas & Parallel Agents

**Finding:** Replit launched Agent 4 ($400M raise, $9B valuation) with an
"infinite canvas" visual workspace and parallel agent execution. The Canvas
concept (work products as movable/annotatable cards) and parallel progress
visibility are worth stealing for Genesis's dashboard evolution.

**Architecture impact:** Validates our parallel CC session dispatch pattern.
Canvas UX concepts tracked in `docs/plans/post-v5-horizon.md` §5.

### 7.3 Gemini Embedding 2

**Finding:** Google's first natively multimodal embedding model. 3072 dims
(Matryoshka to 768), text/image/video/audio/PDF, 8192 token input, benchmark
leader. Qdrant compatible.

**Decision:** Do NOT adopt now. Reasons:
1. 1024 dims is the sweet spot for Genesis's content type. Research confirms
   accuracy curve flattens between 768–1024. Going to 3072 triples storage
   with marginal gains.
2. Cloud API = privacy concern (all memory content to Google).
3. Cost per embedding operation conflicts with local-first philosophy.
4. Monitor for local alternatives at ~1024 dims with comparable quality.

**Action:** Full migration analysis tracked in `docs/plans/post-v5-horizon.md` §1.

### 7.4 GPT-5.4

**Finding:** OpenAI's latest frontier model. 1M context, built-in computer use,
compaction training (purpose-built for long agent trajectories), 33% fewer
false claims than GPT-5.2, strong on tool-heavy workloads.

**Decision:** Add to model pool as option for:
- Computer-use tasks (first mainline model with this capability)
- Long-context agentic work (compaction training advantage)
- Disagreement gate partner (V4, different training biases from Claude)

**Action:** Added to `docs/reference/models.md`. Available via OpenRouter.

### 7.5 AWS Bedrock AgentCore

**Finding:** Managed agent platform with Memory (short/long-term + streaming
notifications), Browser Tool (Firecracker microVM isolation + live view +
human takeover), Gateway (REST API → MCP auto-conversion), Policy (natural
language → Cedar enforcement).

**Decision:** Do NOT adopt the service (managed dependency violates Genesis
philosophy). Pattern-steal:
- **API-to-MCP gateway** concept for V4 (auto-convert REST APIs to MCP tools)
- **Browser live view + takeover** for post-V5 dashboard evolution
- **Streaming memory notifications** validates our EventBus pattern

**Action:**
- Browser upgrade tracked in `docs/plans/post-v5-horizon.md` §2
- API-to-MCP gateway noted for V4 planning

### 7.6 AWS Strands Agents SDK

**Finding:** Open-source agent SDK (14M+ downloads). Model-driven orchestration
(LLM plans and decides, not explicit workflows). 13+ model providers, native
MCP support, multi-agent patterns (handoffs, swarms, A2A), hot-reload tools
from directory. Strands Labs: AI Functions (natural-language-defined capabilities
with auto-validation), Robots (physical hardware integration).

**Decision:** Do NOT adopt the framework. Genesis is on Agent Zero. But the
design philosophy validates our LLM-first approach. Pattern-steal:
- **Hot-reload tool discovery** from directory (drop file → auto-register)
- **AI Functions** concept for runtime capability expansion
- **A2A protocol** for future multi-agent coordination

**Action:** All items tracked in `docs/plans/post-v5-horizon.md` §3, §4, §6.

### 7.7 Context Mode MCP Server (Session Continuity)

**Finding:** Context Mode (github.com/mksglu/context-mode) is an MCP server
that solves two problems: context consumption (98% reduction via sandboxed
execution — Playwright 56KB→299B) and session fragmentation (structured event
capture + priority-tiered snapshots survive compaction).

**Architecture:** PostToolUse captures file/git/task/error events into SQLite,
PreCompact builds ≤2KB XML snapshot (critical: active files/tasks/rules;
high: git ops/errors; normal: MCP tool counts; low: intent/role), SessionStart
restores via FTS5-indexed event history + 15-category Session Guide.

**Decision:** Adopt the session continuity hook pattern for Genesis's CC
session dispatch. The sandbox execution pattern deferred to V4.

**Action:**
- Session continuity hooks added to `genesis-v3-build-phases.md` Phase 9
- Sandbox execution pattern added to `v4-research-driven-features-spec.md` §6

### 7.8 Anthropic Tool Search API

**Finding:** Server-side tool search enables deferred tool loading — send all
tool definitions with `defer_loading: true`, Claude discovers tools on-demand
via regex or BM25 search. Reduces tool definition context by 85%+. Supports
up to 10,000 tools. Works with MCP servers via `mcp_toolset`. Custom
implementation supported (return `tool_reference` blocks from your own
search — could use Genesis's Qdrant embeddings).

**Decision:** Implement in V4 when tool count approaches 30+. Complements
hot-reload tool discovery.

**Action:** Added to `v4-research-driven-features-spec.md` §5.

### 7.9 Claude Cowork

**Finding:** Anthropic's product bringing CC agentic capabilities to Claude
Desktop. Scheduled tasks, context files, plugins. Requires computer awake —
sleep terminates sessions.

**Decision:** Irrelevant to Genesis. Consumer product, different problem space.
Validates our awareness loop and CLAUDE.md context file patterns.

### Consolidated Action Items — 2026-03-14

| Item | Document | Scope | Priority |
|------|----------|-------|----------|
| GPT-5.4 model entry (replaces GPT-5.2) | `docs/reference/models.md` | V3 | **Done** |
| CC session continuity hooks | `genesis-v3-build-phases.md` Phase 9 | V3 Phase 9 | **High** |
| CC free compute fallback for surplus | `genesis-v3-build-phases.md` V3 Closing Infra | V3 Post-Phase 9 | Medium |
| Hot-reload tool discovery | `v4-research-driven-features-spec.md` §1 | V4 | Medium |
| AI Functions / runtime capability expansion | `v4-research-driven-features-spec.md` §2 | V4 | Medium |
| A2A protocol | `v4-research-driven-features-spec.md` §3 | V4 | Medium |
| API-to-MCP gateway | `v4-research-driven-features-spec.md` §4 | V4 | Medium |
| Tool Search API (deferred loading) | `v4-research-driven-features-spec.md` §5 | V4 | Medium |
| Context-efficient CC sessions (sandbox) | `v4-research-driven-features-spec.md` §6 | V4 | Medium |
| Embedding migration (full cutover) | `post-v5-horizon.md` §1 | Post-V5 | Monitor |
| Browser live view + takeover | `post-v5-horizon.md` §2 | Post-V5 | Monitor |
| Canvas dashboard evolution | `post-v5-horizon.md` §3 | Post-V5 | Monitor |

---

*Document created: 2026-03-08*
*Last updated: 2026-03-14*
*Status: Living document — updated during research conversations*
