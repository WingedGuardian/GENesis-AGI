# Genesis ↔ Agent Zero Integration Architecture

**Status:** Active | **Last updated:** 2026-03-07


> **Addendum (2026-03-07):** AZ's role has shifted from "brain + body" to "body only."
> Claude Code is now the primary intelligence layer (user conversation, deep/strategic
> reflection, task orchestration). AZ retains infrastructure responsibilities: web dashboard,
> messaging relay, health monitoring, signal collection (awareness loop), and micro/light
> reflection via API. The `chat_model`/`utility_model` slots below apply only to AZ-resident
> work. See `docs/plans/2026-03-07-agentic-runtime-design.md` for the full architecture.

**Status:** Active design document
**Last updated:** 2026-03-04
**Depends on:** `genesis-v3-autonomous-behavior-design.md`, `genesis-v3-build-phases.md`

## Integration Philosophy: Option C

Genesis integrates with Agent Zero by building to upstream's trajectory, not our
current fork pin. Upstream's `development` branch ships a plugin system that
solves most integration problems cleanly. Our fork is pinned at `fa65fa3`
(testing branch, v0.9.8.2) which predates the plugin system.

**Option C** means: structure Genesis code AS IF the plugin system exists today.
Apply a minimal bridge patch (~20 lines in `get_paths()`) to enable plugin
discovery on our pin. When upstream ships the plugin system to `testing`/`main`,
delete our patch and everything just works.

### Rules

1. **Build to upstream's trajectory** — match upstream's plugin directory layout,
   naming conventions, and interface contracts. Don't invent Genesis-specific
   patterns that will need migration later.
2. **Plugin directory structure** — all Genesis code that runs inside Agent Zero
   lives in `~/agent-zero/usr/plugins/genesis/` (or `genesis-memory/`). Never
   scatter Genesis files through Agent Zero's default directories.
3. **Don't fight upstream** — if upstream has a pattern for something (even only
   on `development`), match it. If upstream solves a problem differently than we
   would, prefer their approach unless it conflicts with Genesis requirements.
4. **Minimal core patches** — only patch Agent Zero core when there is no
   alternative. Every core patch is a rebase liability. Document all patches in
   `docs/reference/agent-zero-fork-tracking.md`.
5. **Extensions are the primary behavior surface; MCP servers are the data
   backend.** Extensions drive behavior — they inject context into prompts,
   observe lifecycle events, and trigger Genesis logic at the right moments.
   MCP servers are called BY extensions (and by the agent's tool interface)
   to read/write data. Extensions decide WHEN and WHY; MCP servers provide
   WHAT. This is the opposite of what you might assume — MCP servers are not
   the "heavyweight" surface. They're the storage/compute layer that extensions
   orchestrate.
6. **Apply across V3/V4/V5** — this philosophy is not V3-specific. Every version
   should maintain alignment with upstream's direction.

---

## Background Threading: DeferredTask

Agent Zero runs background work in `DeferredTask` threads — each gets its own
`asyncio` event loop, completely isolated from the main uvicorn loop. Genesis
background tasks (Awareness Loop scheduler, reflection, observation extraction)
MUST use this pattern.

```python
# How Genesis starts the Awareness Loop inside AZ:
from python.helpers.defer import DeferredTask

def _start_awareness_loop(agent):
    async def _run():
        loop = AwarenessLoop(db, collectors)
        await loop.start()  # APScheduler runs in this thread's event loop

    DeferredTask(agent, _run)
```

**Why this matters:**
- The main uvicorn loop has `nest_asyncio.apply()` — reentrancy is fragile
- DeferredTask threads have clean event loops with no reentrancy
- APScheduler's `AsyncIOScheduler` works correctly in a dedicated thread
- `asyncio.Lock` (used in `AwarenessLoop._tick_lock`) works correctly here
- Genesis MUST NOT call `nest_asyncio.apply()` — keep all async work in
  DeferredTask threads

See `docs/reference/agent-zero-architecture-deep-dive.md` for full event loop
topology.

---

## Plugin Layout

```
~/agent-zero/usr/plugins/
├── genesis/
│   ├── plugin.yaml
│   ├── default_config.yaml
│   ├── extensions/
│   │   └── python/
│   │       ├── agent_init/
│   │       │   └── _10_initialize_genesis.py
│   │       ├── message_loop_start/
│   │       │   └── _11_critical_escalation_check.py
│   │       ├── message_loop_prompts_after/
│   │       │   └── _51_awareness_loop_briefing.py
│   │       ├── system_prompt/
│   │       │   └── _21_reflection_engine_prompt.py
│   │       ├── message_loop_end/
│   │       │   └── _53_outcome_tracking.py
│   │       └── monologue_end/
│   │           ├── _52_extract_observations.py
│   │           └── _53_self_learning_signals.py
│   ├── tools/
│   │   └── (Genesis-specific tools if needed)
│   ├── prompts/
│   │   └── agent.system.genesis.md
│   └── agents/
│       └── genesis-monitor/
│           ├── agent.yaml
│           └── prompts/
│               └── agent.system.main.role.md
│
└── genesis-memory/
    ├── plugin.yaml
    ├── default_config.yaml
    ├── extensions/
    │   └── python/
    │       ├── monologue_start/
    │       │   └── _10_memory_init.py
    │       ├── message_loop_prompts_after/
    │       │   ├── _50_recall_memories.py
    │       │   └── _91_recall_wait.py
    │       ├── monologue_end/
    │       │   ├── _50_memorize_fragments.py
    │       │   └── _51_memorize_solutions.py
    │       └── system_prompt/
    │           └── _20_behaviour_prompt.py
    ├── tools/
    │   ├── memory_save.py
    │   ├── memory_load.py
    │   ├── memory_delete.py
    │   ├── memory_forget.py
    │   └── behaviour_adjustment.py
    ├── helpers/
    │   └── memory.py
    ├── prompts/
    │   └── agent.system.tool.memory.md
    └── api/
        └── memory_dashboard.py
```

---

## Compute Routing

### How AZ Makes LLM Calls: unified_call()

All LLM calls in Agent Zero flow through `LiteLLMChatWrapper.unified_call()`
in `models.py`:

```python
# LiteLLMChatWrapper.unified_call() — the single entry point for all chat LLM calls
async def unified_call(self, system_message="", user_message="", messages=None,
                       response_callback=None, reasoning_callback=None,
                       tokens_callback=None, rate_limiter_callback=None,
                       explicit_caching=False, **kwargs) -> Tuple[str, str]:
```

This wraps LiteLLM's `acompletion()` with rate limiting (sliding window per
model), streaming assembly, error handling, and retry (up to 2 retries on
transient errors: 408, 429, 500+). AZ also has `agent.call_chat_model()` and
`agent.call_utility_model()` convenience methods that delegate to
`unified_call()`.

**Key architectural detail (confirmed 2026-03-04):** AZ creates a **fresh model
instance per call** — `get_chat_model()` instantiates a new `LiteLLMChatWrapper`
every time. This means per-call routing decisions are trivial to implement.

**Genesis does NOT replace `unified_call()`** — it wraps the call sites that
invoke it, adding pre-call routing logic (fallback chains from the model
routing registry) and post-call tracking (cost events to SQLite). AZ already
has rate limiting; Genesis adds circuit breakers and cost accounting.

### Extension Hooks for Routing (Confirmed 2026-03-04)

AZ's extension framework provides two hooks relevant to routing:

| Hook | Location | Purpose |
|------|----------|---------|
| `before_main_llm_call` | `agent.py` line 423, before `call_chat_model()` | Intercept chat model calls; can modify `loop_data` |
| `util_model_call_before` | `agent.py` line 790, before utility model | Intercept utility model calls; receives `call_data` (model, system, message) |

**Routing integration pattern:** Genesis extension in
`usr/plugins/genesis/extensions/before_main_llm_call/_05_genesis_routing.py`
temporarily swaps `agent.config.chat_model` with the routed `ModelConfig`,
stores the original in `loop_data.params_temporary`, and a corresponding
`message_loop_end` extension restores it. If routing fails, the original
config stays in place — zero-risk fallback.

### Model Slots

Agent Zero has two independent model slots:

| Slot | Setting | Genesis Use |
|------|---------|------------|
| `chat_model` | Main reasoning loop | User conversations, deep reflection (Sonnet/Opus) |
| `utility_model` | Lightweight tasks | Awareness ticks, micro/light reflection, memory ops (SLM on Ollama) |

### How It Works

The awareness loop extension calls `self.agent.call_utility_model()` for
micro/light reflection — this routes to the SLM on Ollama (`${OLLAMA_URL:-localhost:11434}`),
which is always available and free. For deep reflection, the extension injects
context into the main system prompt and lets `chat_model` (Sonnet) handle it.

No model switching. No subordinate agents for routing. No core patches. The two
model slots are the routing mechanism.

### Model Configuration

```
chat_model:
  provider: anthropic
  name: claude-sonnet-4-20250514
  ctx_length: 200000

utility_model:
  provider: ollama
  name: qwen2.5:3b          # SLM on Ollama container
  api_base: http://${OLLAMA_URL:-localhost:11434}
  ctx_length: 32000

embeddings_model:
  provider: ollama
  name: qwen3-embedding:0.6b
  api_base: http://${OLLAMA_URL:-localhost:11434}
```

### Reflection Depth Routing

| Depth | Model Slot | Provider | Trigger |
|-------|-----------|----------|---------|
| Micro | `utility_model` | Ollama SLM | Every 5-min tick |
| Light | `utility_model` | Ollama SLM | Elevated urgency score |
| Deep | `chat_model` | Sonnet | High urgency or weekly schedule |
| Strategic | `chat_model` | Sonnet/Opus | Monthly or critical escalation |

> **Note:** This table is a simplified view of Agent Zero's two model slots.
> For the complete call-site-to-model mapping (28 call sites with primary
> models, fallback chains, free compute sources, and paid alternatives), see
> the [Model Routing Registry](genesis-v3-model-routing-registry.md).

> **Updated (2026-03-07):** Deep and Strategic reflection now run in CC background sessions
> (Sonnet high thinking and Opus high thinking respectively), not through AZ's model slots.
> Only Micro and Light remain AZ-resident. See agentic-runtime-design.md §3.

---

## Memory Replacement

Genesis replaces Agent Zero's FAISS-based memory with Qdrant + SQLite FTS5.
This is implemented as a separate plugin (`genesis-memory`) that provides the
same 5-tool interface Agent Zero's memory plugin exposes.

### Tool Interface Contract

| Tool | Parameters | Returns |
|------|-----------|---------|
| `memory_save` | `text`, `area` | Memory ID |
| `memory_load` | `query`, `threshold`, `limit`, `filter` | List of matching memories |
| `memory_delete` | `ids` (comma-separated) | Deletion confirmation |
| `memory_forget` | `query`, `threshold`, `filter` | Deletion confirmation |
| `behaviour_adjustment` | `adjustments` | Merge confirmation |

### Backend Mapping

| AZ Memory Concept | Genesis Implementation |
|---|---|
| FAISS vector index | Qdrant `episodic_memory` collection (1024-dim, cosine) |
| Memory areas (MAIN, FRAGMENTS, SOLUTIONS) | Qdrant payload field `area` with filtered search |
| LangChain CacheBackedEmbeddings | Direct Ollama embedding calls (`qwen3-embedding:0.6b`) |
| `usr/memory/<subdir>/` file storage | SQLite FTS5 `memory_fts` table + Qdrant vectors |
| Per-project memory isolation | SQLite `collection` column in FTS5 |

### Why Replace (Not Wrap)

- FAISS is local, per-session, file-based. Genesis needs cross-session, remote, queryable.
- Qdrant is already running on `localhost:6333` and Genesis Phase 0 built the wrapper.
- The memory plugin interface is clean — same tool names, same parameters.
- AZ's recall/memorize extensions continue working unchanged — they call tool
  names, not FAISS directly.

### Memory Dashboard: Keep the UI, Swap the Backend

AZ provides a full memory management dashboard (Alpine.js + Flask). The
dashboard API handler (`memory_dashboard.py`) calls `Memory` class methods —
not FAISS directly. This means the frontend and API handler stay untouched.

**Strategy:** Rewrite the `Memory` class internals (~400 lines, 5-6 public
methods) to backend to Qdrant + SQLite instead of FAISS. The class keeps
the same public API: `search_similarity_threshold()`, `get_all_docs()`,
`delete_documents_by_ids()`, `update_documents()`. Users manage Genesis
memories through AZ's existing dashboard with zero frontend changes.

This is a documented AZ core patch (Phase 5), tracked in
`docs/reference/agent-zero-fork-tracking.md`.

See `docs/reference/agent-zero-architecture-deep-dive.md` → Memory System
for full dashboard API contract and data format details.

### Two-Layer Memory Architecture

Genesis has two memory access paths that share the same Qdrant backend:

**Layer 1: AZ Plugin Tools** (backward-compatible with AZ's recall/memorize extensions)
```
Agent conversation loop
  → AZ extensions (_50_recall_memories, _50_memorize_fragments, etc.)
  → memory_save / memory_load / memory_delete / memory_forget
  → genesis-memory plugin
  → Qdrant + SQLite FTS5
```

**Layer 2: Genesis MCP Tools** (richer API for Genesis-specific operations)
```
Genesis extensions (_51_awareness_loop, _52_extract_observations, etc.)
  → memory-mcp server
  → memory_recall / memory_store / memory_extract / observation_write / etc.
  → same Qdrant + SQLite FTS5 backend
```

Layer 1 uses AZ's 5 standard tool names so existing extensions work unmodified.
Layer 2 uses Genesis's richer 13-tool API (Phase 0 stubs) for observations,
knowledge base, proactive recall, and memory stats.

Both layers write to the same Qdrant collections and SQLite tables. The
genesis-memory plugin and memory-mcp server share the same `genesis.db` and
`genesis.qdrant` backend code.

**Phase 0 MCP stubs do not need to change.** The 5-tool plugin wrapper is a
small addition in Phase 5 when memory goes live.

---

## Extension Hook Mapping

Genesis uses 7 of Agent Zero's 23 extension points, organized into three
subsystems. All Genesis extension files use the `genesis_` prefix to avoid
collisions with future AZ built-in extensions.

### What Extensions CANNOT Do

Before the mapping, critical constraints:

- **Can't force tick timing** — extensions fire when AZ's `monologue()` runs,
  not on their own schedule. The APScheduler tick runs in a DeferredTask thread;
  the extension that injects awareness briefings only fires when the agent is
  already in a monologue loop.
- **Can't bypass the monologue loop** — extensions can't spawn new iterations.
  They execute within the current iteration and set flags for the next one.
- **Can't force out-of-cycle ticks** — `force_tick()` works for the standalone
  APScheduler tick (writes to DB, creates observations), but the extension that
  injects the briefing into the agent's prompt only fires when `monologue()`
  runs. There can be a delay between the tick result and the agent seeing it.
- **Genesis proposes; AZ's runtime decides** — Genesis sets flags and injects
  context. AZ's `monologue()` controls when agents actually run and for how long.

### Awareness Loop (Phase 1)

| Hook | File | Fires | Purpose |
|------|------|-------|---------|
| `message_loop_start` | `_11_genesis_critical_escalation_check.py` | Each iteration | Check health-mcp for critical alerts; bypass tick if urgent |
| `message_loop_prompts_after` | `_51_genesis_awareness_briefing.py` | Each iteration | Inject latest tick results into agent context; signals already collected by APScheduler |
| `system_prompt` | `_21_genesis_reflection_prompt.py` | Prompt build | Append reflection prompt when depth > 0; include governance constraints |

**`_51_awareness_loop_briefing.py` is the single most critical integration
point.** It's where Genesis's perception meets Agent Zero's reasoning loop.
Runs after `_50_recall_memories` (AZ's memory recall), so memory context is
available.

### Self-Learning Loop (Phase 4-6)

| Hook | File | Fires | Purpose |
|------|------|-------|---------|
| `monologue_end` | `_52_genesis_extract_observations.py` | Conversation end | Background `DeferredTask`: LLM extracts observations → memory-mcp |
| `monologue_end` | `_53_genesis_self_learning_signals.py` | Conversation end | Background: prediction error, drive signals, retrospective |
| `message_loop_end` | `_53_genesis_outcome_tracking.py` | Each iteration | Track outcomes, update procedure metrics, populate surplus queue |

### Infrastructure (Phase 0+)

| Hook | File | Fires | Purpose |
|------|------|-------|---------|
| `agent_init` | `_10_genesis_initialize.py` | Agent startup | Boot MCP connections, load identity, autonomy bounds, signal weights from SQLite |

### Hooks Genesis Does NOT Use

- `reasoning_stream*` — AZ's reasoning UI (leave as-is)
- `response_stream*` — Secret masking (AZ handles this)
- `tool_execute_before/after` — Secret masking + injection scan (AZ handles this)
- `error_format`, `banners`, `user_message_ui` — AZ UI concerns
- `hist_add_before/after` — AZ's history management
- `message_loop_prompts_before` — Reserved for AZ's history compression wait

### Execution Order

Extensions execute alphabetically by filename within each hook point. Genesis
uses numeric prefixes to control ordering relative to AZ's built-in extensions:

```
_10_ — Infrastructure (AZ defaults + _10_genesis_initialize.py)
_20_ — Prompts (AZ behaviour prompt)
_21_ — Genesis reflection prompt (_21_genesis_reflection_prompt.py)
_50_ — Memory (AZ recall, AZ memorize)
_51_ — Genesis awareness briefing (_51_genesis_awareness_briefing.py)
_52_ — Genesis observations (_52_genesis_extract_observations.py)
_53_ — Genesis learning/outcome (_53_genesis_*.py)
_91_ — Recall wait (AZ)
```

---

## MCP Server Integration

Genesis's 4 MCP servers are configured in Agent Zero's `mcp_servers` setting
and run as separate processes. AZ connects as an MCP client.

| Server | Transport | Tools | Phase |
|--------|----------|-------|-------|
| `memory-mcp` | stdio | 13 tools (recall, store, extract, observations, knowledge) | 0 (stubs) → 5 (live) |
| `recon-mcp` | stdio | 4 tools (findings, triage, schedule, sources) | 0 (stubs) → 1 (live) |
| `health-mcp` | stdio | 3 tools (status, errors, alerts) | 0 (stubs) → 1 (live) |
| `outreach-mcp` | stdio | 5 tools (send, queue, engagement, preferences, digest) | 0 (stubs) → 8 (live) |

### MCP Session Behavior

Agent Zero creates ephemeral MCP sessions — each `call_tool` opens a new
connection. Genesis MCP servers must manage their own state externally (SQLite,
Qdrant) and not rely on session persistence. This is already how Phase 0 is
built.

### MCP Configuration Format

```json
{
  "mcpServers": {
    "memory-mcp": {
      "command": "python",
      "args": ["-m", "genesis.mcp.memory_mcp"],
      "env": {"GENESIS_DB": "${HOME}/genesis/genesis.db"}
    },
    "health-mcp": {
      "command": "python",
      "args": ["-m", "genesis.mcp.health_mcp"]
    },
    "recon-mcp": {
      "command": "python",
      "args": ["-m", "genesis.mcp.recon_mcp"]
    },
    "outreach-mcp": {
      "command": "python",
      "args": ["-m", "genesis.mcp.outreach_mcp"]
    }
  }
}
```

---

## Bridge Patch: Plugin Discovery

Our fork pin (`fa65fa3`) doesn't have the plugin system. A minimal patch to
`python/helpers/subagents.py` adds plugin directory scanning to `get_paths()`.

**Scope:** ~20 lines in `get_paths()`. Scans `usr/plugins/*/` for each subpath,
appending results after the user-level paths and before the default paths.

**Lifecycle:** This patch is temporary. When upstream ships the plugin system to
`testing`/`main` and we forward-merge, this patch is deleted. The directory
structure is identical, so Genesis code doesn't change.

**Implementation:** Committed as `b96801a` in the Agent Zero fork. Two files:
`python/helpers/plugins.py` (minimal discovery module) and a patch to
`get_paths()` in `python/helpers/subagents.py`. Plugin stubs created at
`usr/plugins/genesis/` and `usr/plugins/genesis-memory/` (local, not in git —
`usr/` is gitignored). Includes stub extension `_10_initialize_genesis.py`.

---

## User-Scheduled Crons (Category 3)

User-scheduled cron jobs ("check my email every morning", "run this report
Friday") should use AZ's built-in `TaskScheduler` — NOT Genesis's APScheduler.

AZ's `TaskScheduler` (`python/helpers/task_scheduler.py`) already provides:
- Cron expression support
- UI integration (task list, manual trigger, enable/disable)
- Persistence across restarts
- Context management (which agent runs which task)

Genesis doesn't need to rebuild this. The Awareness Loop's APScheduler
handles categories 1 (event-driven reflection) and 2 (Genesis rhythms).
Category 3 is AZ's domain.

---

## Data Flow: 5-Minute Awareness Tick

```
APScheduler tick (every 5 min, in DeferredTask thread)
  │
  └─ perform_tick(): collect signals → score → classify → store to SQLite
     (runs independently of the agent's monologue loop)

Agent monologue loop (when AZ decides to run):
  │
  ├─ message_loop_start
  │   └─ _11_genesis_critical_escalation_check
  │      └─ health-mcp.health_alerts(active_only=true)
  │      └─ If critical → set deep reflection flag
  │
  ├─ message_loop_prompts_after
  │   └─ _51_genesis_awareness_briefing
  │      └─ Read latest tick result from awareness_ticks table
  │      └─ Inject briefing into loop_data.system (depth, signals, reason)
  │      └─ NOTE: tick already happened; this just injects the result
  │
  ├─ system_prompt
  │   └─ _21_genesis_reflection_prompt
  │      └─ If depth > 0: append reflection prompt + governance constraints
  │
  ├─ LLM call
  │   └─ Micro/Light: utility_model (SLM on Ollama)
  │   └─ Deep: chat_model (Sonnet)
  │
  ├─ message_loop_end
  │   └─ _53_genesis_outcome_tracking
  │      └─ Record iteration metrics, outcome classification
  │
  └─ monologue_end
      ├─ _52_genesis_extract_observations (background DeferredTask)
      │   └─ utility_model extracts observations → memory-mcp.observation_write()
      └─ _53_genesis_self_learning_signals (background DeferredTask)
          └─ Calculate prediction error, log drive signals
```

---

## State Management

| State Type | Storage | Lifecycle |
|---|---|---|
| Signal weights, autonomy levels | SQLite (Genesis DB) | Persistent across restarts |
| Drive weights | SQLite (Genesis DB) | Persistent, fixed in V3 |
| Observations, execution traces | SQLite + Qdrant | Persistent |
| Current urgency score, reflection depth | `agent.data` dict | In-memory, per-session |
| Loop-level flags (escalation, skip) | `LoopData` | Per-monologue, ephemeral |
| MCP server state | Each server's own SQLite | Persistent |

`agent.data` is in-memory only. Genesis loads durable state from SQLite at
`agent_init` and saves back at `monologue_end`. The in-memory dict is a cache.

---

## Upstream Compatibility Notes

### Patches That Will Conflict on Rebase

| File | Issue | Severity |
|------|-------|----------|
| `run_ui.py` | Upstream massively restructured (moved auth, added plugin routes, `@extensible`) | HIGH |
| `agent.py` | Upstream added `@extensible` decorators to ~20 methods | HIGH |
| `secrets.py` | Upstream renamed `get_project_meta_folder()` → `get_project_meta()` | HIGH (runtime crash) |

### Cherry-Picked Community PRs

None of our cherry-picked PRs (#1149, #1150, #1090, #1114) were merged into
upstream `development`. Every fix exists only in our fork. They were open PRs
that upstream never accepted. This means all fixes must be maintained and
re-applied on any future rebase.

### Patches Still Needed After Rebase

- `litellm` in requirements.txt (upstream still missing it)
- `structuredContent` handling in MCP (upstream doesn't have it)
- MCP startup secret resolution (upstream only does runtime resolution)
- Negative `max_tokens` guard (not in upstream)
- `drop_params=True` (not in upstream)
- `finish_reason` tracking (not in upstream)
- Browser/state debounce (not in upstream)
- History compression error handling (not in upstream)
- Chat load dirty marking (not in upstream)
- All security hardening (not in upstream)

### Patches to Drop After Rebase

- Whisper import guard (upstream pinned the package, guard is redundant)

### Missing Upstream Fix to Cherry-Pick

- Empty content message filtering in `models.py` (upstream lines 364-368).
  Prevents sending empty-content messages to LLM APIs.

### Rebase Strategy

When upstream ships plugins to `testing`/`main`:

1. Cherry-pick the empty-content fix from upstream NOW (before rebase)
2. Re-apply security hardening as a Genesis plugin (not core patches)
3. Re-apply remaining fixes to the new file structure
4. Drop whisper guard and bridge patch
5. Run full 30-test verification suite + Genesis 170 tests
6. Update fork tracking doc

---

## Phase 0 Compatibility

Genesis Phase 0 (SQLite schemas, CRUD, Qdrant wrapper, MCP stubs) lives in
`~/genesis/src/genesis/` — a separate repository. Zero file conflicts with
Agent Zero. The genesis-memory plugin will import from `genesis.db` and
`genesis.qdrant` packages, bridging the two codebases at the plugin level.

---

## Key Files Reference

| File | Purpose |
|------|---------|
| `~/agent-zero/python/helpers/extension.py` | Extension loading + cache |
| `~/agent-zero/python/helpers/subagents.py` | Path resolution (`get_paths()`) |
| `~/agent-zero/python/helpers/tool.py` | Tool base class |
| `~/agent-zero/python/helpers/mcp_handler.py` | MCP client |
| `~/agent-zero/agent.py` | Core agent loop, `monologue()`, `call_chat_model()` |
| `~/agent-zero/models.py` | Model config, `LiteLLMChatWrapper`, `call_utility_model()` |
| `~/agent-zero/initialize.py` | Agent factory, settings → config mapping |
| `~/genesis/src/genesis/db/` | SQLite schemas + CRUD |
| `~/genesis/src/genesis/qdrant/` | Qdrant wrapper |
| `~/genesis/src/genesis/mcp/` | 4 MCP server stubs |
| `docs/reference/agent-zero-fork-tracking.md` | Patch inventory + rebase notes |

---

## Related Documents

- [genesis-v3-vision.md](genesis-v3-vision.md) — Core philosophy
- [genesis-v3-build-phases.md](genesis-v3-build-phases.md) — Build order and AZ dependencies
