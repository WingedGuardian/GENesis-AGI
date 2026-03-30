# V4 Feature Spec: Research-Driven Capabilities

**Status:** DESIGNED — not yet implemented. Items sourced from competitive
landscape research (2026-03-14). Scoped for V4 based on dependency chain
and maturity assessment.
**GWT Integration:** Research-driven capabilities run through the LIDA
proposal cycle. New tools and integrations are proposed by modules and
approved by the workspace controller. See
`docs/architecture/genesis-v4-architecture.md` §8.

**Source:** `docs/plans/2026-03-08-research-insights-and-followups.md` §7

---

## 1. Hot-Reload Tool Discovery

**Inspiration:** AWS Strands SDK — `Agent(load_tools_from_directory=True)`

**Current state:** Tools registered programmatically in MCP servers. Adding
a new tool requires code changes and restart.

**Target state:** Directory-based tool discovery — drop a Python file in a
tools directory, it auto-registers as an MCP tool. Uses `@tool` decorator
with docstring-based descriptions (Strands pattern).

**Why V4:** Current tool count (24 across 4 MCP servers) is manageable.
V4 introduces expanded capabilities that will increase tool count. Hot-reload
also enables AI Functions (§2 below).

**Dependencies:** MCP server architecture modification, tool validation layer.

**Design notes:**
- Scan `tools/` directory on startup and on filesystem change (inotify)
- Each `.py` file exports a function decorated with `@tool`
- Docstring becomes tool description for LLM
- Type hints become parameter schema
- Validation: tool must pass a dry-run before registration

---

## 2. AI Functions / Runtime Capability Expansion

**Inspiration:** AWS Strands Labs AI Functions

**Current state:** Genesis learns procedures from experience (Self-Learning
Loop, Phase 6). Procedures are stored as memory, retrieved and applied by
the LLM as prompt context.

**Target state:** Genesis can define new capabilities at runtime using natural
language specifications + validation conditions, with automatic code generation
and validation. Essentially: procedures that can produce and execute code,
not just guide the LLM.

**Why V4:** Requires Phase 6 (Learning Fundamentals) to be operationally
mature. V3 builds the procedure learning infrastructure; V4 extends it
to code generation.

**Dependencies:** Hot-reload tool discovery (§1), sandboxed code execution,
6+ months procedure learning data.

**Design notes:**
- `@ai_function` decorator: natural-language description + Python validator
- Agent loop generates code, validates against spec, auto-retries on failure
- Safety boundary: sandboxed execution only (no filesystem/network without
  explicit tool grants)
- Graduated trust: new AI Functions start at L1 autonomy, can be promoted

---

## 3. Agent-to-Agent Protocol (A2A)

**Inspiration:** AWS Strands (handoffs, swarms, graph workflows), Google ADK

**Current state:** Genesis is a single-agent system. Multi-agent coordination
happens via CC session dispatch (one-way: dispatch → result).

**Target state:** Standardized protocol for Genesis to coordinate with other
agent systems (other Genesis instances, third-party agents, tool-providing
agents).

**Why V4:** A2A standards are nascent but converging. V4 is the right time
to prototype — standards will be more stable by then, and Genesis's
multi-session dispatch architecture provides a natural extension point.

**Dependencies:** A2A protocol standardization (Strands, ADK, or new standard).

**Design notes:**
- MCP is already an agent-to-tool protocol — A2A could extend MCP semantics
- Start with simple request/response between Genesis instances
- Handoff pattern (transfer context to another agent) most immediately useful
- Do NOT build swarm orchestration in V4 — that's V5+ complexity

### Migration trigger (from "designed" to "implementing")

1. A2A protocol standards stabilize (one winner emerges or interop layer exists).
2. Genesis runs multiple instances that need to coordinate.
3. External tool agents become available that Genesis could delegate to.

---

## 4. API-to-MCP Gateway

**Inspiration:** AWS AgentCore Gateway

**Current state:** Genesis wraps external APIs manually as MCP server tools.
Each integration requires writing adapter code.

**Target state:** Automated conversion of REST API specs (OpenAPI/Swagger)
into MCP-compatible tools. Point at an API spec, get MCP tools.

**Why V4:** V3 tool count is manageable with manual wiring. V4's expanded
capabilities will require more integrations — automation reduces the marginal
cost of each new API.

**Dependencies:** Hot-reload tool discovery (§1), OpenAPI spec parser.

**Design notes:**
- Input: OpenAPI spec URL or file
- Output: generated MCP tool definitions with proper schemas
- Auth: support API key, OAuth, bearer token injection
- Validation: generated tools must pass schema validation before registration
- Consider: AWS AgentCore Gateway wraps REST APIs and Lambda functions into
  MCP-compatible tools with "a few lines of code" — study their implementation

---

## 5. Tool Search API (Deferred Tool Loading)

**Inspiration:** Anthropic Claude API Tool Search Tool

**Current state:** Genesis's MCP servers expose all tools upfront. Tool
definitions consume context tokens on every API-routed CC session. At 24
tools this is manageable (~5k tokens). Beyond 30-50 tools, Claude's tool
selection accuracy degrades significantly.

**Target state:** API-routed sessions use `defer_loading: true` on most
MCP tools, with a `tool_search_tool` (regex or BM25) to discover tools
on-demand. Only 3-5 most frequently used tools loaded immediately. Reduces
tool definition context by ~85%.

**Why V4:** Tool count is 24 in V3 — below the accuracy cliff. V4 adds
hot-reload tools (§1), API-to-MCP gateway (§4), and capability modules,
pushing tool count toward 50+. Tool search becomes necessary at that scale.

**Dependencies:** Hot-reload tool discovery (§1) — more tools means more
need for deferred loading. Also requires API-routed sessions (surplus system).

**Key specs (from Anthropic docs):**
- Up to 10,000 tools in catalog
- Returns 3-5 most relevant tools per search
- Two variants: regex (`tool_search_tool_regex_20251119`) and BM25
  (`tool_search_tool_bm25_20251119`)
- Works with MCP servers via `mcp_toolset` with `default_config`:
  `{ "defer_loading": true }`
- Supports custom implementation: return `tool_reference` blocks from
  your own search tool (could use Genesis's Qdrant embeddings for semantic
  tool search)
- Sonnet 4.0+, Opus 4.0+ only

**Design notes:**
- Keep 3-5 core tools non-deferred (memory ops, awareness, outreach)
- Defer all specialty tools (research, content, capability modules)
- Custom embedding-based tool search using Qdrant is more sophisticated
  than regex/BM25 and uses existing infra — evaluate at implementation time
- Namespace tool names by MCP server (e.g., `memory_`, `research_`,
  `health_`) so regex patterns naturally surface tool groups

## 6. Context-Efficient CC Sessions (Sandbox Pattern)

**Inspiration:** Context Mode MCP server (github.com/mksglu/context-mode)

**Current state:** When Genesis's CC sessions run tools that produce large
output (web search results, code search, file reads), raw data floods the
context window. A single Playwright snapshot can be 56KB. This limits how
much work a CC session can accomplish before hitting context limits.

**Target state:** Sandbox execution for CC sessions — tool outputs captured
in isolated processes, only refined results enter context. Large outputs
auto-indexed into SQLite FTS5, searchable by intent. Claimed 98% context
reduction for typical workflows.

**Why V4:** V3 CC sessions are short-lived (single reflection, single task).
V4 introduces longer autonomous sessions (strategic reflection, multi-step
tasks) where context efficiency becomes critical.

**Dependencies:** Phase 9 CC lifecycle hooks (V3), session continuity
pattern (V3 Phase 9).

**Design notes:**
- Adopt sandbox pattern for tool outputs >5KB: index into FTS5, return
  summary + search vocabulary for follow-up queries
- Intent-driven filtering: when output exceeds threshold and intent is
  stated, index full result and return only intent-relevant sections
- Progressive throttling on repeated searches to prevent context abuse
- Complements Tool Search (§5): deferred loading reduces tool definition
  context, sandbox reduces tool output context — together they dramatically
  extend session lifetime

---

## 7. Agentic Retrieval Loop (System-Wide)

**Inspiration:** NVIDIA NeMo Retriever — #1 ViDoRe v3, iterative agent-retriever
loop (think → retrieve → evaluate → reformulate → retrieve again).

**Current state:** Single-pass hybrid retrieval (Qdrant vectors + FTS5 + RRF).

**Target state:** Wrap memory_recall in a ReACT loop for background tasks.
Apply anywhere latency tolerates: surplus, recon, deep/strategic reflection,
procedure recall reformulation. Single-pass stays for latency-sensitive paths.

**Why V4:** 136s/query latency at Opus quality. Needs cheaper models and/or
distilled patterns before viable for routine use. Background tasks can absorb
the latency now; interactive paths need V4 model improvements.

**Dependencies:** Router call site for agentic retrieval, ReACT loop framework.

---

## 8. Bayesian Behavioral Reinforcement

**Inspiration:** Google Bayesian Teaching for LLMs (March 2026).

**Current state:** Procedure Activation Architecture (hooks, injection, rules).
Mechanical enforcement. LLM still drifts from instructions under task pressure.

**Target state:** Statistical complement — track instruction adherence per rule,
maintain probability distributions, reinforce rules that work, surface ignored
rules more aggressively. Bayesian updating for user model priors.

**Why V4:** Requires instrumentation of instruction following (which rules get
followed, which get ignored) that doesn't exist yet. Procedure Activation is the
V3 mechanical foundation; Bayesian is the V4 statistical layer on top.

**Dependencies:** Procedure Activation Architecture (V3, complete), compliance
instrumentation, Bayesian update framework.

---

## 9. Revenue Multiplier: Agent Development as a Service

**Inspiration:** "Selling AI Agents" ecosystem + HN discussion.

**Target state:** Genesis as force multiplier for one-person AI agency. Research
requirements → design → scaffold → implement → test → deploy → learn from each
project. User is the credibility proxy; Genesis is the factory.

**Why V4:** Requires Genesis working on external projects (not just itself),
multi-repo awareness, client requirement → architecture pipeline.

---

## 10. Revenue Multiplier: Mobile App Development

**Inspiration:** Xcode + Claude/Codex video.

**Target state:** Genesis builds mobile apps for the user. Vibe coding for
prototyping, CC sessions for real builds. Cross-platform framework (Flutter/RN)
or remote Mac for Xcode.

**Why V4:** Same dependency as §9 — Genesis on external projects.

---

## 11. Channel Expansion

**Inspiration:** OpenClaw 8+ channels vs Genesis 2.

**Items:**
- Voice channel via Vapi/Telnyx (STT→LLM→TTS, ~$30-80/mo)
- Channel framework generalization (common interface behind all channels)
- Proactive callbacks (Genesis calls user for high-priority alerts)
- CC 2.0 Telegram integration investigation (may simplify bridge)

**Why V4:** Architecture compatible. Channel framework design needed before
adding channels piecemeal.

---

## 12. Antigravity / ADK Integration

**Inspiration:** Google Cloud Tech — vibe coding secure agents with ADK.

**Items:**
- ADK callback pattern (before/after model) for router
- Antigravity dispatch from Genesis (like CC dispatch)
- Model Armor pattern for content sanitization

**Why V4:** Different stack. Needs investigation into CC + Antigravity interop.

---

## 13. Infrastructure Maturation

**Items:**
- Model-level pre/post callbacks in router (Google ADK pattern)
- Quick security wins from NVIDIA OpenShell (cherry-pick, not adopt)
- MCP event-driven integration (dependent on spec, ~June 2026)
- Autonomous value metrics (hours saved, accuracy, tasks completed)

---

## 14. GitHub Issue Integration (Deferred from V3 P-2)

Genesis currently operates as a private single-developer repo. Tying GitHub API
calls into the awareness loop core is premature. When the repo goes public or
issue tracking becomes relevant:

- **Bridge pattern**: GitHub issues as a recon MCP source, not awareness loop signal
- **Scope**: Issue creation, labeling, milestone tracking, PR linkage
- **Trigger**: Public repo transition or multi-developer collaboration
- **Key constraint**: Must not add latency to awareness tick; async ingest only

This plugs into the recon subsystem without touching core awareness/perception.

---


## Deferred from Operational Vitals (2026-03-22)

- **Autonomous provider switching/failover**: When the activity tracker detects
  a provider is consistently failing, automatically switch to an alternate
  provider without user intervention. Requires the executor architecture
  (jolly-cooking-bee.md) and multi-step reasoning about *what to do* about
  health signals — beyond the V3 "alert the user" pattern.

*Document created: 2026-03-14*
*Last updated: 2026-03-22*
*Status: Designed — implementation during V4 planning*
