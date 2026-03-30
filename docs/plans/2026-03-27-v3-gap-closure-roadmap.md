# Phase 1: Make the Brain Real — Detailed Implementation Plan

**Parent roadmap:** `docs/plans/2026-03-27-v3-gap-closure-roadmap.md`

## What This Phase Does

Wire confidence scores into Genesis as load-bearing infrastructure. Today,
confidence exists in output schemas but nothing reads it. After this phase,
confidence gates observation writes, reflection routing, and memory upsertion.

## Due Diligence Findings (2026-03-27)

Audited actual confidence/salience values in production data BEFORE implementing
gates. Critical findings that reshape the implementation order:

**Observation confidence (SQLite, last 200):**
- Range: 0.70–0.95. Mean 0.85. **Nothing below 0.7.**
- LLM never reports low confidence — everything is 0.7+ due to overconfidence
  and defaults. A simple threshold gate would filter nothing.

**Qdrant confidence (episodic_memory, 200 points):**
- Bimodal: 56% are exactly 0.5 (default param), rest cluster at 0.8–1.0.
- The 0.5 values aren't "low confidence" — they're "no confidence was set."
- A 0.5 threshold would incorrectly filter 56% of legitimate memories.

**Salience (last 300 observations):**
- Range: 0.4–0.85. **Nothing below 0.4.** Current 0.1 gate has never fired.

**Observation volume:** `user_model_delta` dominates at 721 — this is the backlog.

**Implications:**
1. Gates without better prompting are theater — the LLM must be prompted to
   report honest confidence AND we need separability to distinguish "genuinely
   0.85" from "defaulting because uncertain"
2. Must distinguish "default 0.5" from "deliberately low" in Qdrant — use
   `None` when caller doesn't set confidence, only gate on explicit values
3. Separability prompting must come BEFORE gating to produce real signal

## Implementation Steps (Revised Order)

**NOTE:** Main branch has received significant new changes to the memory system
(decay, dedup, and related). Steps below need revalidation against the new code
before implementation begins. File paths, line numbers, and function signatures
may have changed.

### Step 1: Settings Infrastructure (unchanged)

Register `confidence_gates` domain in existing settings.

**Files:**
- `src/genesis/mcp/health/settings.py` — add domain + validator
- `config/confidence_gates.yaml` (NEW) — default thresholds

### Step 2: Confidence Helper Module (unchanged)

**Files:**
- `src/genesis/config/confidence.py` (NEW ~60 LOC)

### Step 3: Separability Fields + Prompt Changes (MOVED UP from Step 6)

**Why first:** Without this, confidence values are meaningless for gating.
The LLM must be prompted to report honest confidence AND separability.

**Files:**
- `src/genesis/reflection/types.py` — extend `DeepReflectionOutput`
- `src/genesis/identity/REFLECTION_DEEP.md` — extend prompt
- `src/genesis/reflection/output_router.py` — parse new fields

**Work:**
1. Add `alternative_assessment`, `separability_estimate` to dataclass
2. Add separability prompt: "What is the second most likely assessment?
   Rate separability 0.0-1.0."
3. **Also:** strengthen confidence calibration prompt — explicitly instruct
   the LLM: "Report confidence below 0.5 when you are uncertain or when
   your assessment relies on incomplete data. Do not default to 0.7."

### Step 4: Gate Observation Writes (REVALIDATE against new memory code)

**Files:**
- `src/genesis/perception/writer.py` — needs revalidation
- May interact with new decay/dedup changes on main

### Step 5: Gate Deep Reflection Routing (REVALIDATE)

**Files:**
- `src/genesis/reflection/output_router.py` — needs revalidation

**Critical:** Gate BEFORE any writes, not between them.

### Step 6: Gate Memory Upsertion (REVALIDATE against new memory code)

**Files:**
- `src/genesis/memory/store.py` — needs revalidation
- **Key fix:** Distinguish `confidence=None` (caller didn't set) from
  `confidence=0.3` (explicitly low). Only gate on explicit values.

### Step 7: Self-Consistency Module (unchanged)

**Files:**
- `src/genesis/cc/consistency.py` (NEW ~100 LOC)
- Only for deep reflection outputs.

---

## BEFORE EXECUTING: Revalidation Required

Main branch has received new memory system changes (decay, dedup, etc.).
Before implementing Steps 4-6, must:

1. Read all changed files on main (especially memory/, perception/, reflection/)
2. Verify file paths, line numbers, function signatures are still correct
3. Check for new confidence-related code that may overlap or conflict
4. Update step details with current line numbers and integration points

---

## File Summary

| File | Change | LOC |
|------|--------|-----|
| `mcp/health/settings.py` | Add domain + validator | +30 |
| `config/confidence_gates.yaml` | New config file | +20 |
| `config/confidence.py` | New helper module | +60 |
| `perception/writer.py` | Refactor salience gate, add confidence gate | +15 |
| `reflection/output_router.py` | Add gate in route(), parse separability | +25 |
| `reflection/types.py` | Add 2 fields to DeepReflectionOutput | +3 |
| `identity/REFLECTION_DEEP.md` | Add separability prompt section | +10 |
| `memory/store.py` | Add confidence gate before embedding | +20 |
| `cc/consistency.py` | New consistency check module | +100 |
| **Total new/modified** | | **~283** |

## Testing

- ~30 new tests across confidence gating, separability parsing, consistency
- Integration test: observation with confidence 0.3 → verify SQLite only, no Qdrant
- Integration test: observation with confidence 0.8 → verify both
- Integration test: deep reflection with confidence 0.2 → verify quarantine
- Unit test: separability fields parse correctly with defaults for missing
- Unit test: consistency module detects divergence between two provider outputs
- Existing tests must still pass (no regressions)

## Verification

1. `ruff check . && pytest -v` — all pass
2. Manual: trigger a low-confidence reflection, verify it's quarantined
3. Manual: check Qdrant point count before/after — low-confidence memories absent
4. Check settings MCP: `settings_get confidence_gates` returns config

---

# Overarching Roadmap (Reference)

## Context

Evaluated 9 external sources covering agentic RAG, hallucination mitigation,
speculative execution, visual web agents, multi-agent orchestration, confidence
systems, and the generalist trust-layer thesis. This plan closes the gaps those
evaluations exposed before Genesis enters V4.

Full evaluation document: `docs/plans/2026-03-27-evaluation-action-items.md`

**Out of scope (handled elsewhere or dropped):**
- Guardian merge, USER.md bugs, tool_bootstrap.py, cognitive state (separate sessions)
- AgentMail (dropped), session bookmarks / memory-photographic (own workstreams)
- Observation backlog / reflection heartbeat (likely already addressed)
- PR-based self-modification governance (deferred to V4)

---

## Phase 1: Make the Brain Real

Confidence wiring unlocks everything downstream — agentic RAG quality
evaluation, speculative routing, observation gating, user-facing trust signals.

### 1a. Confidence Wiring (V3-1)

**Files:**
- `src/genesis/config/confidence.py` (NEW ~80 LOC) — threshold config via settings
- `src/genesis/reflection/output_router.py` (MODIFY) — gate before routing
- `src/genesis/perception/parser.py` (MODIFY) — threshold check after parsing
- `src/genesis/db/crud/observations.py` (MODIFY) — write-time confidence filter

**Work:**
1. Register confidence domain in existing settings infra (`mcp/health/settings.py`)
2. Gate reflection outputs: below threshold → flag for review, don't auto-apply
3. Gate observation writes: below threshold → SQLite only, skip Qdrant
4. Follow salience gate pattern already in `perception/writer.py:74`

**Verify:** Observation with confidence 0.3 → SQLite only. 0.8 → both.

### 1b. Separability Concept (V3-3)

**Files:**
- `src/genesis/identity/REFLECTION_DEEP.md` (MODIFY) — add separability prompt
- `src/genesis/reflection/types.py` (MODIFY) — add fields
- `src/genesis/perception/parser.py` (MODIFY) — validate new fields

### 1c. Self-Consistency for Autonomous Outputs (V3-2)

**Files:**
- `src/genesis/cc/consistency.py` (NEW ~100 LOC)
- Wire into reflection dispatch for deep reflection outputs

---

## Phase 2: Agentic RAG

Depends on Phase 1 (confidence thresholds for quality evaluation).

**Decision (2026-03-27):** Layered approach. Layer 1: build loop ourselves +
RAGAS. Layer 2: DSPy optimization as surplus compute (Phase 5c).

### 2a. Qdrant MMR + Hybrid Search Upgrade

**Files:**
- `src/genesis/qdrant/collections.py` (MODIFY) — add `search_mmr()`
- `src/genesis/memory/retrieval.py` (MODIFY) — add `mode="hybrid_mmr"`

### 2b. Build the Retrieval Loop

**Files:**
- `src/genesis/memory/agentic_recall.py` (NEW ~200 LOC)
- `src/genesis/memory/retrieval_quality.py` (NEW ~80 LOC)

**Work:**
1. `agentic_recall(query, max_iterations=3)`:
   retrieve → grade_quality → if low: reformulate → re-retrieve
2. Fallback: Qdrant → FTS5 → web search → log knowledge_gap
3. Steal grading/reformulation prompts from LangGraph tutorial
4. RAGAS for offline quality measurement

### 2c. Wire Into Call Sites
Swap single-pass recall → agentic recall at perception + deep reflection paths.

**Verify:** Poor first-pass query → reformulation → re-retrieval improves results.

---

## Phase 3: Make Autonomy Work

Task system is ~80% built. Gap is connecting existing infrastructure.

**Existing (DO NOT REBUILD):** `task_states` CRUD (0 callers), `message_queue`
(partial), `CheckpointManager` (wired, 0 callers), `SurplusScheduler/Queue`
(active), `CCInvoker` (capable), unused call sites: `27_pre_execution_assessment`,
`31_outcome_classification`, `32_delta_assessment`.

### 3a. Real Executor — CCSessionExecutor
**Files:** `src/genesis/surplus/executor.py`, `runtime/init/surplus.py` (MODIFY)

### 3b. Task Decomposition
**Files:** `src/genesis/surplus/task_decomposer.py` (NEW ~150 LOC)
Uses call site `27_pre_execution_assessment`.

### 3c. Dependency Resolution
**Files:** `src/genesis/db/crud/task_states.py` (MODIFY)

### 3d. Wire CheckpointManager + Speculative Routing
Wire checkpoint into execution. Add `speculative_route()` to router (~60 LOC).

**Verify:** Compound goal → decompose → workers → deps resolve → synthesize.

---

## Phase 4: Go Public

### 4a. Onboarding Experience
Interactive post-install CLI wizard + tutorial. NOT a design doc.
**Files:** `scripts/post_install_setup.py` (NEW or extend), README in public repo

### 4b. Trust Layer Framing
GENesis-AGI README: "Genesis shows its confidence, explains its reasoning,
earns autonomy through demonstrated competence."

### 4c. GitAgent Structure Compatibility
Audit spec, add manifest if needed. ~90% already matches.

---

## Phase 5: New Capabilities

### 5a. Visual Web Agent Adapter (MolmoWeb)
**Files:**
- `src/genesis/providers/visual_agent.py` (NEW ~120 LOC)
- `runtime/init/providers.py`, `learning/tool_discovery.py` (MODIFY)
- `config/model_routing.yaml`, `config/model_profiles.yaml` (MODIFY)

### 5b. Nemotron-Cascade 2 Config
YAML-only. Provider entries + model profile. Contingent on API hosting.

### 5c. DSPy Integration (Agentic RAG Layer 2)
Background optimization as surplus compute task.
**Files:** `src/genesis/surplus/dspy_optimizer.py` (NEW ~150 LOC)
Cold-start: does nothing until ~100+ memories. Layer 1 handles everything before.

---

## V4 — Deferred

| Item | Reason |
|------|--------|
| PR-based self-modification | User explicitly deferred |
| Full confidence-aware router redesign | Depends on V3 proving out |
| Agentic depth tracking | Optimization, not capability |
| Full leader/worker orchestration | V3 gets basics, V4 gets templates |
| Multi-sample verification pipeline | Depends on robust memory retrieval |
| GitAgent export mechanism | Distribution play |
| Wave execution pattern | Needs V3 task system stable first |
| Structured plan format with dependencies | Needs V3 plan primitives |
| User profiling dimensions | Needs V3 perception stable first |
| Nyquist-style test gap detection | Needs V3 test coverage baseline |
| Autonomous execution loop structure | Needs ego Batch 6 + V3 autonomy |

### V4 Detail: Evaluation Batch 3 Items (2026-03-29)

**Wave Execution (from GSD):** Execute plan steps in dependency-ordered waves.
Steps with no unresolved dependencies run in parallel via subagent dispatch.
Each wave completes before the next starts. Requires structured plan format
(below) as prerequisite. Maps to leader/worker pattern but adds explicit wave
boundaries and progress tracking.

**Structured Plan Format (from GSD):** Plans should include machine-readable
dependency edges between steps — not just ordered lists. Enables dependency
resolution, parallel execution of independent steps, and progress tracking.
Format TBD (YAML frontmatter per task vs JSON plan spec).

**User Profiling Dimensions (from GSD):** Extend USER.md with structured
dimensions: communication preferences, domain expertise areas, trust calibration
per category, active hours, approval patterns. 8-dimension model from GSD:
communication style, decision speed, explanation depth, debugging approach,
UX philosophy, vendor philosophy, frustration triggers, learning style.

**Nyquist Test Gap Detection (from GSD/best-practice):** If a module changes
N times per week, it needs proportional test touch-points. Identify under-tested
high-churn modules and prioritize test investment. Auto-generate tests for
gaps where coverage is below the Nyquist threshold.

**Autonomous Execution Loop (from GSD):** Formalize discover → discuss → plan →
execute → verify → gap-close as a reusable execution primitive. Each phase has
entry/exit criteria, deviation handling (4 levels: cosmetic/minor/significant/
critical), and state serialization. Subsumes current ad-hoc session dispatch.

---

## Verification

After each phase:
1. `ruff check .` + `pytest -v`
2. `/review` + `superpowers:code-reviewer`
3. Commit after each logical unit
4. End-to-end verification, not just unit tests

**Phase 1:** Observation confidence 0.3 → SQLite only. 0.8 → both.
**Phase 2:** Poor query → reformulate → re-retrieve → improved results.
**Phase 3:** Compound goal → decompose → dispatch → deps → synthesize.
**Phase 4:** Post-install wizard produces working secrets.env in clean env.
**Phase 5:** Mock MolmoWeb endpoint → adapter returns structured action.
