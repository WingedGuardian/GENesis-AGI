<!-- SUPERSEDED: 2026-03-07 -->
> **Note (2026-03-07):** The three-engine model described in this document (AZ as brain +
> Claude SDK as power tool + OpenCode as backup) has been superseded by the dual-runtime
> architecture in `docs/plans/2026-03-07-agentic-runtime-design.md`. Claude Code (Max
> subscription) is now the primary intelligence layer; Agent Zero serves as infrastructure
> (dashboard, relay, health monitoring). This document is retained as decision history.

# Genesis v3: Agent Zero + Claude SDK Architecture Plan

**Status:** Superseded | **Last updated:** 2026-03-07


**Date:** 2026-02-22
**Status:** APPENDIX — Superseded as active plan; retained as decision history and audit trail

> **Document status (2026-02-23):** This document is **not the current plan.** It is
> an appendix that preserves the decision history of how we converged on the Genesis v3
> architecture. The active design documents are:
> - `genesis-v3-autonomous-behavior-design.md` — the primary v3 architecture document
> - `genesis-v3-gap-assessment.md` — pre-implementation gaps and open questions
>
> This document remains a useful reference for: framework decision rationale (why Agent
> Zero), three-engine concept, memory system MCP wrapping approach, CLAUDE.md handshake
> protocol, migration plan outline, container architecture, and risk assessment. But
> specific sections (scheduler, MCP server list, review cycle, code execution economics)
> have been superseded or revised by the documents above.
>
> **Do not use this document for current design decisions.** When it conflicts with the
> autonomous behavior design doc or gap assessment, those documents take precedence.

---

## 1. Executive Summary

Replace nanobot with **Agent Zero** as Genesis's core framework, supplemented by **Claude Agent SDK** for high-stakes code work and **OpenCode** as a fallback when Claude rate limits are exhausted. Port Genesis's superior memory system as an MCP server. Rebuild the cognitive layer (heartbeat, dream, recon, reviews) as Agent Zero extensions and scheduled tasks.

**One sentence:** Agent Zero is the brain. Claude SDK is the power tool. OpenCode is the backup tool. Your memory system is the shared nervous system.

---

## 2. Why Agent Zero Over Nanobot

### Philosophy alignment

| Principle | Nanobot (current) | Agent Zero |
|-----------|-------------------|------------|
| LLM-first | Bolt-on via copilot hooks into a chat framework | Core architecture — everything is prompt-driven |
| Model agnostic | Custom RouterProvider + failover chain | LiteLLM native, 100+ providers, 4 model roles |
| Multi-agent | Custom SubagentManager, `process_direct()` contention | Hierarchical subordinate agents, clean context isolation |
| Memory/learning | Sophisticated (Qdrant + FTS5 + SQLite) but tangled into nanobot | Simple FAISS (we replace with our superior system via MCP) |
| Extensibility | Copilot hooks bolted into loop.py | First-class extension lifecycle hooks |
| Tool system | Custom tool classes in nanobot | Built-in + custom + MCP, unified interface |
| Self-modification | Possible but constrained by framework | Native — can install/run tools dynamically |
| Security | Runs on host | Docker or bare-metal-in-container isolation |

### What we stop fighting

Nanobot is a chat framework we've been stretching into an autonomous cognitive system. Every feature requires hooking into `loop.py`, managing `process_direct()` race conditions, working around `skip_enrichment`, and navigating the upstream merge backlog. Agent Zero IS an autonomous agent framework — the architecture assumes everything we've been adding.

---

## 3. Architecture

```
┌─── Incus Container: genesis-v3 (NEW) ─────────────────────────┐
│                                                                 │
│  Agent Zero (bare metal — Incus IS the sandbox)                 │
│  ├── LiteLLM (model routing + fallback chains)                  │
│  │   ├── chat_model: configurable per task (Opus/Sonnet/Flash)  │
│  │   ├── utility_model: Haiku/Flash (parsing, extraction)       │
│  │   ├── embeddings_model: text-embedding-3-small               │
│  │   └── browser_model: Sonnet (vision-capable)                 │
│  │                                                              │
│  ├── Extensions (Genesis cognitive layer):                      │
│  │   ├── memory_injection (before_llm_call → MCP recall)        │
│  │   ├── memory_storage (message_loop_end → MCP store)          │
│  │   ├── heartbeat_tick (system_prompt enrichment)              │
│  │   ├── identity_files (SOUL.md loading + evolution)           │
│  │   └── situational_briefing (active tasks, spend, alerts)     │
│  │                                                              │
│  ├── Scheduler (cron):  ← SUPERSEDED (see note below)                                          │
│  │   ├── heartbeat: every 2h                                    │
│  │   ├── dream_cycle: daily 3 AM (13 jobs as subordinates)      │
│  │   ├── weekly_review: Sunday 9 AM                             │
│  │   ├── monthly_review: 1st of month 10 AM                    │
│  │   ├── recon jobs: various (email, web, GitHub, models)       │
│  │   └── health_check: every 30min (pure Python extension)      │
│  │                                                              │
│  ├── Custom Tools:                                              │
│  │   ├── claude_code — Claude Agent SDK for code work           │
│  │   │   └── fallback: Bedrock → Vertex → opencode_fallback    │
│  │   ├── opencode_fallback — OpenCode for when Claude limited   │
│  │   └── (standard: code_execution, knowledge, browser, etc.)   │
│  │                                                              │
│  ├── MCP Clients:  ← SUPERSEDED (see note below)                                               │
│  │   ├── genesis-memory — YOUR Qdrant+FTS5+SQLite system        │
│  │   ├── genesis-observations — dream_observations lifecycle    │
│  │   ├── genesis-recon — recon_findings + triage                │
│  │   └── (future: email, calendar, etc.)                        │
│  │                                                              │
│  └── Web UI (React + WebSocket — built into Agent Zero)         │
│                                                                 │
│  Qdrant (vector DB — can share with v1 or fresh instance)       │
│  SQLite (observations, recon, cost tracking, task state)        │
│                                                                 │
│  Interface Relay:                                               │
│  └── Telegram bot → Agent Zero WebSocket API                    │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘

┌─── Incus Container: genesis-v1 (EXISTING — stays running) ─────┐
│  nanobot + all current services                                 │
│  Keeps running until v3 is ready for cutover                    │
│  Qdrant can be shared between containers if desired             │
└─────────────────────────────────────────────────────────────────┘
```

> **SUPERSEDED — Scheduler:** The cron-based scheduler above is replaced by the Awareness Loop +
> Reflection Engine in `genesis-v3-autonomous-behavior-design.md`. The event-driven model with
> adaptive calendar floors/ceilings is the target architecture, not fixed cron intervals.

> **SUPERSEDED — MCP Clients:** The 3-server list above (genesis-memory, genesis-observations,
> genesis-recon) is replaced by the 4-server design (memory-mcp, recon-mcp, health-mcp, outreach-mcp)
> in the autonomous behavior design doc. Observations are folded into memory-mcp.

### The Three Engines

| Engine | Role | When Used |
|--------|------|-----------|
| **Agent Zero** (primary) | Brain, orchestrator, memory, scheduling, 80% of all work | Always — every interaction starts here |
| **Claude Agent SDK** (power tool) | High-quality code editing, multi-file refactoring, complex builds | When Agent Zero's `claude_code` tool is invoked — for tasks, weekly/monthly code reviews, self-modification |
| **OpenCode** (backup tool) | Same capabilities as Claude SDK but model-flexible | When Claude rate limits are exhausted across all paths (direct → Bedrock → Vertex) |

### Code Execution Economics (2026-02-23)

> **This section was added after the original plan and reflects updated understanding
> of Claude Agent SDK pricing.**

The original three-engine design assumed Claude SDK usage would be offset by a Pro/Max
subscription. **This is not the case.** Per Anthropic's TOS:

> "Unless previously approved, Anthropic does not allow third party developers to offer
> claude.ai login or rate limits for their products, including agents built on the
> Claude Agent SDK."

Claude SDK bills at API rates: ~$15/$75 per MTok (Opus), ~$3/$15 per MTok (Sonnet).
An hour of heavy agentic code work can cost $2-10 at Opus rates. This fundamentally
changes the economics of the three-engine model:

**Revised engine roles:**

| Engine | Original Role | Revised Role |
|--------|--------------|-------------|
| **Agent Zero + LiteLLM** | Brain, orchestrator, 80% of work | Brain, orchestrator, **AND primary code engine** for routine work |
| **Claude Agent SDK** | Power tool for all code work | **Premium tool** — reserved for complex multi-file refactoring, architectural work, tasks where quality justifies $5-10 |
| **OpenCode** | Backup when Claude rate-limited | **Primary alternative code engine** — model-flexible, can use cost-efficient providers for routine code tasks |
| **Claude Code (subprocess)** | Not in original plan | **Experimental** — Agent Zero invokes `claude` CLI as subprocess, which uses subscription OAuth. Architecturally messy but economically attractive. Worth attempting in Phase 0. Could be closed by Anthropic at any time. |

**Key implications:**
1. **Cost consciousness is a first-class feature.** Before spawning Claude SDK for a
   task, Genesis must estimate cost and notify the user: "This task will use Claude SDK
   and may cost $5-10 to complete. Proceed?" This is a governance check, not a UX
   nicety.
2. **OpenCode is promoted from fallback to workhorse.** For routine code tasks (add a
   function, fix a bug, write tests), OpenCode with Sonnet/GPT-4o through its own
   routing may be more cost-effective than Claude SDK at API rates.
3. **Claude Code as subprocess is worth attempting.** If Agent Zero can invoke the
   `claude` CLI binary and capture its output, this uses the user's subscription
   allocation. This is not SDK usage — it's running Anthropic's own CLI product. The
   risk: Anthropic could restrict automated CLI invocation, or the CLI's interactive
   nature may not map cleanly to Agent Zero's tool interface. Test in Phase 0.
4. **Bedrock/Vertex become cost-optimization paths, not just fallbacks.** Volume
   pricing, committed use discounts, and different rate structures may make these the
   economically rational primary routing targets for SDK-level work.

### Why OpenCode is promoted in this architecture

OpenCode is no longer just a fallback. It's the cost-effective code engine for routine
work:

- **Model-flexible**: Can use Sonnet, GPT-4o, Gemini, or any LiteLLM-supported model
- **No vendor lock-in**: Not tied to Anthropic API pricing
- **Good enough for 80% of code tasks**: Adding functions, fixing bugs, writing tests
  don't require Opus-level reasoning
- **Claude SDK reserved for the 20%**: Complex refactoring, multi-file architectural
  changes, tasks requiring deep codebase understanding

The fallback chain becomes a **cost-optimization chain**:

```
Code task arrives
  │
  ├─ Routine (add function, fix bug, write test)
  │    → OpenCode with cost-efficient model (Sonnet/GPT-4o)
  │
  ├─ Complex (multi-file refactor, architecture change)
  │    → Claude Code subprocess (if working — uses subscription)
  │    → Claude SDK direct API (if CLI unavailable — notify user of cost)
  │    → Bedrock/Vertex (volume pricing fallback)
  │
  └─ Claude rate-limited
       → OpenCode with best available model
```

### Why OpenCode survives in this architecture

OpenCode is NOT just a fallback. It's a cost-effective code engine (see above) that
also activates when Claude is unavailable:

```python
class ClaudeCode(Tool):
    async def execute(self, task, **kwargs):
        # Try Claude SDK: direct API → Bedrock → Vertex
        for provider in ["direct", "bedrock", "vertex"]:
            try:
                return await self._run_claude_sdk(task, provider)
            except RateLimitError:
                continue

        # All Claude paths exhausted → fall back to OpenCode
        self.agent.log("Claude rate-limited, falling back to OpenCode")
        return await self._run_opencode(task, model="openai/gpt-4o")
```

This gives us:
- **No vendor lock-in** for code work
- **Graceful degradation** instead of "sorry, can't do code tasks right now"
- **Minimal complexity** — it's one fallback path in one tool, not a whole second engine

### Memory System: MCP Server Wrapping Genesis's System

Agent Zero's FAISS is replaced. Our memory system is superior and becomes a shared MCP server:

**MCP Server: `genesis-memory`**
Exposes these tools to Agent Zero (and Claude SDK via MCP):

| Tool | Maps To | Purpose |
|------|---------|---------|
| `memory_recall` | `MemoryManager.recall()` | Hybrid search (Qdrant + FTS5 + scoring) |
| `memory_store` | `MemoryManager.remember_exchange()` | Store conversation exchanges |
| `memory_extract` | `MemoryManager.remember_extractions()` | Store facts/decisions/entities |
| `memory_proactive` | `MemoryManager.proactive_recall()` | Cross-session context injection |
| `memory_core_facts` | `MemoryManager.get_high_confidence_items()` | High-confidence facts for prompts |
| `memory_stats` | `MemoryManager.stats()` | Health check (Qdrant up? counts?) |

**How memories get stored from conversations:**

```
User message arrives
  → Agent Zero message_loop_prompts_before extension:
      calls memory_proactive(current_message) via MCP
      calls memory_core_facts() via MCP
      injects recalled context into prompt
  → Agent Zero processes, responds
  → Agent Zero message_loop_end extension:
      calls memory_store(user_msg, assistant_msg) via MCP
      utility_model extracts facts/decisions (cheap, fast)
      calls memory_extract(extractions) via MCP
```

**How dream cycle / reviews interact:**

Dream cycle subordinate agents have MCP access. They call memory tools directly:
- Consolidation job: `memory_recall(query="recent patterns")` → analyze → `memory_extract(new_patterns)`
- Reflection job: `memory_recall(query="yesterday errors")` → structured observations
- Identity evolution: `memory_core_facts()` → compare with SOUL.md → propose changes

**The nuance is preserved** because:
1. Source metadata tags every memory (user_conversation, dream_cycle, heartbeat, weekly_review, etc.)
2. Confidence scoring, access counting, recency decay — all live in your existing MemoryManager
3. The MCP server is a thin wrapper — the intelligence is in the code you already wrote

**MCP Server: `genesis-observations`**
Wraps the dream_observations + evolution_log tables:

| Tool | Purpose |
|------|---------|
| `obs_write` | Write observation (source, type, category, content, priority) |
| `obs_query` | Query open observations by source/type/priority |
| `obs_resolve` | Mark observation resolved with resolution notes |
| `obs_propose_evolution` | Write evolution proposal for identity files |

**MCP Server: `genesis-recon`**
Wraps the recon_findings table + triage workflow.

---

## 4. What This Migration Solves

### From the Gap Analysis (2026-02-21)

#### ELIMINATED — No longer relevant (nanobot-specific problems that go away)

| Gap | Why It Goes Away |
|-----|-----------------|
| **57 upstream merge items** | We're leaving nanobot. The entire POST-V2-UPSTREAM-MERGE.md is irrelevant. |
| SqlitePool adoption (30+ violations) | Fresh codebase, no legacy raw `aiosqlite.connect()` calls |
| `process_direct()` race condition | Doesn't exist — Agent Zero agents have isolated contexts |
| `_strip_think()` missing | LiteLLM handles model-specific output formatting |
| MCP pipeline unwired (5 items) | Agent Zero has native MCP client+server |
| Memory consolidation routing bug | Agent Zero uses `utility_model` for internal tasks (separate from `chat_model`) |
| Temperature/max_tokens not passed | LiteLLM handles per-model config |
| Progress streaming | Agent Zero has WebSocket streaming built-in |
| `get_next_pending()` picks active tasks | Fresh task system design, no legacy query |
| `skip_enrichment` complexity | Agent Zero's extension system handles this cleanly |
| Custom tool definitions | Agent Zero's tool system + MCP |
| Router class rename bikeshedding | Gone with RouterProvider |
| Interleaved CoT injection waste | Agent Zero doesn't inject these |
| Non-destructive consolidation | Agent Zero's context management handles this |

**That's roughly 40+ items from the gap analysis + the entire 57-item upstream merge doc that become IRRELEVANT.**

#### SOLVED by new architecture (natural fit)

| Gap | How It's Solved |
|-----|----------------|
| Natural language model switching | LiteLLM + Agent Zero's model config. `/use opus`, `/use gemini`, `/use cheapest` |
| Model registry | LiteLLM's provider/model catalog + Agent Zero's `models.yaml` |
| Context bridging on model switch | Agent Zero maintains context across model changes natively |
| Worker tool restriction | Agent Zero per-agent `permissions` config (ask/allow/deny per tool) |
| Failover chain | LiteLLM native fallback chains with rate limit detection |
| Subagent orchestration | `CallSubordinate` — hierarchical, context-isolated |
| Parallel task execution | Multiple subordinate agents run concurrently |
| Browser automation | Agent Zero's `BrowserAgent` + Playwright + `browser_model` |
| Self-evolving extension lifecycle | Agent Zero's `SkillsTool` + SKILL.md standard |
| Wake event (no more polling) | Agent Zero's event-driven architecture |
| Free tier tracking | LiteLLM's usage tracking + rate limit detection |

#### NEEDS REBUILDING (on cleaner foundation)

| Gap | Approach in Agent Zero |
|-----|----------------------|
| Dream cycle 13 jobs | Scheduled task → spawns 13 subordinate agents with per-job model+prompt |
| Heartbeat cognitive ticks | Scheduler extension, enriched prompt, `utility_model` for cheap ticks |
| Weekly/monthly reviews | Scheduled tasks, MANAGER/DIRECTOR prompts, `claude_code` tool for code analysis |
| Identity evolution | Extension hook on `monologue_end` + scheduled evolution check |
| Recon cron jobs | Scheduler tasks with per-source model selection via LiteLLM |
| Observation lifecycle | `genesis-observations` MCP server |
| Situational awareness briefing | `message_loop_prompts_before` extension injects active tasks/spend/alerts |
| Task retrospectives | Post-task subordinate that writes to memory MCP |
| Cost tracking | Extension hook tracks per-interaction LiteLLM usage data |
| AlertBus equivalent | Extension + Agent Zero's logging + MCP server for alert state |
| Timezone normalization | Handle in MCP servers and extensions (fresh code, no 25+ legacy sites) |
| Two-phase intake interview | Prompt engineering in Agent Zero's system prompt |
| Task budget enforcement | Extension tracks cumulative cost, pauses subordinate at threshold |

#### PRESERVED (ported, not rebuilt)

| Component | How It's Ported |
|-----------|----------------|
| Memory system (Qdrant + FTS5 + SQLite) | MCP server wrapping existing MemoryManager code |
| SOUL.md / identity files | Agent Zero's `prompts/` directory (native pattern) |
| Dream observations schema | `genesis-observations` MCP server |
| Recon findings schema | `genesis-recon` MCP server |
| Confidence scoring, access tracking | Lives in MemoryManager, exposed via MCP |
| Multi-factor recall scoring | Lives in EpisodicStore, exposed via MCP |
| Embedder (local-first → cloud fallback) | Lives in Embedder class, used by MCP server |

---

## 5. Memory Coordination: Two Systems, One Truth

### The Problem

Claude Code / Agent SDK has its own memory layer:
- **CLAUDE.md** — project instructions, loaded automatically at session start (persists on disk)
- **Session history** — full conversation + tool calls, resumable by session ID (persists as .jsonl)
- **Context compression** — auto-summarizes older content (within session only)
- **Internal task tracking** — TaskCreate/TodoWrite (within session only)
- **Working knowledge** — files read, patterns discovered (within session only)

Genesis has its memory system (Qdrant + FTS5 + SQLite, via MCP). Without coordination, these two systems operate independently and learnings get lost.

### The Solution: CLAUDE.md Is the Handshake Protocol

CLAUDE.md is the one file both systems natively understand. Agent Zero writes it. Claude Code reads it automatically. One-directional flow, no conflicts.

```
Agent Zero                          Claude Agent SDK
───────────                         ────────────────

BEFORE invocation:
  memory_recall(task context)
  memory_core_facts()
  obs_query(relevant observations)
  Read SOUL.md principles
           │
           ▼
  WRITE dynamic CLAUDE.md ─────────→ Reads CLAUDE.md automatically
  (project conventions,              (knows past failures, patterns,
   past retrospectives,               conventions, current context)
   known pitfalls,
   current session context)          Does the work. Builds deep
           │                          internal understanding.
           ▼                          Reads files, writes code,
  Invoke claude_code tool ──────────→ runs tests.
  (with session_id for resume)
                                     Returns result + summary
  Capture session_id ←──────────────
           │
           ▼
AFTER invocation:
  Extract learnings from result
  Spawn retrospective subordinate
    (utility_model — cheap, fast)
  memory_store(retrospective)
  memory_extract(facts, decisions)
  obs_write(task outcome observation)
```

### Session Resumption for Multi-Step Tasks

Claude Agent SDK sessions are resumable. For complex tasks that span multiple steps:

```python
class ClaudeCode(Tool):
    async def execute(self, task, resume_session=None, **kwargs):
        # Update CLAUDE.md with latest Genesis memories
        await self._write_dynamic_claude_md(task)

        session_id = None
        async for message in query(
            prompt=task,
            options=ClaudeAgentOptions(
                resume=resume_session,  # Resume if continuing
                cwd=self.agent.config.project_dir,
                allowed_tools=["Read", "Edit", "Bash", "Glob", "Grep"],
            ),
        ):
            if hasattr(message, "subtype") and message.subtype == "init":
                session_id = message.session_id
            ...

        # Store session_id for potential continuation
        self.agent.data["last_claude_session"] = session_id

        # Run retrospective, store in Genesis memory
        await self._run_retrospective(task, result)
        return Response(message=result)
```

Step 1: "Build the rate limiter" → Claude SDK reads 30 files, writes code. Session saved.
Step 2: "Fix the 2 test failures" → resume=session_id. Full context preserved. No re-reading.
Step 3: "Add load testing" → resume=session_id. Cumulative understanding.

Between each step, Agent Zero can query new memories, update CLAUDE.md, run mini-retrospectives, check budget.

### The Two Memory Systems Are Complementary

| | Genesis Memory (MCP) | Claude Code Session Memory |
|---|---|---|
| **Scope** | Everything — all tasks, all services, all time | One task, one session |
| **Lifetime** | Permanent (Qdrant + SQLite) | Session duration (or until resumed) |
| **Breadth** | Cross-project patterns, retrospectives, core facts | Deep file-level understanding of THIS codebase |
| **Who writes** | Agent Zero (always) | Claude Code (within session) |
| **Who reads** | Everyone (via MCP) | Claude Code only (within session) |
| **Sync: Genesis → Claude** | Dynamic CLAUDE.md written before each invocation | Read automatically |
| **Sync: Claude → Genesis** | Result extraction + retrospective after each invocation | Stored in Qdrant + SQLite |

### Review Cycle Integration

> **SUPERSEDED:** The linear pipeline below (task → retrospective → nightly dream → weekly → monthly)
> is replaced by the Reflection Engine's adaptive depth model in `genesis-v3-autonomous-behavior-design.md`.
> The Reflection Engine dynamically selects Light/Deep/Strategic depth based on signal urgency,
> rather than running on fixed calendar schedules.

The learning loop from task outcomes through review cycles:

```
Task completes → retrospective stored in Genesis memory
                          ↓
Dream cycle (nightly) → queries recent retrospectives
                        analyzes patterns across tasks
                        stores consolidated learnings
                          ↓
Weekly review (Sunday) → queries week's retrospectives + observations
                         identifies recurring failures, capability gaps
                         updates SOUL.md if patterns warrant it
                         invokes claude_code for code-level analysis
                          ↓
Monthly review (1st) → queries month's weekly findings
                       strategic assessment of task effectiveness
                       adjusts budget policies, model assignments
```

Every review cycle reads from and writes to Genesis memory. Task outcomes → retrospectives → dream analysis → weekly patterns → monthly strategy. The dynamic CLAUDE.md means each subsequent task benefits from everything learned before it.

---

## 6. What We Delete (and the Code Cost)

### From nanobot (goes away entirely)

| Component | Est. Lines | Replaced By |
|-----------|-----------|-------------|
| `agent/loop.py` (copilot hooks) | ~1,500 | Agent Zero's monologue loop |
| `agent/context.py` (ExtendedContextBuilder) | ~800 | Agent Zero extensions |
| `cli/commands.py` (provider setup) | ~1,100 | Agent Zero + LiteLLM |
| `copilot/routing/` (RouterProvider, failover, heuristics) | ~2,500 | LiteLLM + Agent Zero model config |
| `copilot/tasks/` (manager, worker, navigator) | ~3,000 | Agent Zero subordinate agents |
| `copilot/context/` (events, extended) | ~500 | Agent Zero extensions |
| `config/schema.py` (CopilotConfig) | ~400 | Agent Zero settings.json |
| `config/loader.py` (secrets, deep merge) | ~300 | Agent Zero config + env vars |
| Various glue code | ~2,000 | Not needed |
| **Total deleted** | **~12,000** | |

### What we write (new code)

| Component | Est. Lines | Notes |
|-----------|-----------|-------|
| MCP server: genesis-memory | ~400 | Thin wrapper around existing MemoryManager |
| MCP server: genesis-observations | ~200 | CRUD for dream_observations table |
| MCP server: genesis-recon | ~200 | CRUD for recon_findings table |
| Agent Zero extensions (5-6) | ~600 | Memory injection, storage, heartbeat, identity, briefing, cost tracking |
| Custom tool: claude_code | ~80 | Claude SDK invocation with fallback |
| Custom tool: opencode_fallback | ~50 | OpenCode invocation |
| Scheduled task configs | ~200 | Cron definitions for all periodic services |
| Prompt files (system prompts) | ~500 | SOUL.md equivalent, heartbeat, dream, review prompts |
| Telegram relay | ~300 | Message routing to/from Agent Zero |
| **Total new code** | **~2,500** | |

**Net reduction: ~12,000 lines deleted, ~2,500 written.** Plus the memory system code (~2,000 lines) is ported (not rewritten) into the MCP server.

> **Updated estimate (2026-02-23):** The ~2,500 new lines estimate above predates the
> autonomous behavior design (`genesis-v3-autonomous-behavior-design.md`). Revised estimate:
> **5,000-8,000 new lines** with the full cognitive layer (Awareness Loop, Reflection Engine,
> Self-Learning Loop, health-mcp, outreach-mcp, engagement tracking, signal-weighted triggers).

---

## 7. The V2 Roadmap — What Changes

### V2 phases that become IRRELEVANT

| Phase | Original Scope | Status |
|-------|---------------|--------|
| V2.1 Phase 0 | Infrastructure fixes (SqlitePool, race conditions, tz.py) | **IRRELEVANT** — fresh codebase |
| V2.1 Phases 1-5 | Task lifecycle engine on nanobot | **REPLACED** — Agent Zero subordinate agents |
| V2.2 | OpenCode CLI integration as coding worker | **REPLACED** — claude_code + opencode tools |
| V2.2 | Parallel task execution | **SOLVED** — Agent Zero subordinates run concurrently |
| V2.2 | Full heartbeat reframe | **REBUILT** — as extension + scheduler |
| POST-V2 upstream merge | 57 items across 6 files | **IRRELEVANT** — leaving nanobot |
| POST-V2 project rename | Directory/path renames | **IRRELEVANT** — fresh container |

### V2 phases that REMAIN (rebuilt on Agent Zero)

| Phase | Original Scope | New Approach |
|-------|---------------|-------------|
| V2.3 | Browser automation | Agent Zero BrowserAgent (SOLVED natively) |
| V2.3 | External to-do integration | MCP server for Todoist/Notion/Linear |
| V2.4 | Email (IMAP/SMTP) | MCP server or extension |
| V2.4 | Content pipelines | Agent Zero subordinate agents |
| V2.5 | Deeper lessons, shadow mode | Extension hooks + memory MCP |
| V2.5 | Self-evolving extension lifecycle | Agent Zero SkillsTool (SOLVED natively) |
| V2.6+ | Dashboard | Agent Zero's built-in Web UI (partially solved) |
| V2.6+ | Telegram multi-channel | Interface relay design |

### The V2 vision — preserved, accelerated

The V2 vision was: "An autonomous digital executive assistant — takes your to-do list and gets it done."

That vision is UNCHANGED. What changes is the foundation:
- Instead of building task lifecycle on nanobot's `process_direct()` and custom SubagentManager → Agent Zero's subordinate agent hierarchy
- Instead of custom routing for model selection → LiteLLM with 4 model roles
- Instead of bolting browser automation onto nanobot → Agent Zero's native BrowserAgent
- Instead of fighting upstream merge debt → clean foundation, no debt

The cognitive layer (dream cycle, heartbeat, identity evolution, recon) was always YOUR unique contribution. It ports cleanly because it's conceptual, not code-bound to nanobot.

---

## 8. Migration Plan

### Phase 0: Validation (BEFORE committing)

**Goal:** Prove the architecture works. Kill the idea early if it doesn't.

**Setup:**
```bash
# Create new Incus container
incus launch ubuntu:24.04 genesis-v3
incus exec genesis-v3 -- bash

# Install Agent Zero (bare metal, no Docker)
git clone https://github.com/agent0ai/agent-zero.git /opt/agent-zero
cd /opt/agent-zero
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Install Claude Agent SDK
pip install claude-agent-sdk

# Install OpenCode (for fallback testing)
# (follow opencode.ai install instructions)

# Configure API keys
# Set up models.yaml with your providers
```

**Validation tests:**
1. **Agent Zero basic operation**: Run the web UI, send a task, verify monologue loop works
2. **LiteLLM model switching**: Configure Claude, Gemini, GPT models. Switch between them mid-conversation
3. **Subordinate agent quality**: Have Agent Zero spawn a subordinate for a multi-step task. Compare quality to nanobot's task system
4. **Claude SDK as tool**: Build the `claude_code` custom tool. Give Agent Zero a code task. Verify it delegates to Claude SDK and gets good results
5. **OpenCode fallback**: Simulate Claude rate limit. Verify fallback to OpenCode works
6. **MCP server**: Build a minimal genesis-memory MCP server exposing one endpoint (recall). Connect Agent Zero to it. Verify memory retrieval works
7. **Scheduler**: Configure a cron job in Agent Zero. Verify it fires and can invoke a subordinate
8. **Extension hooks**: Build a minimal `message_loop_end` extension. Verify it fires after each exchange

**Decision gate:** If tests 1-4 pass with acceptable quality, proceed. If Agent Zero's subordinate agent quality is significantly worse than expected, investigate why and determine if it's fixable.

### Phase 1: Foundation (~1-2 weeks)

1. **MCP servers** — Port MemoryManager, observations, recon into MCP servers
2. **Core extensions** — Memory injection/storage, identity files, situational briefing
3. **Custom tools** — `claude_code` with fallback chain, `opencode_fallback`
4. **Prompt files** — Port SOUL.md, heartbeat prompt, dream prompts, review prompts
5. **Basic scheduling** — Heartbeat (2h), health check (30min)

### Phase 2: Cognitive Layer (~1-2 weeks)

6. **Dream cycle** — 13 jobs as scheduled subordinate agents with per-job model config
7. **Weekly review** — MANAGER role, architecture analysis via `claude_code` tool
8. **Monthly review** — DIRECTOR role, budget policy, strategic reflection
9. **Recon cron jobs** — 5 jobs with per-source model selection
10. **Identity evolution** — Extension + scheduled evolution check

### Phase 3: Interface + Integration (~1 week)

11. **Telegram relay** — Message routing to/from Agent Zero
12. **Cost tracking** — Extension hook + reporting
13. **Alert system** — Extension-based equivalent of AlertBus
14. **Model switching UX** — `/use` command for model changes

### Phase 4: Cutover

15. **Parallel run** — Both containers operating, compare behavior
16. **Data migration** — Qdrant data (shared or copied), SQLite tables
17. **Cutover** — Point Telegram to v3 container
18. **Decommission** — Shut down v1 container (keep as backup)

---

## 9. Open Questions

1. **Qdrant sharing vs fresh instance** — Share v1's Qdrant (all existing memories available instantly) or start fresh? Sharing is simpler but creates a dependency between containers during parallel run.

2. **Agent Zero web UI vs custom dashboard** — Agent Zero's React UI may be sufficient for monitoring. Do we need a custom dashboard, or can we extend theirs?

3. **A0T token stripping** — Fork the repo, remove token-related code from UI/governance. The core agent framework is MIT-licensed and token-free. How much UI code is tangled with the token?

4. **Extension vs MCP for memory** — We chose MCP for memory. Alternative: build memory directly as a Python extension (no MCP overhead, simpler). Tradeoff: MCP is sharable across engines; extension is faster but Agent Zero-only.

5. **Cost tracking granularity** — LiteLLM tracks usage per call. How do we aggregate this into per-task, per-service, per-day reporting like the current system?

6. **Existing task data** — Do we migrate in-progress tasks from v1? Or clean start?

7. **Claude SDK authentication in container** — Agent SDK needs `ANTHROPIC_API_KEY`. Also needs Bedrock/Vertex credentials if using those as fallback paths. How to manage secrets in the new container?

---

## 10. Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| Agent Zero 24/7 stability | HIGH | Phase 0 soak test. Process supervision (systemd). v1 stays running as fallback |
| Agent Zero subordinate quality < nanobot tasks | MEDIUM | Phase 0 validation test #3. Prompt tuning if needed. Fallback: rethink approach |
| MCP server latency for memory ops | LOW | Local MCP (stdio), not network. Benchmark in Phase 0 |
| A0T token governance shifts project | MEDIUM | Fork early. Monitor upstream. MIT license protects the code |
| Migration takes longer than expected | MEDIUM | v1 stays running. No deadline pressure. Incremental migration |
| OpenCode adds complexity for rare fallback | LOW | It's one tool (~50 lines). If never needed, easy to remove |
| Loss of nanobot-specific features | LOW | Thorough gap analysis done (this doc). Nothing critical is lost |

---

## 11. What This Plan Does NOT Cover

- Specific Agent Zero prompt engineering (that's implementation)
- Telegram bot UX design (interface layer decision)
- New features beyond v2 scope (build on stable foundation first)
- Cost projections for the new architecture (need Phase 0 data)
- A0T token stripping details (need to audit the codebase)

---

## 12. Summary

| Metric | Current (nanobot) | v3 (Agent Zero + Claude SDK + OpenCode) |
|--------|------------------|----------------------------------------|
| Custom Python code | ~35,000 lines | ~2,500 new + ~2,000 ported MCP |
| Upstream merge debt | 57 items across 6 files | Zero |
| Infrastructure bugs | 5 active (SqlitePool, tz, consolidation, etc.) | Zero (fresh start) |
| Model providers | Custom routing + failover | LiteLLM: 100+ providers, native fallbacks |
| Code editing | None (planned for V2.2) | Claude SDK (best) + OpenCode (backup) |
| Browser automation | None (planned for V2.3) | Agent Zero native (Playwright) |
| Multi-agent | Custom SubagentManager + contention issues | Native hierarchical subordinates |
| Self-learning | Dream cycle consolidation | Agent Zero auto-indexing + dream cycle |
| Task system | ~30% complete, many gaps | Subordinate agents (foundation) + rebuild safety layer |
| Memory system | Sophisticated (Qdrant+FTS5+SQLite) | SAME system, exposed via MCP |
| Docker/container | None | Incus container (bare metal Agent Zero) |
| Vendor lock-in | Partially (Claude-heavy) | LiteLLM + OpenCode fallback = fully flexible |

**First step:** Spin up the Incus container and run Phase 0 validation tests.

---

## 13. Memory System — Deferred Improvements (from v2 audit, 2026-02-26)

Identified during a memory audit when investigating whether the copilot actually stored a user-requested memory correctly. Three issues were fixed in v2 (tier separation, importance inversion, topic tagging). Three are deferred to v3:

### Confidence Decay
- `memory_items.access_count` exists but doesn't feed back into confidence
- Items that are never recalled should have confidence decay over time
- Items that are frequently recalled should see confidence increase
- Implement as a periodic job in dream consolidation or a dedicated cron job
- Design: exponential decay with floor (e.g., `confidence *= 0.95` per week, min 0.1)

### Post-Hoc Tag Quality Improvement
- SLM extraction now emits tags (implemented in v2, 2026-02-26)
- After ~2 weeks of data, audit tag quality across memory_items
- If tags are too generic or inaccurate, add a separate lightweight tagging pass
- Can be batched in dream consolidation to avoid real-time latency
- Decision point: evaluate at v3 kickoff whether SLM tags are sufficient

### Deterministic Deduplication
- Current dedup relies on LLM-driven dream consolidation (unreliable)
- Add a deterministic merge pass: same category + high text similarity → merge
- Could use Jaccard similarity on tag sets as a fast pre-filter
- Run as a dream cycle job or standalone migration
