# Evaluation Action Items — 2026-03-27

Living document capturing action items from technology evaluation batch
(hallucination mitigation, SpecEyes speculative execution, MolmoWeb visual
web agent, generalist trust-layer thesis). Items are added during evaluation,
then planned and executed separately.

**Sources evaluated:**

*Batch 1:*
1. [5 Practical Techniques to Detect and Mitigate LLM Hallucinations](https://machinelearningmastery.com/5-practical-techniques-to-detect-and-mitigate-llm-hallucinations-beyond-prompt-engineering/) — Machine Learning Mastery
2. [SpecEyes: Accelerating Agentic MLLMs via Speculative Perception and Planning](https://huggingface.co/papers/2603.23483) — arxiv 2603.23483
3. [AI2 Releases MolmoWeb](https://venturebeat.com/data/ai2-releases-molmoweb-an-open-weight-visual-web-agent-with-30k-human-task/) — VentureBeat
4. [The Generalist in the Vibe Work Era](https://venturebeat.com/technology/you-thought-the-generalist-was-dead-in-the-vibe-work-era-theyre-more/) — VentureBeat

*Batch 2:*
5. Agentic RAG (general concept research) — multiple sources
6. [GitAgent: Docker for AI Agents](https://www.marktechpost.com/2026/03/22/meet-gitagent-the-docker-for-ai-agents-that-is-finally-solving-the-fragmentation-between-langchain-autogen-and-claude-code/) — MarkTechPost
7. [ClawTeam Multi-Agent Swarm Orchestration](https://www.marktechpost.com/2026/03/20/a-coding-implementation-showcasing-clawteams-multi-agent-swarm-orchestration-with-openai-function-calling/) — MarkTechPost
8. [Uncertainty-Aware LLM System](https://www.marktechpost.com/2026/03/21/a-coding-implementation-to-build-an-uncertainty-aware-llm-system-with-confidence-estimation-self-evaluation-and-automatic-web-research/) — MarkTechPost

*Batch 3:*
10. [claude-code-best-practice](https://github.com/shanraisshan/claude-code-best-practice) — CC operational patterns catalog (settings, hooks, workflows)
11. [IdeaBlast](https://ideablast.app/) — Local-first MCP-integrated brainstorming (MCP relay pattern)
12. [get-shit-done (GSD)](https://github.com/gsd-build/get-shit-done) — Autonomous execution loop, wave parallelism, context monitoring
9. [NVIDIA Nemotron-Cascade 2](https://www.marktechpost.com/2026/03/20/nvidia-releases-nemotron-cascade-2-an-open-30b-moe-with-3b-active-parameters-delivering-better-reasoning-and-strong-agentic-capabilities/) — MarkTechPost

*Batch 4:*
13. [7 Steps to Mastering Memory in Agentic AI Systems](https://machinelearningmastery.com/7-steps-to-mastering-memory-in-agentic-ai-systems/) — Machine Learning Mastery (memory architecture reference)
14. [How to Use MCP to Build a Personal Financial Assistant](https://www.freecodecamp.org/news/how-to-use-mcp-to-build-a-personal-financial-assistant/) — FreeCodeCamp (narrator pattern, MCP-as-data-layer, capability benchmark)
15. [What To Vibe Code First](https://www.forbes.com/sites/jodiecook/2026/03/24/what-to-vibe-code-first-to-buy-back-hours-every-week/) — Forbes (GTM positioning reference only)

---

## V3 Scope — Do Now

### V3-1: Wire Confidence Scores to Observation Gating
**Source:** Hallucination article (Technique 4) + cross-source synthesis
**What:** Genesis generates confidence scores in reflection outputs,
perception outputs, and observation types — but nothing reads them. They're
decoration. Confidence must become load-bearing:

1. **Observation write gate** — before persisting an observation to the DB,
   check its confidence score. Below a configurable threshold (start at 0.5),
   log it but don't persist to Qdrant. This directly addresses the 308 obs/24h
   backlog by filtering low-signal noise at write time.
2. **Reflection output gate** — deep reflection outputs with confidence < 0.6
   get flagged for user review rather than auto-applied. Light reflection
   outputs already have confidence but it's ignored in `output_router.py`.
3. **Perception field gate** — user model updates from perception include
   per-field confidence. Low-confidence fields should be treated as
   provisional, not authoritative. Add a "provisional" flag to USER.md
   updates that are below threshold.

**Key design question:** Where do thresholds live? Options:
- Hardcoded constants (simple, rigid)
- `settings` table in DB (configurable via MCP, preferred)
- Per-subsystem config (most flexible, most complex)

**Start with:** Settings table, single global confidence floor (0.5),
per-subsystem overrides later if needed.

**Implementation touches:**
- `src/genesis/perception/parser.py` — add threshold check after parsing
- `src/genesis/reflection/output_router.py` — add confidence gate
- `src/genesis/db/crud/observations.py` — add write-time confidence filter
- New: `src/genesis/config/confidence.py` — threshold config + settings read

**Priority:** High — addresses observation backlog AND hallucination risk
simultaneously. Single change, compound benefit.
**Dependencies:** None
**Scope:** V3

---

### V3-2: Self-Consistency Check for Autonomous Session Outputs
**Source:** Hallucination article (Technique 2)
**What:** Genesis dispatches background CC sessions for reflection, surplus
work, and inbox processing. These sessions generate outputs that are ingested
without cross-validation. A lightweight self-consistency check:

1. For high-stakes autonomous outputs (deep reflection conclusions, user
   model updates, procedure proposals), run the same prompt through 2
   providers via the existing router fallback chain.
2. Compare outputs. If they agree on key claims, proceed. If they diverge
   on material facts, flag for review rather than auto-ingesting.
3. "Material facts" = structured fields (confidence, recommendations,
   proposed changes). Not free-text comparison — compare the JSON fields.

**Not for:** Low-stakes outputs (light reflection, routine observations,
heartbeats). The cost/latency overhead isn't worth it for routine signals.

**Implementation:** Add a `verify_consistency` option to the CC invoker or
to the reflection dispatch. When enabled, runs a second inference call and
compares structured fields. Disagreement → observation with type
`consistency_divergence` for user review.

**Priority:** Medium — defense-in-depth for memory quality
**Dependencies:** Router must support explicit "use a different provider"
for the verification call (already possible via provider pinning)
**Scope:** V3

---

### V3-3: Answer Separability Concept for Confidence Calibration
**Source:** SpecEyes paper (cognitive gating via answer separability)
**What:** Even without logit access, the *concept* of separability —
measuring how distinct the top answer is from alternatives — can improve
Genesis's confidence calibration. Implementation path:

1. **Self-reported separability prompt.** When asking for confidence, also
   ask: "What is the second most likely answer? How different is it from
   your primary answer?" If the model reports the alternatives are very
   close, that's low separability → lower effective confidence, regardless
   of the self-reported number.
2. **Track separability vs. outcome.** Log (reported_confidence,
   reported_separability, actual_outcome) tuples. Over time, learn which
   confidence-separability combinations are reliable. This feeds into V3-1
   threshold tuning.
3. **Start with reflection only.** Deep reflection already produces
   structured JSON. Add `alternative_assessment: str` and
   `separability_estimate: float` fields. Light reflection doesn't need
   this — it's already fast/cheap.

**Why this matters now:** The SpecEyes paper proves that separability is a
better discriminator than raw confidence (Figure 3: sharp bimodal separation
vs. overlapping distributions). We can't compute it from logits, but we can
approximate it through structured prompting. This makes V3-1's thresholds
more meaningful.

**Priority:** Medium — calibration improvement, feeds V3-1
**Dependencies:** V3-1 (thresholds exist to tune)
**Scope:** V3

---

### V3-4: Speculative Routing for Batch Operations
**Source:** SpecEyes paper (heterogeneous parallel funnel)
**What:** The speculative execution pattern — try cheap first, escalate on
low confidence — applies directly to Genesis's batch operations:

1. **Observation triage** — currently uses a single model. Try the cheapest
   model in the router first. If its triage confidence is high (>0.8),
   accept. If low, escalate to a more capable model. Most observations are
   routine — the cheap model handles them fine.
2. **Surplus task screening** — before dispatching an expensive CC session
   for a surplus task, have a cheap model assess whether the task is worth
   doing. Low-value tasks get filtered without burning Opus tokens.
3. **Procedure matching** — before full LLM-based procedure recall, try
   FTS5 keyword matching first. Only escalate to embedding search + LLM
   reranking if FTS5 doesn't produce a confident match.

**Pattern:** This is the SpecEyes funnel applied to text:
```
Batch of N items
  → Cheap model screens all N (fast, parallel)
  → High-confidence items accepted (cost: cheap × N)
  → Low-confidence residual set escalated (cost: expensive × R)
  → Total cost: cheap×N + expensive×R where R << N
```

**Priority:** Medium — cost efficiency + quality improvement
**Dependencies:** Router needs a "try cheap, escalate" mode (may exist via
fallback chain, needs verification)
**Scope:** V3

---

### V3-5: MolmoWeb Integration — Visual Web Agent Capability
**Source:** MolmoWeb article + architecture exploration
**What:** Add visual web agent capability to Genesis's browser-use stack.
MolmoWeb (4B, Apache 2.0) operates from screenshots — browser-agnostic,
no DOM parsing. Integration with existing Playwright MCP creates a visual
perception layer on top of mechanical browser control.

**Architecture:**
```
User request (or autonomous task)
  → Playwright: browser_navigate(url)
  → Playwright: browser_take_screenshot()
  → MolmoWeb: analyze screenshot + task instruction
  → MolmoWeb returns: (reasoning, next_action, coordinates)
  → Playwright: execute action (click, type, scroll at coordinates)
  → Loop until task complete or max steps reached
```

**Integration surface (all exist in Genesis):**
- `ToolProvider` protocol → new `VisualWebAgentAdapter`
- `ProviderRegistry` → register with content_type `interactive_page`
- `CONTENT_TYPE_ROUTING` → add `"interactive_page": ["visual_web_agent", "playwright"]`
- `.mcp.json` → optional MCP server wrapper for CC session access
- `KNOWN_TOOLS` → register in tool discovery
- Browser skill → update three-layer architecture doc

**Deployment options (ranked by practicality):**
1. **Self-hosted FastAPI endpoint** — run MolmoWeb-4B on a GPU machine
   (user's LM Studio machine or a cloud GPU). Genesis calls the endpoint.
   Most control, most setup.
2. **Modal serverless** — deploy MolmoWeb-4B on Modal. Pay-per-use, no
   persistent infrastructure. Cold start ~30s, warm inference fast.
3. **HuggingFace Inference Endpoint** — dedicated, always-on. Monthly cost
   but zero ops.
4. **User's local machine via LM Studio** — requires GGUF export (not
   available yet). Monitor for community quantizations.

**Blocking question:** GPU access. The 4B model needs ~8GB VRAM. Options:
- User's existing hardware (when available)
- Cloud GPU spot instance (~$0.20/hr for adequate GPU)
- Modal serverless (pay-per-inference, no idle cost)

**Not blocking:** Architecture, integration code, Playwright coordination —
all infrastructure exists. The code can be written now with a stubbed
inference endpoint, then connected when GPU access is resolved.

**Priority:** High — significant capability expansion
**Dependencies:** GPU inference endpoint (any of the 4 options above)
**Scope:** V3 (integration code), V3/V4 boundary (deployment)

---

### V3-6: User Journey Design for Public Launch
**Source:** Generalist article (three-stage progression) + going-public timing
**What:** Genesis going public requires an explicit user onboarding journey.
The article's three stages map to Genesis's autonomy model:

**Stage 1: Optimism (new user)**
- Genesis operates at L1-L2 autonomy (inform, suggest)
- All actions visible, nothing autonomous
- User sees Genesis's confidence scores, reasoning traces
- Goal: demonstrate competence, build trust

**Stage 2: Doubt (edge cases found)**
- User discovers Genesis's limitations
- Confidence framework becomes visible ("I'm 60% sure, here's why")
- User learns to calibrate trust
- Autonomy stays low until user explicitly grants more

**Stage 3: Mental Model (graduated trust)**
- User understands Genesis's capabilities and blind spots
- Autonomy escalates to L3-L4 for proven domains
- Genesis proactively surfaces its uncertainty
- User delegates more, verifies less

**Implementation needs:**
1. **Onboarding mode** in autonomy manager — new users start at L1, with
   explicit prompts explaining what Genesis can/can't do autonomously
2. **Trust dashboard** — visible confidence scores, action history, success
   rates. "Here's what I did, here's how confident I was, here's what
   happened." Transparency builds trust faster than capability.
3. **Graduated unlock UX** — when Genesis consistently succeeds at L2 tasks,
   surface the option to grant L3. Don't nag — present the evidence and
   let the user decide.
4. **Failure transparency** — when Genesis is wrong, surface it prominently.
   "I was 80% confident and I was wrong. Here's what I learned." This
   builds more trust than hiding failures.

**Design doc needed:** User journey from first interaction to full autonomy.
Map each stage to: autonomy level, visible UI elements, confidence
presentation, escalation behavior, and unlock criteria.

**Priority:** High — Genesis is going public. First impressions matter.
**Dependencies:** Confidence wiring (V3-1) must work for this to be real
**Scope:** V3 (design), V4 (implementation of onboarding flow)

---

### V3-7: Agentic RAG — Iterative Retrieval Loop for Memory
**Source:** Agentic RAG research + F-7 (from 2026-03-17 doc)
**What:** Genesis retrieves once and accepts the result. Agentic RAG adds
the missing loop: retrieve → evaluate quality → reformulate → re-retrieve.

**Decision (2026-03-27):** Layered approach after evaluating CrewAI, Haystack,
RAGLight, DSPy, and build-it-ourselves options.

**Layer 1 (immediate):** Build the retrieval loop ourselves (~200 LOC in
`agentic_recall.py`). Uses our existing `HybridRetriever` + Qdrant MMR.
Steal grading/reformulation prompts from LangGraph tutorial. Measure quality
with RAGAS (`pip install ragas`). This ships in 1-2 sessions.

**Layer 2 (surplus compute, weeks later):** DSPy optimizes the retrieval
prompts automatically. Wrap our retrieval as a DSPy tool, define a
`GenesisRAG` module, run `BootstrapFewShot` optimizer as a monthly surplus
task. No cold-start problem — new users get Layer 1, DSPy kicks in when
there's enough data (~100+ memories). Pin `dspy` version, maintenance ~1hr/quarter.

**Why not frameworks:**
- CrewAI: Creates parallel Qdrant collections, duplicates Genesis agent arch
- Haystack: Can't use existing Qdrant collections without migration (feasible
  but unnecessary given our custom memory semantics — activation scoring,
  scope filtering, drive-weighted relevance). Revisit if Layer 1 proves unreliable.
- RAGLight: Uses LangChain transitively, single maintainer, manages own indexing
- LangGraph: Overkill for a 3-step loop, LangChain dependency conflicts with LiteLLM

**Implementation (Layer 1):**
1. Add MMR search to `qdrant/collections.py`
2. Build `memory/agentic_recall.py`: retrieve → grade → reformulate → re-retrieve
3. Fallback chain: Qdrant → FTS5 → web search → log knowledge_gap
4. Wire into perception and reflection recall paths
5. Measure with RAGAS context_precision metric

**Implementation (Layer 2):**
1. Wrap retrieval as `dspy.Tool`
2. Define `GenesisRAG(dspy.Module)` with ChainOfThought
3. Surplus task: `BootstrapFewShot` optimizer runs monthly
4. Serialized compiled module loaded on startup

**Priority:** High
**Dependencies:** V3-1 (confidence wiring) for quality thresholds
**Scope:** V3 (Layer 1), V3/V4 boundary (Layer 2)
**Cross-ref:** F-7 in `docs/plans/2026-03-17-evaluation-action-items.md`

---

### V3-8: Task Board for Inter-Session Coordination
**Source:** ClawTeam swarm orchestration
**What:** Genesis runs multiple CC sessions concurrently but they can't
coordinate. Add a task board backed by SQLite:

1. **New DB table: `task_board`** — columns: id, title, status (pending,
   blocked, in_progress, completed, failed), assigned_session, blocked_by
   (JSON array of task IDs), result (JSON), created_at, completed_at
2. **MCP tools** — `task_create`, `task_claim`, `task_complete`,
   `task_list`, `task_check_dependencies`. Exposed via genesis-health MCP
   or a new genesis-coordination MCP.
3. **Dependency resolution** — when a task completes, auto-unblock any
   tasks that were blocked_by it. Simple: check all blocked tasks, remove
   completed ID from their blocked_by array, transition to pending if
   array is empty.
4. **Leader/worker dispatch** — autonomy manager creates tasks on the board,
   dispatches CC sessions to claim and execute them. Sessions check the
   board via MCP, claim available tasks, report results.

**Not building:** Full ClawTeam messaging system. Start with the task board
only — it's the highest-value primitive. Messaging can come later via
observation writes (existing pattern).

**Priority:** Medium — enables multi-step autonomous workflows
**Dependencies:** None — DB and MCP infrastructure exist
**Scope:** V3

---

### V3-9: Nemotron-Cascade 2 as Router Provider
**Source:** NVIDIA Nemotron-Cascade 2 article
**What:** Add Nemotron-Cascade 2 as a provider in Genesis's router, if
available via any API provider. The model's thinking/non-thinking toggle
maps to our speculative routing pattern:

1. **Check availability** — scan OpenRouter, DeepInfra, Together AI,
   Fireworks for Nemotron-Cascade 2 hosting. Add to recon watchlist.
2. **If available:** register as a provider with two modes:
   - Non-thinking: cost_tier=CHEAP, for screening/triage tasks
   - Thinking: cost_tier=MODERATE, for reasoning tasks
3. **Router integration:** cheapest-first routing puts non-thinking mode
   at the front of the fallback chain for routine tasks.

**Priority:** Medium — contingent on API availability
**Dependencies:** Provider hosting (external)
**Scope:** V3 (if hosted), V4 (if self-hosted)

---

### V3-10: Context Window Monitoring
**Source:** GSD context-monitor hook + claude-code-best-practice
**What:** Genesis dispatches long-running CC sessions with no visibility into
context window utilization. Implement a bridge-file pattern: statusline hook
writes context usage metrics to a temp file, PostToolUse hook reads and injects
warnings at configurable thresholds (WARNING ≤35%, CRITICAL ≤25%). Enables
graceful handoff or compaction before context overflow.

**Priority:** High — prevents silent context overflow in autonomous sessions
**Dependencies:** Hook infrastructure (exists)
**Scope:** V3

---

### V3-11: Assumptions-Based Discussion Mode
**Source:** GSD autonomous loop (discuss phase)
**What:** Before ego or autonomous sessions execute complex plans, add a
structured "surface assumptions" step. The session reads codebase/state,
outputs assumptions as structured JSON with confidence tiers (CERTAIN >95%,
HIGH 80-95%, MEDIUM 60-80%, LOW <60%). Orchestrator or user can confirm,
deny, or modify before execution. In autonomous mode, assumptions proceed
without asking — verification catches errors.

**Priority:** High — reduces wasted execution on wrong assumptions
**Dependencies:** Confidence framework (V3-1, merged), ego sessions (Batches 1-5)
**Scope:** V3

---

### V3-12: LLM-Readable Task State Summary
**Source:** GSD file-based state + IdeaBlast structured output
**What:** Create a single MCP tool (`task_state_summary`) that returns a
compact JSON digest of current system state: active tasks, recent completions,
pending proposals, resource utilization. Currently requires multiple MCP calls.
Suitable for injection into autonomous session context without tool overhead.

**Priority:** Medium — reduces context waste on state reconstruction
**Dependencies:** Task system (exists), MCP infrastructure (exists)
**Scope:** V3

---

### V3-13: Checkpoint-as-Continuation-Prompt
**Source:** GSD checkpoint protocol (30KB reference)
**What:** When a CC session hits context limits or needs to hand off, serialize
current state as a continuation prompt: what was accomplished, what remains,
what context the next session needs. The checkpoint IS the handoff — a new
session can resume from it without manual reconstruction.

**Priority:** Medium — enables longer autonomous workflows
**Dependencies:** CC relay (exists), session dispatch (exists)
**Scope:** V3

---

### V3-14: Settings Audit Against Best-Practice Catalog
**Source:** claude-code-best-practice (60+ settings, 100+ env vars documented)
**What:** Diff shanraisshan's comprehensive CC settings catalog against our
`settings.json` and environment configuration. Identify: (a) useful settings
we're not using, (b) env vars that could improve performance or behavior,
(c) settings with suboptimal defaults. Produce inventory and apply quick wins.

**Priority:** Medium — free capability boost from settings we may be missing
**Dependencies:** None
**Scope:** V3

---

### V3-15: Memory Retrieval Test Suite
**Source:** MLM "7 Steps to Memory in Agentic AI" (Step 7)
**What:** Build a curated test suite of queries paired with expected memory
retrievals. Isolates memory layer problems from reasoning problems. When
agent behavior degrades, quickly identifies whether root cause is retrieval,
context injection, or model reasoning.

Include:
- Retrieval precision: are retrieved memories relevant?
- Retrieval recall: are important memories surfaced?
- Context utilization: are retrieved memories actually used?
- Memory staleness: how often does the agent rely on outdated facts?

**Priority:** High — we have zero retrieval quality testing today
**Dependencies:** memory_recall MCP (exists), Qdrant (exists)
**Scope:** V3

---

### V3-16: Recency-Weighted Scoring in memory_recall
**Source:** MLM "7 Steps to Memory in Agentic AI" (Step 5)
**What:** Our Qdrant semantic search returns by similarity alone. Add
recency weighting so recent relevant memories score higher than old
equally-relevant ones. The proactive hook does recency selection, but
the `memory_recall` MCP tool does not.

Implementation: combine cosine similarity score with exponential time
decay factor. Tunable decay half-life (default 7 days).

**Priority:** Medium — improves recall quality for active workflows
**Dependencies:** memory_recall MCP (exists)
**Scope:** V3

---

### V3-17: Memory Staleness Detection
**Source:** MLM "7 Steps to Memory in Agentic AI" (Step 7)
**What:** Track and surface stale memories — entries that are retrieved
frequently but reference outdated state. Add `last_verified` timestamp
to memory entries. Flag memories older than N days that are still being
retrieved. Dashboard metric for memory freshness distribution.

**Priority:** Medium — prevents silent degradation as memory grows
**Dependencies:** Qdrant metadata (exists), health dashboard (exists)
**Scope:** V3

---

### V3-18: Narrator Pattern for Autonomous Outputs
**Source:** FreeCodeCamp MCP Financial Assistant
**What:** Formalize the fact/narration separation for Genesis outputs.
Morning reports, ego proposals, observation summaries — all should
pre-compute metrics deterministically (Python), then pass a structured
facts object to the LLM with strict "narrate only these facts"
instructions. Reduces hallucination in user-facing autonomous messages.

Also represents a capability benchmark: Genesis should be able to build
and operate MCP-based data integrations like the financial assistant
autonomously. Any structured-data-to-narrative pipeline is within scope.

**Priority:** Low — improvement to existing functionality, not a gap
**Dependencies:** Outreach system (exists), ego sessions (Batches 1-5)
**Scope:** V3

---

## V4 Scope — Design Now, Build Later

### V4-1: Confidence-Aware Router Escalation
**Source:** SpecEyes paper + hallucination article
**What:** Router redesign where model selection is confidence-driven rather
than fixed-ordering. The speculative pattern from SpecEyes, adapted for
text-based API routing:

1. Route all queries to cheapest adequate model first
2. Model returns response + confidence signal
3. If confidence > threshold → accept (cheap cost)
4. If confidence < threshold → re-route to more capable model (expensive)
5. Track acceptance rates per query type to auto-tune thresholds

**Additional sophistication (from SpecEyes):**
- Where providers expose logprobs (OpenRouter does for some models),
  compute actual separability scores instead of relying on self-reported
  confidence
- Heterogeneous parallel processing for batch queries — cheap model handles
  the easy ones, expensive model handles the residual
- Tunable threshold as a "control knob" for the accuracy-cost Pareto front

**Design depends on:** V3-1 (confidence wiring), V3-3 (separability concept)
**Scope:** V4

---

### V4-2: Agentic Depth Tracking and Optimization
**Source:** SpecEyes paper (agentic depth formalization)
**What:** Track the number of sequential steps (tool calls, LLM invocations)
each autonomous session requires. Use this as an efficiency metric:

1. **Telemetry** — for every CC session dispatch, log: total tool calls,
   total LLM calls, task complexity classification, outcome quality
2. **Efficiency signal** — "agentic depth" per task type. If similar tasks
   consistently require 20+ tool calls, investigate whether the prompt,
   tools, or approach can be simplified
3. **Optimization target** — minimize agentic depth while maintaining
   outcome quality. The SpecEyes insight: many "deep" pipelines can be
   short-circuited when the system is confident enough to skip steps

**Scope:** V4

---

### V4-3: Visual Web Agent — Advanced Capabilities
**Source:** MolmoWeb article + F-13 (browser-use self-improving agents)
**What:** Beyond basic screenshot → action loops (V3-5), the visual agent
should learn:

1. **Procedural memory for web tasks** — when the visual agent successfully
   completes a multi-step web task, save the trajectory as a procedure.
   Next time a similar task appears, replay the procedure instead of
   re-inferring each step.
2. **Self-correction from visual feedback** — if an action produces an
   unexpected screenshot (e.g., error page, wrong element clicked),
   detect the deviation and recover.
3. **MolmoWebMix as training data** — the 30K human trajectory dataset
   could be used to fine-tune or evaluate Genesis's visual agent
   performance.

**Scope:** V4 (depends on V3-5 basic integration)

---

### V4-4: Multi-Sample Verification Pipeline
**Source:** Hallucination article (Technique 2) + V3-2 extension
**What:** Extend V3-2's consistency check into a full verification pipeline:

1. **Secondary model review** — after primary model generates, a different
   model reviews for factual consistency, unsupported claims, internal
   contradictions
2. **Cross-reference against memory** — check claims against Genesis's
   episodic and knowledge memory. If the model claims X and memory says
   not-X, flag the contradiction
3. **Citation verification** — when outputs reference specific facts,
   verify against the source material (if available in context)

**Scope:** V4 (requires robust memory retrieval — currently a gap)

---

### V4-5: Multi-Session Leader/Worker Orchestration
**Source:** ClawTeam swarm orchestration
**What:** Extend V3-8 (task board) into full leader/worker pattern:

1. **Leader session** — receives a complex goal, decomposes into subtasks
   with dependency chains, creates tasks on the board
2. **Worker sessions** — claim and execute individual tasks, report results
3. **Synthesis** — leader session watches for all subtasks to complete,
   then synthesizes a final result from worker outputs
4. **Team templates** — pre-configured decomposition patterns for common
   Genesis workflows (research task, codebase audit, multi-source evaluation)

**Scope:** V4 (depends on V3-8 task board)

---

### V4-6: GitAgent-Compatible Export for Public Repo
**Source:** GitAgent article
**What:** Make GENesis-AGI exportable in GitAgent format so other developers
can take Genesis's identity/skills/rules and run them in their preferred
framework (LangChain, CrewAI, etc.). Not for Genesis to run in those
frameworks — for Genesis's *design* to be portable.

**Scope:** V4 (public distribution play)

---

### V4-7: PR-Based Self-Modification Governance
**Source:** GitAgent article (git as supervision layer)
**What:** When Genesis wants to modify its own identity (SOUL.md),
rules (STEERING.md), or learned procedures, it creates a branch and PR
rather than modifying in place. User reviews and merges. Maps to L3/L4
governance — the PR IS the approval gate.

**Scope:** V4 (requires autonomy manager refinement)

---

### V4-8: Self-Hosted Inference Stack
**Source:** Nemotron-Cascade 2 + MolmoWeb + existing qwen3-embedding
**What:** Complete self-hosted inference independent of API providers:
- **Reasoning:** Nemotron-Cascade 2 (30B MoE, 3B active)
- **Vision:** MolmoWeb-4B (screenshot → action)
- **Embedding:** qwen3-embedding via Ollama (already in place)
- **Deployment:** FastAPI endpoints on GPU hardware

This is the infrastructure independence endgame. No API keys, no rate
limits, no provider outages. Requires GPU access.

**Scope:** V4 (depends on GPU infrastructure)

---

## Reference — Track but Don't Build

### REF-1: Visual Web Agent Space Monitoring
**Source:** MolmoWeb article
**What:** Track developments in open-weight visual web agents:
- MolmoWeb updates (model improvements, GGUF exports, API hosting)
- Competing open models (OpenAI Operator alternatives, Anthropic computer
  use open implementations)
- Browser-agent benchmarks (WebVoyager, Mind2Web, DeepShop)
- Hosted inference providers adding Molmo/MolmoWeb support

**Add to recon watchlist:** `allenai/molmoweb` GitHub repo,
`allenai/MolmoWeb-4B` on HuggingFace.

### REF-2: "Trust Layer" Positioning for Public Launch
**Source:** Generalist article
**What:** The article's thesis — AI empowers generalists who become the trust
layer between AI output and organizational standards — maps directly to
Genesis's public narrative. Genesis IS the AI-native trust layer: built-in
confidence framework, graduated autonomy, transparent reasoning.

Use this framing in:
- GENesis-AGI README
- Architecture overview for new users
- Any public-facing documentation

Key message: "Genesis doesn't ask you to trust it blindly. It shows you
its confidence, explains its reasoning, and earns autonomy through
demonstrated competence."

### REF-3: Separability Score as Confidence Research
**Source:** SpecEyes paper
**What:** Monitor which API providers expose logprobs. When logprob access
is available, separability scores are strictly better than self-reported
confidence for gating decisions. Currently:
- OpenRouter: some models expose logprobs
- Groq: limited logprob support
- DeepInfra: check availability
- Anthropic API: not available (as of knowledge cutoff)

Track this and update V4-1 design when the landscape shifts.

### REF-4: GitAgent Spec as Distribution Standard
**Source:** GitAgent article
**What:** Monitor the GitAgent spec evolution. If it gains traction as a
cross-framework standard, Genesis should be compatible. Track:
- `open-gitagent/gitagent` GitHub repo
- Framework adoption (Claude Code, LangChain, CrewAI exporters)
- Community adoption metrics

Genesis's structure already matches — the question is whether to formalize
the compatibility.

### REF-5: ClawTeam / Swarm Orchestration Patterns
**Source:** ClawTeam article
**What:** Monitor multi-agent swarm frameworks for patterns Genesis can
adopt. Key repos:
- `HKUDS/ClawTeam` — leader/worker with task board
- OpenAI Swarm (experimental) — lightweight agent handoffs
- LangChain Deep Agents — planning + memory + context isolation

The coordination primitives (task board, dependency resolution, inter-agent
messaging) are more valuable than any specific framework.

### REF-6: Nemotron-Cascade 2 Hosting Availability
**Source:** NVIDIA Nemotron-Cascade 2
**What:** Track which inference providers host Nemotron-Cascade 2:
- HuggingFace: `nvidia/nemotron-cascade-2` (weights available)
- OpenRouter, DeepInfra, Together AI, Fireworks: check for API hosting
- GGUF exports for local inference via LM Studio

When hosted, this becomes immediately actionable (V3-9).

### REF-7: Agentic RAG Reference Implementations
**Source:** Agentic RAG research
**What:** Key reference implementations for V3-7:
- [Vellum Agentic RAG guide](https://vellum.ai/blog/agentic-rag) — architecture patterns
- NVIDIA NeMo Retriever — iterative agent-retriever loop (from 2026-03-17 eval)
- Databricks KARL RAG agent — RL-based retrieval (from VentureBeat sidebar)

These inform the iterative retrieval loop design.

---

## Validation Notes (Batch 2 additions)

Items from this evaluation that **validate existing Genesis architecture**
(no action needed, confidence boost):

1. **Structured output enforcement** — Genesis's strict JSON schema
   validation on all LLM outputs is exactly Technique 3 (constrained
   generation). Already strong.
2. **Autonomy manager approval gates** — Genesis's graduated L1-L5
   governance with protected paths is a more mature implementation of
   "human-in-the-loop" (Technique 5) than anything in the article.
3. **Multi-provider fallback chain** — the router's existing fallback
   architecture is directionally correct for speculative execution.
   Needs confidence-awareness (V4-1) but the bones are right.
4. **Data-first philosophy** — MolmoWeb's positioning of the dataset as
   the moat (not the model) validates Genesis's emphasis on memory/data
   quality over model sophistication.
5. **Behavioral linter hooks** — the "never hide broken things" enforcement
   via PreToolUse hooks is exactly the guardrail pattern the generalist
   article says is missing from "vibe coding" approaches.
6. **SOUL.md + skills + rules + memory structure** — GitAgent independently
   arrived at the same canonical agent architecture Genesis uses. Strong
   signal that Genesis is ahead of the standardization curve.
7. **Uncertainty-aware pipeline** — the three-stage pattern (generate +
   confidence → self-evaluate → auto-research) validates V3-1/V3-2/V3-3
   design from Batch 1. Independent confirmation from a different source.
8. **Multi-provider routing** — Nemotron-Cascade 2's thinking/non-thinking
   toggle validates the speculative routing concept (V3-4). The tiered
   stack (screening → reasoning → frontier) is becoming a standard pattern.

---

## Cross-References

- Previous evaluation action items: `docs/plans/2026-03-17-evaluation-action-items.md`
- Confidence framework: CLAUDE.md § Confidence Framework
- Router architecture: `docs/plans/2026-03-04-phase2-compute-routing-design.md`
- Autonomy design: `docs/architecture/genesis-v3-autonomous-behavior-design.md`
- Observation backlog: memory file `project_reflection_engine_gaps.md`
- V4 specs: `docs/plans/v4-*.md`
