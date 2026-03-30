# Agent Zero Architecture Deep Dive

> **What this is:** A permanent reference documenting Agent Zero's internal
> architecture as observed in the Genesis fork (pinned at `fa65fa3`, v0.9.8.2).
> Written from source code inspection, not AZ documentation.
>
> **Why it exists:** Genesis builds on top of AZ. Every integration decision —
> where to hook in, what to replace, what to leave alone — depends on
> understanding AZ's actual behavior, not what we assume it does.
>
> **Last audited:** 2026-03-04

---

## Event Loop Topology

Agent Zero runs 5 distinct event loops across multiple threads:

```
Main Thread (uvicorn)
├── Flask/async web server (port 5000)
├── WebSocket handler (chat, logs)
└── API endpoint handlers

DeferredTask Threads (N per background job)
├── Each gets its own asyncio event loop
├── job_loop.py — built-in periodic tasks
├── Extension background work (observation extraction, learning signals)
└── NOT on the main uvicorn loop — isolated

Agent Monologue Loops
├── One per active agent (agent_0, sub-agents)
├── Run inside the request context OR DeferredTask thread
├── Synchronous within a single agent: collect prompts → LLM call → parse → tool exec → repeat
└── Max iterations configurable per agent
```

### nest_asyncio

AZ calls `nest_asyncio.apply()` in 4 locations (`run_ui.py`, `instrument.py`,
`mcp_handler.py`, `communication.py`) to allow nested event loop re-entry. This
is a pragmatic workaround for running async code in contexts that already have a
running loop.

**Genesis constraint:** Genesis MUST NOT call `nest_asyncio.apply()`. The
Awareness Loop's APScheduler runs in its own thread (via `DeferredTask`) with
its own event loop. `asyncio.Lock` works correctly in that context. If Genesis
code ever runs on the main uvicorn loop, nest_asyncio reentrancy could cause
unpredictable interleaving. Keep Genesis async work isolated in DeferredTask
threads.

### DeferredTask Pattern

```python
# ~/agent-zero/python/helpers/defer.py
class DeferredTask:
    def __init__(self, agent, callable, *args, **kwargs):
        self.thread = Thread(target=self._run, daemon=True)

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.callable(*self.args, **self.kwargs))
```

Each DeferredTask spawns a daemon thread with its own event loop. This is how
AZ runs background work (observation extraction, memory operations) without
blocking the chat loop. Genesis should use this pattern for:
- Awareness Loop scheduler (APScheduler in a DeferredTask thread)
- Background reflection (observation extraction, learning signals)
- Any long-running Genesis computation

---

## Extension System

Extensions are AZ's primary behavior injection surface. They are Python files
in hook-point directories that execute at specific lifecycle moments.

### Hook Points (23 total)

| Hook | Fires When | Can Modify |
|------|-----------|------------|
| `agent_init` | Agent object created | Agent config, initial state |
| `system_prompt` | System prompt assembled | Prompt content (append/prepend) |
| `message_loop_start` | Each monologue iteration begins | Loop flags, early exit |
| `message_loop_prompts_before` | Before prompt assembly | Nothing (observation only) |
| `message_loop_prompts_after` | After prompt assembly | `loop_data.system` (inject context) |
| `message_loop_end` | Each iteration completes | Nothing (observation only) |
| `monologue_start` | Monologue begins | Nothing (observation only) |
| `monologue_end` | Monologue finishes | Nothing (observation only) |
| `response_stream*` | Token-by-token streaming | Can filter tokens |
| `reasoning_stream*` | Reasoning token streaming | Can filter tokens |
| `tool_execute_before` | Before tool runs | Can block or modify args |
| `tool_execute_after` | After tool completes | Can filter/modify result |
| `hist_add_before` | Before adding to history | Can modify message |
| `hist_add_after` | After adding to history | Nothing (observation only) |
| `error_format` | Error formatting | Error display |
| `banners` | UI banner generation | Banner content |
| `user_message_ui` | User message display | UI formatting |

### Execution Order

Extensions execute **alphabetically by filename** within each hook directory.
AZ uses numeric prefixes to control ordering:

```
_10_ — Infrastructure (early init)
_20_ — Prompts (behaviour, system)
_50_ — Memory operations (recall, memorize)
_91_ — Recall wait (sync barrier)
```

### What Extensions CAN Do

- Append/prepend content to system prompts and loop data
- Read agent state (`self.agent.data`, config, history)
- Call MCP tools (via agent's tool interface)
- Spawn DeferredTask background work
- Set flags in `loop_data` for the current iteration
- Write to agent's data dict for cross-iteration persistence

### What Extensions CANNOT Do

- Force tick timing or bypass the monologue loop
- Spawn new monologue iterations — only the scheduler/user can start one
- Directly call LLM APIs — must go through `agent.call_chat_model()` or
  `agent.call_utility_model()`
- Modify other extensions' behavior at runtime
- Access the database directly (must go through MCP or tools)

### Genesis Extension Naming Convention

All Genesis extension files use the `genesis_` prefix in their filenames
to avoid collisions with future AZ built-in extensions:

```
_51_genesis_awareness_briefing.py   (not _51_awareness_briefing.py)
_52_genesis_extract_observations.py (not _52_extract_observations.py)
```

The numeric prefix controls execution order relative to AZ built-ins;
the `genesis_` prefix prevents name collisions.

---

## Model Layer

### unified_call() Interface

All LLM calls in AZ go through a single function:

```python
# ~/agent-zero/models.py
async def unified_call(
    agent: Agent,
    model_type: str,          # "chat_model" or "utility_model"
    messages: list[dict],     # Standard chat format
    *,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    stream: bool = True,
    tools: list | None = None,
    tool_choice: str | None = None,
    response_format: dict | None = None,
) -> ModelResponse:
    ...
```

This wraps LiteLLM's `acompletion()` with:
- Rate limiting per model (token bucket)
- Streaming assembly
- Error handling and retry
- Token counting
- Cost tracking (LiteLLM provides per-call cost)

### LiteLLMChatWrapper

The actual LLM call goes through `LiteLLMChatWrapper.call()`:

```python
class LiteLLMChatWrapper:
    async def call(self, messages, **kwargs):
        # Applies rate limiting
        # Calls litellm.acompletion()
        # Handles streaming
        # Returns assembled response
```

### Rate Limiting

AZ has a built-in `RateLimiter` class (token bucket algorithm) applied
per-model. Genesis cost tracking supplements this — it doesn't duplicate it.

### Genesis Integration Point

Genesis MUST route all LLM calls through `unified_call()` (via
`agent.call_chat_model()` or `agent.call_utility_model()`). Genesis adds:
- Circuit breakers (health-mcp tracks provider health)
- Fallback chains (model routing registry defines primary → fallback)
- Cost tracking (per-call cost from LiteLLM, aggregated in SQLite)
- Output validation contracts (per call site)

Genesis does NOT replace `unified_call()` — it wraps the call sites that
invoke it, adding pre-call routing logic and post-call tracking.

---

## MCP Integration

### Initialization Sequence

1. `AgentConfig` loads `mcp_servers` from settings
2. `MCP_Handler` initializes per-server on first tool call (lazy)
3. Each server runs as a subprocess (stdio transport)
4. Tools discovered via `list_tools()` on first connection
5. Tool names prefixed with server name: `memory-mcp.memory_recall`

### Session Behavior

AZ creates **ephemeral MCP sessions** — each `call_tool()` can open a new
connection. Genesis MCP servers must manage their own state externally
(SQLite, Qdrant) and not rely on session persistence.

### Tool Naming

MCP tools are namespaced by server: `{server_name}.{tool_name}`. Agent sees
them in its tool list alongside native tools. Extensions call tools through
the agent's standard tool interface.

### Secret Resolution

MCP server configurations support secret placeholders:
`{{secret_name}}` in args/env is resolved from `SecretsManager` at startup.
Genesis MCP servers should use this for database paths, API keys, etc.

---

## Plugin System

### plugin.yaml Format

```yaml
title: "Genesis Core"                    # Required: display name
description: "Genesis cognitive layer"   # Required: what it does
version: "0.1.0"                        # Required: semver
always_enabled: true                    # Optional: skip enable/disable UI
```

Both Genesis stubs (`genesis/plugin.yaml` and `genesis-memory/plugin.yaml`)
are correctly formatted and will be discovered by AZ's plugin system.

### Plugin Discovery

Plugins live in `~/agent-zero/usr/plugins/{name}/`. Each plugin can contain:
- `plugin.yaml` — manifest (required)
- `extensions/python/{hook_point}/` — extension files
- `tools/` — tool implementations
- `prompts/` — prompt templates
- `api/` — API endpoint handlers
- `helpers/` — shared utility code

### Priority Rules

Plugin extensions execute AFTER built-in extensions at the same numeric
prefix. Within the same priority level, alphabetical order applies.
Genesis uses higher numeric prefixes (_51_, _52_, _53_) to run after
AZ's built-in _50_ extensions.

---

## Task Scheduler

AZ has a built-in `TaskScheduler` (`~/agent-zero/python/helpers/task_scheduler.py`)
with:
- Cron expression support
- UI integration (task list, manual trigger)
- Persistence across restarts
- Context management (which agent runs which task)

### Genesis Scheduling Strategy

| Category | Mechanism | Example |
|----------|-----------|---------|
| Event-driven reflection | Genesis APScheduler (via DeferredTask) | 5-min awareness tick |
| Genesis rhythms | Genesis APScheduler | Morning report, calibration |
| User-scheduled crons | AZ's TaskScheduler | "Check email every morning" |

Category 3 (user crons) should use AZ's `TaskScheduler` because it already
has cron support, UI integration, and persistence. Genesis doesn't need to
rebuild this.

---

## Memory System

### What Genesis Replaces

AZ's memory is FAISS-based (LangChain wrapper):
- `~/agent-zero/python/helpers/memory.py` — `Memory` class wrapping `MyFAISS`
- Storage: `usr/memory/{subdir}/index.faiss` + `index.pkl`
- Embeddings: Ollama via LangChain `CacheBackedEmbeddings`
- Areas: MAIN, FRAGMENTS, SOLUTIONS (plus SKILLS in newer versions)
- Documents: LangChain `Document` with `page_content` + `metadata`

Genesis replaces with: Qdrant (vectors) + SQLite FTS5 (metadata/full-text).

### Memory Dashboard

AZ provides a full memory management UI (Alpine.js + Flask):

**Frontend:** `webui/components/modals/memory/memory-dashboard.html`
**Store:** `webui/components/modals/memory/memory-dashboard-store.js`
**API:** `python/api/memory_dashboard.py` — single endpoint, action-based

Features: search with similarity threshold, area filtering, pagination,
view/edit/delete individual memories, bulk operations, live polling (2s).

**API contract (what the frontend expects):**

```json
{
  "success": true,
  "memories": [
    {
      "id": "string",
      "area": "main|fragments|solutions",
      "timestamp": "ISO string",
      "content_full": "full memory text",
      "knowledge_source": false,
      "source_file": "optional path",
      "tags": ["array", "of", "tags"],
      "metadata": { "full metadata dict" }
    }
  ],
  "total_count": 42,
  "total_db_count": 150,
  "knowledge_count": 30,
  "conversation_count": 120
}
```

**API actions:** `search`, `delete`, `bulk_delete`, `update`,
`get_memory_subdirs`, `get_current_memory_subdir`

### Memory Dashboard Swap Strategy

The dashboard API handler (`memory_dashboard.py`) calls `Memory` class
methods — not FAISS directly:
- `memory.search_similarity_threshold(query, limit, threshold, filter)`
- `memory.db.get_all_docs()`
- `memory.delete_documents_by_ids(ids)`
- `memory.update_documents(docs)`

**Decision:** Rewrite the `Memory` class internals to backend to Qdrant +
SQLite while keeping the same public API. The dashboard UI and API handler
stay untouched. This is a documented AZ core patch (Phase 5).

The `Memory` class is ~400 lines with a small public surface (5-6 methods).
The swap is honest and maintainable — tracked in `agent-zero-fork-tracking.md`.
Users manage Genesis memories through AZ's existing dashboard with zero
frontend changes.

### Memory Extensions (What Genesis Keeps)

AZ's memory extensions call tool interfaces, not FAISS directly:

| Extension | Hook | What It Does |
|-----------|------|-------------|
| `_50_recall_memories.py` | `message_loop_prompts_after` | Calls `memory_load` tool |
| `_50_memorize_fragments.py` | `monologue_end` | Calls `memory_save` tool via utility model |
| `_51_memorize_solutions.py` | `monologue_end` | Calls `memory_save` tool |

These extensions don't need modification. They call tool names
(`memory_save`, `memory_load`), and the Genesis memory plugin provides
those same tools backed by Qdrant + SQLite instead of FAISS.

### Fragment Extraction Pipeline

`_50_memorize_fragments.py` fires at `monologue_end`:
1. Sends conversation to utility model with extraction prompt
2. Model returns 1-5 memory fragments (JSON)
3. Each fragment saved via `memory_save` tool
4. Consolidation logic (merge/replace/update/keep) runs on duplicates

Genesis doesn't need to change this pipeline. The tool backend swap is
transparent to the extraction logic.

---

## Secrets Management

### SecretsManager

`~/agent-zero/python/helpers/secrets.py` loads secrets from:
1. `usr/secrets.env` (primary, gitignored)
2. Environment variables (fallback)

Secrets are resolved in multiple contexts:
- MCP server configurations (startup)
- Tool arguments (before execution)
- System prompts (template variables)

### StreamingSecretsFilter

Applied to LLM streaming output to mask any secret values that appear
in responses. Handles chunk boundaries (secret split across chunks).

### 8+ Masking Points

| Point | Mechanism |
|-------|-----------|
| LLM streaming output | `StreamingSecretsFilter` |
| Tool execution args | `tool_execute_before` extension |
| Tool execution results | `tool_execute_after` extension |
| MCP tool responses | `tool_execute_after` hook |
| History messages | `hist_add_before` extension |
| Error messages | Error formatting pipeline |
| Log output | Logger filter |
| WebSocket messages | Output filter |

### Genesis Rules

- Don't return raw secrets in MCP tool responses
- Sanitize error messages before returning
- Don't log unmasked tool arguments
- Use `{{secret_name}}` placeholders in MCP server configs
- Real keys in `usr/secrets.env`, not in code or MCP configs

---

## History & Compression

### Multi-Tier System

AZ manages context windows with a 3-tier compression system:

```
Full History (all messages)
    ↓ when approaching context limit
Current Tier (50% of budget) — recent messages, uncompressed
Topics Tier (30% of budget)  — summarized by topic
Bulk Tier (20% of budget)    — heavily compressed oldest messages
```

### How Compression Works

1. **Trigger:** Total tokens approach model's context limit
2. **Extension:** `_50_memory_compression_start.py` (message_loop_prompts_before)
   sets a wait flag; `_50_memory_compression_end.py` (hist_add_after) runs
   the actual compression
3. **Compression:** Utility model summarizes messages into topic buckets
4. **Result:** Older messages replaced with compressed summaries; recent
   messages kept verbatim

### Genesis Constraints

- **DO NOT** duplicate compression — AZ handles it
- **DO NOT** call `history.compress()` directly
- **DO NOT** maintain a separate history
- **DO NOT** re-implement token counting
- **DO** use `history.output()` for retrospective analysis (read-only)
- Phase 5 (Memory Operations) layers on top of AZ's compression, not
  parallel to it

---

## Sub-Agent Architecture

AZ supports hierarchical agents:

```
agent_0 (main)
├── agent_1 (sub-agent, e.g., code execution)
├── agent_2 (sub-agent, e.g., web search)
└── agent_N
```

### Context Sharing

- Sub-agents share the parent's `AgentContext` (settings, MCP connections)
- Each sub-agent has its own history and monologue loop
- Results flow back to parent via tool response
- Sub-agents can recursively create their own sub-agents (depth-limited)

### Genesis Consideration

Genesis's monitor agent (`genesis-monitor/`) runs as a sub-agent within
AZ's hierarchy. It shares MCP connections with the main agent, which means
Genesis MCP servers are accessible from both the main conversation and the
monitor sub-agent.

---

## Cross-References

- **Integration architecture:** `docs/architecture/genesis-agent-zero-integration.md`
- **Fork tracking:** `docs/reference/agent-zero-fork-tracking.md`
- **Model routing:** `docs/architecture/genesis-v3-model-routing-registry.md`
- **Build phases:** `docs/architecture/genesis-v3-build-phases.md`
- **AZ source:** `~/agent-zero/` (fork pinned at `fa65fa3`)
