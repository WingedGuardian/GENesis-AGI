# V4 Feature Spec: Meta-Prompting Protocol (Pattern 2)

**Status:** DESIGNED — repositioned within GWT architecture. Requires 1–2 months
V3 operational data for DSPy optimization corpus.
**Dependency:** Phase 7 (complete), V4 Strategic Reflection
**V3 Groundwork:** Call site #18 (model routing registry), static prompt templates
(6 micro + 6 light + deep + strategic), PromptBuilder with round-robin/focus
selection, CAPS markdown convention, reflection quality tracking in
Self-Learning Loop.
**GWT Integration:** Meta-prompting applies to the PERCEIVE step (V5) and
SELECT step (V5) of the LIDA cognitive cycle. V4 uses static prompts; V5
replaces with 3-step meta-prompted protocol. See
`docs/architecture/genesis-v4-architecture.md` §8.

---

## What This Is

Meta-prompting replaces V3's static monolithic prompts with a 3-step protocol
that uses a cheap model to determine WHAT to think about, a capable model to
DO the thinking, and a synthesis pass to find cross-cutting patterns. This
produces higher-quality reflections at potentially lower cost, because each
step's context is smaller and more focused.

From the autonomous behavior design (lines 2449–2451):

> Why the meta-prompter is the most critical call in the system: If the
> meta-prompter asks the wrong questions, the entire reflection is wasted
> regardless of how capable the answering model is. A brilliant answer to
> the wrong question is worthless.

## The 3-Step Protocol

From `docs/architecture/genesis-v3-autonomous-behavior-design.md` (lines 2419–2451):

### Step 1: Meta-Prompt (cheap model)

**Model:** DeepSeek V4 (~$0.14 blended) / Qwen 3.5 Plus / GPT-5 Nano (call site #18)
**Context:** ~4K tokens (full signal landscape from Awareness Loop)
**Cost:** ~100–500 tokens output

```
Input: Full signal landscape from Awareness Loop
Task: "Given these signals, what are the 3-5 most important
       questions this reflection should answer? Consider
       cross-cutting patterns across signals, not just
       individual items. What might connect seemingly
       unrelated signals?"
Output: 3-5 focused questions with relevant context scope
```

**Critical principle:** The meta-prompter should err toward breadth. One
unnecessary question is cheap (easily answered and discarded). One missed
question that mattered is expensive (entire reflection misses an insight).

### Step 2: Deep Reflection (capable model)

**Model:** Sonnet (Deep) or Opus (Strategic) — per existing call sites
**Context:** Each question + only its relevant context (from MCP queries)

```
Input: Each question + only its relevant context (from MCP)
Task: Answer each question with grounded evidence
Output: Observations, proposals, actions per question
```

Each question can be answered independently. This enables:
- Parallel execution (multiple CC background sessions)
- Focused context (smaller prompt per question = less positional bias)
- Natural cost scaling (3 questions = 3 calls, not 1 giant call)

### Step 3: Synthesis (capable model, fresh call)

**Model:** Same tier as Step 2 (Sonnet or Opus)
**Context:** ONLY the answers from Step 2 (not the reasoning)

```
Input: ONLY the answers from Step 2 (not the reasoning)
Task: "Do any of these answers interact? Are there patterns
       across them that the individual answers missed?"
Output: Cross-cutting insights, integrated observations
```

**Why a fresh call:** The synthesis model sees only conclusions, not the
reasoning chains. This prevents anchoring on Step 2's framing and enables
genuinely new connections.

### Cost Profile

Total cost is often LESS than a single monolithic prompt because each step's
context is smaller:

| Step | Model | Tokens (est.) | Cost (est.) |
|------|-------|---------------|-------------|
| Meta-prompt | DeepSeek V4 | ~4K in, ~500 out | ~$0.01 |
| Answers (×3-5) | Sonnet | ~2K in, ~1K out each | ~$0.05–0.08 |
| Synthesis | Sonnet | ~3K in, ~1K out | ~$0.02 |
| **Total** | | | **~$0.08–0.11** |

Compare: V3 monolithic Deep reflection with full context: ~$0.10–0.15.
Meta-prompting is cost-neutral or cheaper while producing better output.

## Current V3 State (Static Templates)

### PromptBuilder

From `src/genesis/perception/prompts.py` (lines 30–101):

**Micro templates** (round-robin rotation, tick_number % 3):
- `analyst` — analytical lens
- `contrarian` — challenge assumptions
- `curiosity` — explore unknowns

**Light templates** (focus-area based selection):
- `situation` — current state analysis
- `user_impact` — user-facing implications
- `anomaly` — unexpected patterns

Templates live in `src/genesis/perception/templates/` with CAPS markdown
overrides in `src/genesis/identity/` (e.g., `MICRO_TEMPLATE_ANALYST.md`).

**Deep/Strategic prompts** are single monolithic CAPS files:
- `REFLECTION_DEEP.md` — comprehensive with conditional job sections (Phase 7)
- `REFLECTION_STRATEGIC.md` — identity + strategic analysis
- `SELF_ASSESSMENT.md` — 6-dimension weekly assessment
- `QUALITY_CALIBRATION.md` — quality drift detection

### V3 Scope Boundary

From build phases (lines 1009–1010):

> No meta-prompting — uses a comprehensive static prompt. V4 replaces this
> with the 3-step meta-prompting protocol for higher quality.

### Prompt Variation (Pattern 5 Interaction)

From autonomous behavior design (line 2516):

> With meta-prompting applied to Deep/Strategic reflections, prompt variation
> is only needed for Micro and retrospectives where meta-prompting would be
> overkill. The meta-prompter provides natural variation for deeper reflections.

V4 meta-prompting subsumes the need for template rotation at Deep/Strategic
depths. Micro/Light continue using V3's rotation/focus-based selection.

## DSPy Optimization

From `docs/architecture/genesis-deferred-integrations.md` (lines 24–39):

**Target:** Reflection Engine prompt templates (all depths).

> Replace hand-tuned prompt templates with DSPy-optimized versions. DSPy
> treats prompts as programs with trainable parameters — it uses operational
> data (which reflections produced actionable outputs? which were noise?)
> to algorithmically optimize prompt structure, few-shot examples, and
> instruction phrasing.

**Why V4:** Requires a corpus of reflection inputs/outputs with quality labels.
V3 generates this corpus; V4 uses it.

**Prerequisite:** Reflection Engine must track which outputs were acted on vs
ignored (already designed in V3 Self-Learning Loop).

### What DSPy Optimizes

1. **Meta-prompt instructions** — how the cheap model is told to generate questions
2. **Few-shot examples** — which example questions produce the best reflections
3. **Synthesis instructions** — how to find cross-cutting patterns
4. **Chain-of-thought structure** — which CoT patterns (step-by-step, pros/cons,
   hypothesis-test) produce highest-quality reflections per depth

## Progressive Disclosure Optimization

From `docs/architecture/genesis-deferred-integrations.md` (lines 123–140):

**Concept:** Load inline capabilities (salience evaluation, social simulation,
governance check, drive weighting) on-demand per reflection instead of including
all in every prompt.

**Implementation:** The meta-prompter (Step 1) already decides what questions
to ask. It can also decide what tools/capabilities to provide for each question:

```
Step 1 output (enhanced):
  Question 1: "Is memory consolidation backlog growing?" → Load: memory CRUD
  Question 2: "Is outreach engagement declining?" → Load: engagement heuristics
  Question 3: "Are procedures staling?" → Load: decay metrics, quarantine stats
```

**Benefit:** Smaller prompts per question → less positional bias, lower cost,
and capabilities can be added without growing every prompt proportionally.

## Chain-of-Thought Optimization

From `docs/architecture/genesis-deferred-integrations.md` (lines 287–301):

V3 convention: Light+ depth prompts include explicit CoT scaffolding.
V4 optimizes further:

1. Analyze which CoT patterns produce highest-quality reflections per depth
2. Use DSPy to algorithmically optimize CoT structure
3. A/B test CoT styles during shadow mode

## V4 Application Scope

### Deep/Strategic Reflections (Primary)

Replace monolithic `REFLECTION_DEEP.md` / `REFLECTION_STRATEGIC.md` with
meta-prompted 3-step protocol. The existing prompt files become the default
fallback if meta-prompting is disabled.

### Daily Brainstorming (Upgrade)

From build phases (lines 1424–1431):

V3: Static Light templates for "upgrade user" + "upgrade self" brainstorms.
V4:
1. Cheap model generates brainstorming questions based on recent data,
   user model, system performance
2. Capable model explores the best questions with depth
3. Synthesis: actionable proposals → staging area

### Morning Report (Upgrade)

From build phases (lines 1433–1439):

V3: Static-prompt morning report.
V4:
1. Cheap model asks: "What does the user most need to hear this morning?"
2. Capable model generates report with adaptive section selection
3. Engagement data feeds back into content selection

## Failure Modes and Mitigations

### Wrong Questions (Primary Risk)

From autonomous behavior design (line 2628):

> Meta-prompting adds a new failure mode: wrong questions. If the
> meta-prompter asks the wrong questions, the entire reflection is
> misdirected.

**Mitigation:** Strategic reflection periodically audits meta-prompt question
quality — "Were the questions I asked last Deep reflection the right ones,
in hindsight?" (line 3049)

**Metric:** Did the reflection that followed produce observations that were
subsequently used? If meta-prompt questions consistently lead to unused
observations, the meta-prompter's signal interpretation needs adjustment.

### Over-Decomposition

From autonomous behavior design (line 2644):

> Full decomposition of Deep reflection into independent calls... Loses
> cross-cutting insights (the monolithic prompt's weakness is also its
> strength). Meta-prompting provides better decomposition — the meta-prompter
> sees everything holistically while the answerer gets focused questions.

**Mitigation:** Step 3 (Synthesis) explicitly looks for cross-cutting patterns.
The protocol preserves holistic insight through the synthesis pass.

### Anti-Sycophancy in Meta-Prompting

From model routing registry (lines 70–85):

DeepSeek is used for **analytical tasks** where the risk is wrong conclusions.
This is distinct from self-assessment (am I doing well?) which requires
Anthropic models. Meta-prompting is analytical: "what should we think about?"
not "am I being honest?"

## What V4 Must Build

### New Code

1. **`genesis.reflection.meta_prompter` module:**
   - `MetaPrompter` — Step 1 orchestrator (generates questions from signals)
   - `QuestionRouter` — routes each question to appropriate answerer with
     relevant context scope
   - `SynthesisRunner` — Step 3 orchestrator (cross-cutting pattern detection)
   - `MetaPromptPipeline` — end-to-end 3-step orchestrator

2. **`genesis.reflection.dspy_optimizer` module:**
   - `PromptOptimizer` — DSPy integration for prompt parameter optimization
   - `QualityCorpus` — manages reflection input/output pairs with quality labels
   - `OptimizationRunner` — periodic optimization job (monthly?)

3. **New CAPS markdown files:**
   - `META_PROMPT_TEMPLATE.md` — instructions for the cheap model (Step 1)
   - `SYNTHESIS_TEMPLATE.md` — instructions for synthesis (Step 3)
   - Possibly depth-specific meta-prompt variants

4. **Call site #18 activation:**
   - Currently deferred in model routing registry
   - Wire DeepSeek V4 primary → Qwen 3.5 Plus → GPT-5 Nano fallback chain

### Modifications to Existing Code

5. **CCReflectionBridge** — integrate MetaPromptPipeline as alternative to
   monolithic prompt path (feature-flagged)
6. **ContextGatherer** — support per-question context scoping (gather only
   relevant data for each meta-prompted question)
7. **OutputRouter** — handle multi-answer outputs from Step 2 + synthesis
   output from Step 3
8. **PromptBuilder** — preserve V3 static templates as fallback; add
   meta-prompted path for Deep/Strategic depths
9. **ReflectionScheduler** — toggle between monolithic and meta-prompted
   reflection based on feature flag
10. **Self-Learning Loop** — add reflection quality labeling (actionable/noise)
    for DSPy training corpus

## Data Prerequisites

| Prerequisite | Threshold |
|---|---|
| V3 reflection sessions with outcomes | 50+ Deep, 20+ Strategic |
| Quality labels on reflection outputs | Which observations were acted on |
| Engagement data on outreach | 50+ data points (for morning report optimization) |
| Shadow mode | Required — run meta-prompted alongside monolithic, compare quality |

## Dependency Chain

```
V3: Static prompts, collecting operational data (quality labels, engagement)
  ↓
V4: Meta-prompting uses that data to generate better questions
V4: DSPy uses that data to optimize prompt parameters
  ↓
V5: Soft-prompt fine-tuning (requires V4 meta-prompting to be stable)
```

## Design Constraints

- **Meta-prompter model is cheap, not dumb.** The meta-prompter must be a
  capable analytical model (DeepSeek V4, not a tiny local model). Wrong
  questions waste expensive compute downstream.
- **Monolithic fallback preserved.** V3 static prompts remain available as
  fallback. If meta-prompting degrades quality, revert to monolithic with
  one flag change.
- **Feature-flag per depth.** Can enable meta-prompting for Deep only while
  keeping Strategic monolithic, or vice versa.
- **Shadow mode is non-negotiable.** First 4 weeks run meta-prompted alongside
  monolithic. Compare quality before switching. This doubles cost temporarily
  but prevents quality regression.
- **Step 3 synthesis is mandatory.** Cannot skip synthesis to save cost. The
  cross-cutting insight pass is what makes meta-prompting better than simple
  decomposition.
- **CAPS markdown convention extends.** New meta-prompting templates follow
  the same convention: `META_PROMPT_TEMPLATE.md`, user-auditable and editable.

## References

- Autonomous behavior design: `docs/architecture/genesis-v3-autonomous-behavior-design.md`
  §Pattern 2 (lines 2419–2451), §Prompt Variation Interaction (line 2516),
  §Failure Modes (line 2628), §Decomposition (line 2644), §Meta-Prompt Audit
  (line 3049)
- Deferred integrations: `docs/architecture/genesis-deferred-integrations.md`
  §DSPy (lines 24–39), §Progressive Disclosure (lines 123–140), §CoT
  Optimization (lines 287–301)
- Build phases: `docs/architecture/genesis-v3-build-phases.md` §V3 Scope
  (lines 1009–1010), §V4 Brainstorm Upgrade (lines 1424–1431), §V4 Morning
  Report (lines 1433–1439), §Cross-Version Table (lines 1549–1559)
- Model routing registry: `docs/architecture/genesis-v3-model-routing-registry.md`
  §Call Site #18 (line 126), §Anti-Sycophancy (lines 70–85)
- Vision: `docs/architecture/genesis-v3-vision.md` §Meta-Prompting (line 220)
- Prompt builder: `src/genesis/perception/prompts.py`
- Perception templates: `src/genesis/perception/templates/`
- Identity prompts: `src/genesis/identity/REFLECTION_DEEP.md`,
  `REFLECTION_STRATEGIC.md`, `SELF_ASSESSMENT.md`, `QUALITY_CALIBRATION.md`
