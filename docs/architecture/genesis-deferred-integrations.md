# Genesis — Deferred Design Integrations

> Items evaluated during a thorough review of ~50 external sources against the
> Genesis v3 architecture. Each was confirmed as genuinely valuable but belongs
> in a later version — either because it requires operational data that won't
> exist until V3 is running, or because the complexity isn't justified until
> simpler foundations are proven.
>
> **Status:** These are architectural intentions, not speculative ideas. Each
> has a specific target location in the design docs and will be integrated when
> its version begins.
>
> **Companion document:** The 10 items that were integrated NOW live in
> `genesis-v3-autonomous-behavior-design.md` and `genesis-v3-build-phases.md`
> (committed 2026-03-01).

---

## V4 — The Self-Tuning Copilot

V4 activates when V3 has ~1-2 months of operational data. These items
require either that data or the meta-prompting infrastructure V4 introduces.

### DSPy Algorithmic Prompt Optimization

**Source:** Stanford NLP — DSPy framework
**Target:** Reflection Engine prompt templates

Replace hand-tuned prompt templates (Micro, Light, Deep, Strategic) with
DSPy-optimized versions. DSPy treats prompts as programs with trainable
parameters — it uses operational data (which reflections produced actionable
outputs? which were noise?) to algorithmically optimize prompt structure,
few-shot examples, and instruction phrasing.

**Why V4:** Requires a corpus of reflection inputs/outputs with quality labels
to optimize against. V3 generates this corpus; V4 uses it.

**Prerequisite:** Reflection Engine must track which outputs were acted on vs
ignored (already designed in V3 Self-Learning Loop).

### GraphRAG Entity-Relationship Triples

**Source:** R2R / SciPhi-AI
**Target:** memory-mcp schema and retrieval

Enrich memory-mcp with entity-relationship graphs extracted from stored
memories. Current design uses embedding similarity for retrieval. GraphRAG
adds structured triples (entity → relationship → entity) that enable
traversal queries: "what entities are connected to X?" "what's the path
between A and B?"

**Why V4:** The entity extraction pipeline needs LLM calls during memory
storage (adds cost and latency). V3 should prove the basic memory system
works before adding extraction overhead. Also requires enough stored
memories to make graph traversal valuable.

**Integration point:** memory-mcp `store` operation gains an optional
entity extraction step. `retrieve` gains a graph-traversal mode alongside
embedding similarity.

### Pi Self-Extension Pattern (Write → Save → Dynamic-Load)

**Source:** Armin Ronacher / badlogic — Pi pattern
**Target:** Capability Expansion Pipeline (Spiral 16)

Genesis writes its own tools/extensions → saves them to a registry →
dynamic-loads them at runtime. Currently, the Capability Expansion Pipeline
proposes capability acquisitions for user approval and manual installation.
The Pi pattern automates the last mile: Genesis writes the tool code via
Claude Code (CC background session), saves it to a tools directory, and
the Agent Zero framework discovers it on the next tick.
<!-- Updated 2026-03-07: claude_code tool reference → CC background session per agentic-runtime-design -->

**Why V4:** Requires trust in Genesis's code quality (earned through V3
operational track record) and the Strategic reflection infrastructure to
decide WHAT to build. Also requires sandboxing — auto-loaded tools must
run in a restricted environment until validated.

**Safety requirement:** New self-written tools start sandboxed (no
filesystem access, no network, no memory writes) and graduate to full
permissions after N successful invocations. L6+ autonomy for graduation.

### Idea-Lineage Tracking

**Source:** Vin's `/trace` command (Obsidian + Claude Code)
**Target:** memory-mcp episodic memory schema

Add `idea_thread_id` field to episodic memory records that tracks how an
idea evolves across multiple reflection sessions. Currently, related
observations are linked by embedding similarity but not by explicit
lineage. `/trace` enables queries like: "show me how my thinking about X
evolved from first mention to current understanding."

**Why V4:** Useful but not critical for V3. Requires enough reflection
history to make lineage tracking meaningful. Also needs UI/reporting to
surface lineage to the user (V3 has no rich output formatting yet).

**Schema addition:**
```json
{
  "idea_thread_id": "thread_abc123",
  "thread_position": 3,
  "prior_entry_id": "mem_xyz789",
  "evolution_type": "refinement|contradiction|expansion|merger"
}
```

### Cross-Domain Bridging

**Source:** Vin's `/connect` command (Obsidian + Claude Code)
**Target:** Cognitive Surplus task types (Phase 3)

New Cognitive Surplus task type: "What connects [topic_A] and
[dormant_topic_B]?" Explicitly bridges two seemingly unrelated domains
using the memory graph. Current surplus tasks are open-ended ("what's
interesting?"). This adds a structured creativity task that the system
can run during idle cycles.

**Why V4:** Requires GraphRAG (above) to work well — bridging domains
needs relationship traversal, not just embedding similarity. Also more
valuable with a larger memory corpus.

### Progressive Disclosure for Reflection Engine

**Source:** Cole Medin's second brain skills architecture
**Target:** Reflection Engine inline capabilities

Load inline capabilities (salience evaluation, social simulation,
governance check, drive weighting) on-demand per reflection instead of
including all of them in every prompt. The meta-prompter (Pattern 2)
selects which capabilities to load alongside which questions.

**Why V4:** V3 uses static prompt templates where all capabilities are
always present. V4's meta-prompting infrastructure naturally supports
selective capability loading — the meta-prompter already decides what
questions to ask, it can also decide what tools to provide.

**Benefit:** Smaller prompts → less positional bias (Weakness #7), lower
token cost per reflection, and capabilities can be added without
proportionally growing every prompt.

### MCP-on-Demand Tool Loading

**Source:** Cole Medin's file-to-knowledge RAG pipeline
**Target:** Agent Zero ↔ MCP server connection architecture

Load MCP tool schemas on trigger rather than at startup. Currently Agent
Zero connects to all 4 MCP servers at initialization and loads all tool
schemas into context. With 4 servers × ~5 tools each, this is manageable.
But as tools grow, on-demand loading prevents context bloat.

**Why V4:** V3's 4 servers with ~20 total tools don't warrant the
complexity. V4 may add more MCP servers (knowledge-base-mcp, aws-mcp)
where on-demand loading becomes necessary.

### Parallel Experiment + Reflection

**Source:** GEA paper (UCSB) — group evolution mechanics
**Target:** Strategic reflection → CC background session
<!-- Updated 2026-03-07: claude_code tool → CC background session per agentic-runtime-design -->

When Strategic reflection proposes a code modification, try 2-3
implementation approaches in parallel (using git worktrees), then run a
"reflection pass" that compares all approaches before committing the
winner. The losing approaches' lessons get persisted.

**Why V4:** Requires enough operational confidence to run parallel
experiments autonomously. V3's scope is conservative single-path execution.
CC background sessions (Opus, high thinking) can handle this in V4.

### Procedure Auto-Healing

**Source:** Kane AI auto-heal concept
**Target:** Self-Learning Loop procedural memory

When a previously-confirmed procedure fails during execution, check if
the underlying interface changed (API updated, dependency version bumped,
file structure reorganized) and automatically update the procedure's steps
rather than just logging the failure. The principle stays the same; the
steps adapt.

**Why V4:** Requires reliable interface-change detection (comparing current
environment against procedure's stored context). V3 should accumulate
failure data first; V4 can build the auto-heal logic on top of that data.

**Leverages:** Dual-level storage already in V3 design (principle + steps).
Auto-heal updates steps while preserving principle.

### Dual-Index Memory

**Source:** Cole Medin's agentic RAG pipeline
**Target:** memory-mcp retrieval system

Index episodic memory both semantically (embedding similarity for "what
happened?") AND structurally (SQL-queryable fields for "show me all tasks
that cost > $1" or "which procedures have success_rate < 0.5").

**Why V4:** V3's memory-mcp already stores structured fields (cost_usd,
success_rate, duration_s in execution traces). The SQL query interface is
a natural V4 addition once there's enough data to make structured queries
useful. Adding it to V3 is premature — the fields would be sparsely
populated.

### Raw Data Preservation During Consolidation

**Source:** Brad Bonanno's second brain — "never replace source material"
**Target:** Deep reflection memory consolidation

When memory consolidation produces summaries of older entries, keep the
originals accessible in cold storage rather than deleting them. Currently
the design says older entries get "summarized and archived." Clarify:
archived means moved to a lower-priority storage tier, not deleted.
The summary replaces the original in active retrieval; the original is
accessible via explicit lookup.

**Why V4:** V3's consolidation is straightforward (summarize + archive).
The distinction between "archive" and "cold storage" becomes important
when there's enough memory volume that storage costs matter and when
the system needs to verify its own summaries against originals.

### Middleware Governance Architecture

**Source:** NVIDIA NeMo Agent Toolkit — middleware pattern
**Target:** Task Execution Architecture → governance checks

Refactor governance checks (autonomy permissions, budget checks, reversibility
assessment, cost-tier checks) from inline conditionals in the execution flow
into a composable middleware chain. Each concern becomes a middleware layer
that wraps sub-agent dispatch:

```
sub-agent dispatch request
  → AuthorizationMiddleware (autonomy permissions)
  → BudgetMiddleware (cost check + estimation)
  → ReversibilityMiddleware (blast radius assessment)
  → CostTierMiddleware (engine selection by complexity)
  → LoggingMiddleware (audit trail)
  → actual dispatch
```

**Why V4:** V3's governance checks are straightforward enough to implement as
inline checks (4-5 conditionals before dispatch). The middleware pattern adds
value when: (a) more checks are added (V4 adds meta-prompting governance,
channel learning permissions), (b) checks need to be composed differently per
context (background vs. foreground vs. outreach), or (c) third-party integrations
need their own governance middleware.

**Benefit:** Adding a new governance concern = adding one middleware class instead
of modifying every dispatch site. Easier to test each concern in isolation.

**Leverages:** V3's existing governance check design (same checks, different wiring).

### Adaptive Web Resilience & Tool Registry
<!-- Added 2026-03-08: GL-1 post-implementation discussion -->

**Source:** GL-1 implementation experience (403 errors fetching articles)
**Target:** Research MCP server + Self-Learning Loop procedures

The web landscape changes daily — sites block LLM requests, new workaround tools
emerge, existing tools stop working. Genesis needs a dynamic registry of web access
methods that it maintains through its learning system, not a static fallback chain.

V3 establishes a static fallback chain (direct fetch → search cache → archive.org
→ headless browser). V4 makes this adaptive: each method becomes a learned
procedure with effectiveness scores per site/category, updated as methods succeed
or fail. Genesis proactively researches new tools (agentic web APIs, new MCP
servers, reader services like Jina/Firecrawl) via surplus compute and adds them
to the registry when validated.

**Why V4:** Requires Phase 6 procedural memory to store method effectiveness, and
surplus compute infrastructure to proactively research new tools. V3 can only
hard-code the chain; V4 can learn and evolve it.

**Broader principle:** This pattern applies to ALL obstacle types, not just web
fetching. API rate limits, model unavailability, tool failures, permission errors
— each is an obstacle class with a ranked list of resolution methods that the
learning system maintains. The vision doc's "failure is not an option" philosophy
means exhausting creative workarounds before reporting failure. The question is
always "how do I get past this?" — and the answer should improve over time as
Genesis accumulates experience.

**Key architectural note:** The fallback ordering is itself learned data, not a
developer's static priority list. Today's ranking might be: direct → cache →
archive → Jina → headless browser. Tomorrow Jina gets rate-limited for Genesis's
usage pattern, a new MCP tool appears, and the ranking updates automatically.

### CC Session Infrastructure Self-Introspection
<!-- Added 2026-03-12: TTS bug exposed that CC sessions have no awareness of their own infrastructure -->

**Source:** Live TTS testing — CC responded "no voice capabilities here" while
actively delivering voice responses via the bridge TTS pipeline.
**Target:** CC session ↔ Genesis runtime interface

CC sessions are subprocesses with no access to the Genesis runtime. They cannot
query the provider registry, discover what channel they're on, or know what
capabilities surround them (STT, TTS, web search, memory). This means CC gives
wrong answers about its own infrastructure and can't adapt behavior to available
capabilities.

**V4 approach:** Give CC sessions MCP access to a Genesis self-introspection
endpoint. CC can query on-demand: what channel am I on? What TTS/STT providers
are active? What's my current cost budget? What capabilities does this channel
support? The information is live and universal — no per-channel prompt files to
maintain, no static capability lists that drift from reality.

**Why V4:** Requires MCP-on-Demand Tool Loading (see above) to avoid bloating
every CC session with introspection tools. Also benefits from V3 operational
experience to know which infrastructure questions CC actually needs to answer
(vs. theoretical ones). V3 workaround: user can tell CC about its capabilities
in conversation; the bridge layer handles TTS/STT transparently regardless of
CC's awareness.

**Key principle:** Genesis should know its own infrastructure the way a person
knows their own body — not from a manual, but from direct introspection. The
runtime knows everything; CC sessions should be able to ask.

### Chain-of-Thought Prompt Optimization

**Source:** NVIDIA — LLM prompt engineering and P-Tuning guide
**Target:** Reflection Engine prompt templates

V3 implements a convention that Light+ depth prompts include explicit chain-of-thought
scaffolding. V4 can optimize this further:
- Analyze which CoT patterns (step-by-step, pros/cons, hypothesis-test) produce
  the highest-quality reflections per depth level
- Use DSPy (see above) to algorithmically optimize CoT structure alongside other
  prompt parameters
- A/B test CoT styles during shadow mode

**Why V4:** Requires reflection output quality data from V3 to optimize against.
V3 establishes the CoT convention; V4 tunes it.

---

## V5 — The Autonomous Copilot

V5 activates when V4 has ~3-6 months of operational data. These items
require either population-level mechanics or deep self-modification trust.

### Full GEA Group Evolution

**Source:** GEA paper (UCSB) — Group-Evolving Agents
**Target:** New architectural layer (population management)

Multiple Genesis variants running in parallel with a shared experience
pool. Selection based on performance + novelty scoring. A Reflection
Module extracts group-wide patterns and generates evolution directives
for the next generation. The "super-employee" that possesses combined
best practices of the entire group.

**Why V5:** Requires multiple Genesis instances (infrastructure cost),
a mechanism to evaluate Genesis variants against each other (what does
"better" mean for a personal assistant?), and enough V4 self-modification
capability to generate meaningful variants. Also raises governance
questions: which variant is "the real Genesis" to the user?

**Key GEA finding worth preserving:** Improvements discovered through
evolution are NOT tied to a specific model. Agents evolved on Claude
maintained performance gains when swapped to GPT-5.1 or o3-mini. This
transferability means Genesis could be model-agnostic at the
architectural level.

### Cross-Model Strategy Transfer

**Source:** GEA paper — transferability experiments
**Target:** Multi-provider architecture

Architectural improvements discovered on one model should be maintained
when the underlying engine changes. This requires that improvements are
stored as structured procedures/configurations (not as prompt tricks
that exploit specific model behaviors).

**Why V5:** Requires the Pi self-extension pattern (V4) to be working —
improvements must be stored as code/config, not as prompt adjustments.
Also requires multi-model operational data to validate that transfer
actually works for Genesis's domain.

### HKUDS/FastCode

**Source:** HKUDS — FastCode repository
**Target:** claude_code tool alternative for code understanding tasks

AST + relationship graphs + budget-aware code understanding. Could
become an alternative to Claude SDK for code comprehension tasks where
full LLM reasoning is overkill. Worth watching for release and
evaluating when available.

**Why V5:** Not yet released. Comes from the same research group as
nanobot (HKUDS). If it ships and works well, could slot into the
cost-conscious engine selection table as a cheaper option for code
analysis tasks.

---

## AWS Capability Target

**Source:** User requirement
**Target:** V4+ tool integration

Genesis should be able to BUILD things on AWS on the user's behalf.
This is a tool integration target, not an architectural change:
- `aws-cli` as an Agent Zero tool
- CloudFormation / CDK template generation via claude_code
- Possible `aws-mcp` server wrapping common AWS operations

Place in V4 as an MCP server or tool integration item once the
Capability Expansion Pipeline is operational and can evaluate whether
the user's AWS usage patterns justify the integration investment.

### P-Tuning / Soft Prompt Prefixes

**Source:** NVIDIA — P-Tuning research
**Target:** Reflection Engine + meta-learning system

Instead of adjusting prompt TEXT to encode Genesis's learned behavioral patterns,
train task-specific virtual token prefixes ("soft prompts") that capture learned
behavior in a compressed, transferable form. A small trainable model generates
virtual tokens that prepend to Genesis's prompts, encoding patterns like "how this
user prefers information presented" or "what level of detail this task type needs."

**Why V5:** Requires fine-tuning infrastructure (not in V3/V4 scope). Also requires
sufficient operational data to train meaningful soft prompts — the training signal
is "which prompts produced outputs the user acted on?" which needs months of
engagement data. The virtual tokens would need to be validated against V4's
meta-prompting to ensure they don't conflict.

**Key advantage:** Soft prompts are model-agnostic at the embedding level — they
can potentially transfer across provider switches (aligns with GEA's
transferability finding above).

**Prerequisite:** Meta-prompting (V4) operational, engagement tracking mature,
fine-tuning compute available.

---

## Sources

- [DSPy — Stanford NLP](https://github.com/stanfordnlp/dspy)
- [R2R / SciPhi-AI](https://github.com/SciPhi-AI/R2R)
- [GEA Paper — VentureBeat](https://venturebeat.com/ai/new-agent-framework-matches-human-engineered-ai-systems/)
- [Cole Medin — Second Brain Skills](https://github.com/coleam00/second-brain-skills)
- [Vin's Obsidian Workflows](https://ccforeveryone.com/mini-lessons/vin-obsidian-workflows)
- [Kane AI](https://www.lambdatest.com/kane-ai)
- [Brad Bonanno's Second Brain](https://okhlopkov.com/second-brain-obsidian-claude-code/)
- [HKUDS/FastCode](https://github.com/HKUDS/FastCode)
- [NVIDIA NeMo Agent Toolkit](https://docs.nvidia.com/nemo/agent-toolkit/latest/index.html)
- [NVIDIA — LLM Prompt Engineering and P-Tuning](https://developer.nvidia.com/blog/an-introduction-to-large-language-models-prompt-engineering-and-p-tuning/)
