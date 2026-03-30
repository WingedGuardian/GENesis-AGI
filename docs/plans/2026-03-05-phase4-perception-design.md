# Phase 4: Perception (Micro/Light Reflection) — Design

> **Date:** 2026-03-05
> **Status:** Approved
> **Dependencies:** Phase 1 (Awareness Loop), Phase 2 (Compute Routing)
> **Scope:** Full depth skeleton — Micro/Light functional, Deep/Strategic stubbed

---

## Overview

Phase 4 introduces Genesis's first LLM calls. The Awareness Loop (Phase 1) classifies
signal urgency into depths; Phase 4 adds the Reflection Engine that actually *thinks*
about those signals. This is where Genesis goes from measuring to perceiving.

**Architecture approach:** Layered Pipeline — ContextAssembler → PromptBuilder →
LLMCaller → OutputParser → ResultWriter, orchestrated by ReflectionEngine.

---

## 1. Component Architecture

### Package: `genesis.perception`

```
genesis/perception/
├── __init__.py
├── types.py         # ReflectionResult, PromptContext, MicroOutput, LightOutput, etc.
├── context.py       # ContextAssembler
├── prompts.py       # PromptBuilder (template selection, rotation)
├── caller.py        # LLMCaller (routes through genesis.routing)
├── parser.py        # OutputParser (schema validation, retry)
├── writer.py        # ResultWriter (stores observations, emits events)
├── engine.py        # ReflectionEngine (orchestrates pipeline)
├── schemas/         # Output validation contracts
│   ├── micro.py
│   └── light.py
└── templates/       # Prompt templates
    ├── micro/       # 3 rotating micro templates
    └── light/       # 3 focus-area light templates

genesis/identity/
├── SOUL.md          # Already exists (~1100 tokens)
├── user.md          # Seed file (drafted after Phase 4 implementation)
└── loader.py        # Reads/caches identity docs for context assembly
```

### New DB table

```sql
cognitive_state (
    id          INTEGER PRIMARY KEY,
    content     TEXT NOT NULL,
    section     TEXT NOT NULL,      -- 'active_context' | 'pending_actions' | 'state_flags'
    generated_by TEXT,              -- model that generated this
    created_at  TIMESTAMP NOT NULL,
    expires_at  TIMESTAMP           -- NULL = no expiry
)
```

CRUD module + DDL migration. At Phase 4, generation is stubbed — table and rendering
pipeline are built so they're ready for Phase 7 when Deep reflection regenerates it.

---

## 2. Data Flow

```
TICK TRIGGERS REFLECTION
        │
        ▼
┌─ContextAssembler ──────────────────────────────────┐
│  Assembles by RELEVANCE to depth, not by budget.   │
│  Quality is non-negotiable.                        │
│                                                    │
│  MICRO scope:                                      │
│    SOUL.md + signal batch from tick                 │
│    (everything relevant, nothing truncated)         │
│                                                    │
│  LIGHT scope:                                      │
│    + user.md + cognitive_state                      │
│    + memory hits (top-k by activation score)        │
│    + user model from cache                         │
│                                                    │
│  DEEP/STRATEGIC scope: (stubbed Phase 4)           │
│    + observations + procedural memory + journal     │
│                                                    │
│  If assembled context is abnormally large for the  │
│  depth → log depth_mismatch signal (V4 tuning).    │
│  Never truncate.                                   │
└────────────────────────────────────────────────────┘
        │ PromptContext
        ▼
┌─PromptBuilder ─────────────────────────────────────┐
│  Selects template by depth + focus area            │
│  Micro: round-robin (tick_number % 3)              │
│  Light: by suggested_focus from TickResult         │
│  Renders template with PromptContext variables     │
│  Plain text substitution — no Jinja, no complexity │
└────────────────────────────────────────────────────┘
        │ prompt: str
        ▼
┌─LLMCaller ─────────────────────────────────────────┐
│  1. router.route(call_site_id)                     │
│     Router walks chain based on breaker state +    │
│     availability (NOT cost)                        │
│  2. litellm.acompletion() — direct call, not       │
│     through AZ's unified_call()                    │
│  3. On success: record cost (observability only),  │
│     return LLMResponse                             │
│  4. On failure: breaker records, router tries next │
│     If chain exhausted → emit event, return None   │
│     Dead letter queue captures failed request      │
└────────────────────────────────────────────────────┘
        │ LLMResponse | None
        ▼
┌─OutputParser ───────────────────────────────────────┐
│  Validates against depth-specific output contract  │
│  On validation failure:                            │
│    retry < 2 → re-prompt with error feedback       │
│    retry >= 2 → accept partial/degraded            │
└────────────────────────────────────────────────────┘
        │ ParsedOutput
        ▼
┌─ResultWriter ──────────────────────────────────────┐
│  Micro: stores observation, emits event            │
│         If anomaly=True → set suggested_focus      │
│         for next tick                              │
│  Light: stores observation + applies user model    │
│         deltas to cache via Phase 0 CRUD           │
└────────────────────────────────────────────────────┘
        │ ReflectionResult
        ▼
  ReflectionEngine returns to AwarenessLoop
```

### Design Principle: Quality Over Cost

Cost tracking is observability, not control. ContextAssembler never truncates to save
tokens. LLMCaller never auto-routes to cheaper models based on spend. Cost management
is a conversation between Genesis and the user. See CLAUDE.md "Quality over cost."

---

## 3. Prompt Templates & Output Contracts

### Micro Templates (3 starters)

```
Template 1 (Analyst):    "You are reviewing system telemetry. Classify these signals..."
Template 2 (Contrarian): "Assume these signals are normal. What would prove you wrong?"
Template 3 (Curiosity):  "What's the most interesting thing in this data?"
```

Selection: `tick_number % 3` — deterministic, zero overhead.

Template expansion revisited in Phase 6 when outcome classification data reveals
which templates produce useful observations vs noise. Target 5-7 micro templates
once stabilized, but only if data justifies it.

### Light Templates (3 focus-area based)

```
Template: situation    — general assessment (default)
Template: user_impact  — user-goal oriented
Template: anomaly      — triggered when Micro flagged anomaly=True
```

Selection: driven by `suggested_focus` field on TickResult. If no suggestion,
defaults to `situation`.

### Output Contracts

```python
@dataclass(frozen=True)
class MicroOutput:
    tags: list[str]           # Classification tags
    salience: float           # 0.0-1.0, how noteworthy
    anomaly: bool             # Deviation from expected patterns
    summary: str              # 1-2 sentence plain language
    signals_examined: int     # Batch size

@dataclass(frozen=True)
class LightOutput:
    assessment: str                          # Situation analysis
    patterns: list[str]                      # Identified trends
    user_model_updates: list[UserModelDelta] # Typed deltas
    recommendations: list[str]              # Actions or watch items
    confidence: float                        # 0.0-1.0
    focus_area: str                          # Which template was used

@dataclass(frozen=True)
class UserModelDelta:
    field: str          # What aspect of user model to update
    value: str          # New value or observation
    evidence: str       # What signal/data supports this
    confidence: float   # 0.0-1.0

# Deep/Strategic output contracts: TBD in Phase 7 based on Micro/Light
# operational experience.
```

### Downstream Consumers

- `MicroOutput.anomaly == True` → observation stored tagged `anomaly`, next tick
  gets `suggested_focus = "anomaly"` so Light investigates
- `MicroOutput.salience > threshold` → observation stored with higher activation
  score (Phase 5 retrieval ranking)
- `LightOutput.user_model_updates` → ResultWriter applies deltas to user model
  cache via Phase 0 CRUD
- **Fresh system bootstrap**: if `cognitive_state` table is empty, ContextAssembler
  loads a static bootstrap template: `"[No cognitive state yet. This is a fresh
  system. Assess signals without prior context.]"`

---

## 4. Router Integration & LLMCaller

LLMCaller calls `litellm.acompletion()` directly — NOT through AZ's `unified_call()`.
Genesis's routing system is separate from AZ's model config.

### Call Sites Wired in Phase 4

| Call Site ID | Chain (from routing registry) |
|---|---|
| `3_micro_reflection` | local-30b → mistral-large3-free → groq-llama70b → gpt5-nano |
| `4_light_reflection` | claude-haiku-4.5 → claude-sonnet-4.6 |
| `10_cognitive_state_regen` | glm5 → claude-sonnet-4.6 |

Micro is Bucket 2 (free compute primary). Light and cognitive state regen are
Bucket 3 (judgment & synthesis — paid models required for honest assessment).

### What LLMCaller Does NOT Do

- Pick models (Router's job)
- Manage cost (CostTracker observes, user decides)
- Retry on bad output (OutputParser handles that by calling LLMCaller again)
- Know anything about reflection depths (takes prompt + call_site_id, period)

---

## 5. Pre-Execution Assessment

Architecturally distinct from the reflection pipeline. This is a **prompt pattern
in the task execution flow**, not a standalone component or AZ extension.

### How It Works

When Genesis determines a user request needs a task, and the task is about to be
spawned, the Pre-Execution Assessment fires. The task executor's own model reviews
the task definition with Genesis's context:

- SOUL.md (philosophical mandate for honest engagement)
- User model (preferences, expertise, past decisions)
- Relevant memory (similar past tasks, known failure modes)
- Cognitive state (current projects, recent context)
- Open questions (unresolved uncertainties relevant to this request)

### Decision Space

- **Proceed** — request is clear, information sufficient (vast majority)
- **Proceed with note** — execute, but flag something user should know
- **Clarify** — ambiguous/underspecified, ask minimum needed to proceed well
- **Challenge** — evidence suggests request may be suboptimal or conflicting
- **Suggest alternative** — a better path exists for the user's inferred goal

### What Phase 4 Builds

- `IdentityLoader` — reads/caches SOUL.md + user.md (shared with reflection pipeline)
- Assessment prompt template — the instructions that frame the review
- Context assembly for assessment (reuses ContextAssembler with assessment scope)

### What Phase 4 Does NOT Build

- The task system itself (AZ's existing multi-agent orchestration)
- The planning pass (downstream of assessment)
- Sub-agent spawning (AZ handles this)

### Routing

Uses the same model as the task executor (#27 in routing registry). Does NOT go
through Genesis's Router — it's part of the task's own model context.

---

## 6. ReflectionEngine Orchestration

```python
class ReflectionEngine:
    """Orchestrates the perception pipeline.

    Stateless — all state lives in the DB and context assembly.
    Each call is independent.
    """

    def __init__(
        self,
        context_assembler: ContextAssembler,
        prompt_builder: PromptBuilder,
        llm_caller: LLMCaller,
        output_parser: OutputParser,
        result_writer: ResultWriter,
        event_bus: GenesisEventBus | None = None,
    ): ...

    async def reflect(
        self,
        depth: Depth,
        tick_result: TickResult,
    ) -> ReflectionResult: ...
```

### The `reflect()` Flow

1. `context = context_assembler.assemble(depth, tick_result)`
2. `prompt = prompt_builder.build(depth, context)`
3. `response = llm_caller.call(prompt, call_site_id)`
4. If `response is None` → emit `reflection.failed`, return failed result
5. `parsed = output_parser.parse(response, depth)`
6. If `parsed.needs_retry` and retries < 2 → re-call with error feedback
7. `result_writer.write(parsed, depth, tick_result)`
8. Return `ReflectionResult(success=True, output=parsed)`

### Integration with AwarenessLoop

```python
# In awareness/loop.py — perform_tick() adds:
if tick_result.classified_depth in (Depth.MICRO, Depth.LIGHT):
    reflection = await self.reflection_engine.reflect(
        depth=tick_result.classified_depth,
        tick_result=tick_result,
    )
# Deep/Strategic: no-op in Phase 4 (logged, not acted on)
```

### Error Philosophy

If the entire chain is exhausted and no LLM responds, the tick still succeeds —
it just has no reflection attached. Reflection is additive, not blocking. EventBus
notifies (observability), dead letter queue captures for potential replay.

---

## 7. Testing Strategy

### Layer 1: Unit Tests (deterministic, every commit)

- ContextAssembler: mock signals + identity → correct PromptContext structure
- PromptBuilder: depth + context → correct template rendered with variables
- OutputParser: valid JSON → typed output; malformed JSON → retry; partial → degrade
- ResultWriter: parsed output → correct CRUD calls with correct arguments
- ReflectionEngine: mocked components → correct orchestration order, handles None,
  respects retry limit
- Template rotation: tick_number % 3 selects expected template
- Light template selection: suggested_focus maps to correct template

### Layer 2: Contract Tests (schema validation, every commit)

- Sample LLM-like outputs through OutputParser
- MicroOutput / LightOutput enforce their contracts
- Edge cases: salience out of range, empty tags, missing fields
- Retry prompt includes the parse error for LLM to fix

### Layer 3: Integration Tests (real LLM, manual/pre-release)

- Separate suite, `--run-integration` flag
- Hits actual model endpoints (local 30B if available, free APIs)
- Full pipeline: real signals → valid MicroOutput? Coherent LightOutput?
- Assert structural properties, not exact content
- Slow, costs real compute, not required per commit

**Key principle:** Test behavior in aggregate and at boundaries, not exact LLM text.
If you're asserting exact strings from an LLM, you're testing the wrong thing.

---

## Practical Notes

### API Key Availability

As of 2026-03-05, only AZ's 4 model API keys are configured. Micro reflection
(free chain: local 30B, Mistral free, Groq free) can be tested first. Light
reflection (Haiku/Sonnet) and cognitive state regen (GLM5) require additional
API keys before they can fire.

### Fresh System Bootstrap

A fresh Genesis install should start conservative — mostly Micro depth, building
context before escalating. This is a natural consequence of stub signal collectors
returning minimal data (low urgency → low depth classification), but should also
be designed intentionally via the cognitive state bootstrap flag:
`[Bootstrap: Phase 1 | Day: 0 | Autonomy: L1 | Last Deep: never]`

### Deferred Items

- **Phase 4b: Surplus Wiring** — wire SurplusExecutor to trigger extra micro/light
  reflections during idle cycles on free compute
- **Observation Sweep (#28)** — deferred to Phase 5+ when memory operations provide
  observable data
- **Template expansion** — revisited in Phase 6 when outcome data exists
- **Identity files (user.md)** — drafted after Phase 4 implementation
- **Deep/Strategic output contracts** — defined in Phase 7
