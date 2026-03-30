# Phase 6: Learning Fundamentals — Design & Implementation Plan

> Created: 2026-03-09
> Dependencies: Phase 4 (Perception) ✅, Phase 5 (Memory Operations) ✅
> Unblocks: Phase 7 (after Stages 1-3), Phase 8, Phase 9, Inbox Monitor

## Context

Phase 6 is the Self-Learning Loop — Genesis's "dopaminergic system." It runs after
every user interaction, classifying outcomes, extracting lessons, updating procedural
memory, and calibrating its own triage accuracy. This is the highest-leverage,
highest-risk phase: bad classification compounds into systematic drift.

Phase 6 has grown from the original spec to include tool capability discovery,
sub-agent memory harvesting, skill infrastructure, and real signal collectors
(from research session 2026-03-08). The inbox monitor has its own plan doc
(`docs/plans/2026-03-09-inbox-monitor-plan.md`) — Phase 6 builds infrastructure
it depends on, not the monitor itself.

---

## Key Design Decisions

1. **Option C staging** — Single phase, 5 internal stages. Phase 7 starts after
   Stages 1-3. Stages 4-5 parallel with Phase 7.
2. **Triage hooks into ConversationLoop** — All user interactions route through
   foreground CC. Triage is fire-and-forget post-processing in `handle_message()`.
3. **Router for all LLM calls** — Even local SLM triage. Different users will have
   different setups. Router abstracts provider selection.
4. **TRIAGE_CALIBRATION.md** — CAPS markdown file for calibration rules + few-shot
   examples. Follows SOUL.md/USER.md transparency pattern.
5. **Gemini surgical only** — Mistral preferred for fallback chains. Gemini only
   when specifically best tool (YouTube, etc.).
6. **Simplified daily calibration** — Under-classification audit uses interaction
   summaries (not full chat logs). Full chat log audit upgrades in Phase 7+.
7. **Skills inventory first** — Enumerate ALL skills before building individuals.
   SKILL.md progressive disclosure pattern.

---

## Package Structure

```
src/genesis/learning/
    __init__.py
    types.py                    # All enums + frozen dataclasses
    triage/
        __init__.py
        prefilter.py            # Programmatic pre-filter (zero cost)
        classifier.py           # SLM triage classification via Router
        summarizer.py           # InteractionSummary from CCOutput
        calibration.py          # Daily 30B calibration cycle
    classification/
        __init__.py
        outcome.py              # 5-class outcome classification
        delta.py                # Request-delivery delta assessment
        attribution.py          # Attribution → learning signal routing
    procedural/
        __init__.py
        operations.py           # Store, retrieve, update, confidence
        matcher.py              # Best-match by task_type + context
        maturity.py             # Null hypothesis milestones
    signals/
        __init__.py
        budget.py               # Real BudgetCollector
        error_spike.py          # Real ErrorSpikeCollector
        critical_failure.py     # Real CriticalFailureCollector
        task_quality.py         # Real TaskQualityCollector
        memory_backlog.py       # Real MemoryBacklogCollector
    harvesting/
        __init__.py
        debrief.py              # Structured debrief parser
        auto_memory.py          # Auto-memory directory harvester
    skills/
        __init__.py
        inventory.py            # Skills enumeration
        wiring.py               # AZ plugin directory wiring
    observation_writer.py       # Depth 2+ retrospective observations
    speculative_filter.py       # Quarantine from retrieval context
    signal_tiers.py             # Strong/moderate/weak enforcement
    engagement.py               # Per-channel engagement heuristics
    tool_discovery.py           # Populate tool_registry + content-type routing
    fallback_chains.py          # Static obstacle resolution chains

src/genesis/identity/
    TRIAGE_CALIBRATION.md       # Few-shot examples + calibration rules
    MICRO_TEMPLATE_ANALYST.md   # Externalized from PromptBuilder
    MICRO_TEMPLATE_CONTRARIAN.md
    MICRO_TEMPLATE_CURIOSITY.md
    LIGHT_TEMPLATE_SITUATION.md
    LIGHT_TEMPLATE_USER_IMPACT.md
    LIGHT_TEMPLATE_ANOMALY.md

src/genesis/skills/
    evaluate/SKILL.md + references/
    retrospective/SKILL.md + references/
    research/SKILL.md + references/
    debugging/SKILL.md + references/
    obstacle-resolution/SKILL.md + references/
    triage-calibration/SKILL.md + references/

config/model_routing.yaml      # 3 new call sites
```

---

## Type Definitions

All in `genesis.learning.types`:

```python
class OutcomeClass(StrEnum):
    SUCCESS = "success"
    APPROACH_FAILURE = "approach_failure"
    CAPABILITY_GAP = "capability_gap"
    EXTERNAL_BLOCKER = "external_blocker"
    WORKAROUND_SUCCESS = "workaround_success"

class TriageDepth(IntEnum):
    SKIP = 0           # No retrospective needed
    QUICK_NOTE = 1     # One-line observation
    WORTH_THINKING = 2 # Standard retrospective
    FULL_ANALYSIS = 3  # Deep analysis with delta assessment
    FULL_PLUS_WORKAROUND = 4  # Full + workaround documentation

class DeltaClassification(StrEnum):
    EXACT_MATCH = "exact_match"
    ACCEPTABLE_SHORTFALL = "acceptable_shortfall"
    OVER_DELIVERY = "over_delivery"
    MISINTERPRETATION = "misinterpretation"

class DiscoveryAttribution(StrEnum):
    """Multi-valued — an interaction can have multiple attributions."""
    EXTERNAL_LIMITATION = "external_limitation"
    USER_MODEL_GAP = "user_model_gap"
    GENESIS_CAPABILITY = "genesis_capability"
    GENESIS_INTERPRETATION = "genesis_interpretation"
    SCOPE_UNDERSPECIFIED = "scope_underspecified"
    USER_REVISED_SCOPE = "user_revised_scope"

class SignalWeightTier(StrEnum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"

class EngagementOutcome(StrEnum):
    ENGAGED = "engaged"
    IGNORED = "ignored"
    NEUTRAL = "neutral"

class MaturityStage(StrEnum):
    EARLY = "early"      # < 50 procedures
    GROWING = "growing"  # 50-200 procedures
    MATURE = "mature"    # > 200 procedures

@dataclass(frozen=True)
class InteractionSummary:
    session_id: str
    user_text: str          # Truncated to ~500 chars
    response_text: str      # Truncated to ~1000 chars
    tool_calls: list[str]   # Regex-detected until GL-3 adds structured field
    token_count: int
    channel: str
    timestamp: datetime

@dataclass(frozen=True)
class TriageResult:
    depth: TriageDepth
    rationale: str
    skipped_by_prefilter: bool

@dataclass(frozen=True)
class ScopeEvolution:
    original_request: str
    final_delivery: str
    scope_communicated: bool  # Did user acknowledge scope change?

@dataclass(frozen=True)
class RequestDeliveryDelta:
    classification: DeltaClassification
    attributions: list[DiscoveryAttribution]
    scope_evolution: ScopeEvolution | None
    evidence: str

@dataclass(frozen=True)
class RetrospectiveResult:
    summary: InteractionSummary
    triage: TriageResult
    outcome: OutcomeClass | None          # None if depth 0
    delta: RequestDeliveryDelta | None    # None if depth < 3
    observations_written: int
    procedures_updated: int

@dataclass(frozen=True)
class ProcedureMatch:
    procedure_id: str
    task_type: str
    confidence: float
    success_count: int
    failure_count: int
    failure_modes: list[dict]       # [{condition, transient, count}]
    workarounds: list[dict]         # [{failed_method, working_method, context}]

@dataclass(frozen=True)
class CalibrationRules:
    examples: list[dict]            # [{input_summary, expected_depth, rationale}]
    rules: list[str]                # Free-text calibration adjustments
    generated_at: datetime
    source_model: str

@dataclass(frozen=True)
class EngagementSignal:
    channel: str
    outcome: EngagementOutcome
    latency_seconds: float | None
    evidence: str

@dataclass(frozen=True)
class FallbackChain:
    obstacle_type: str
    methods: list[str]              # Ordered fallback sequence
    current_index: int
```

---

## Stage 1: Foundation

**Goal:** Types, triage pipeline, procedural memory operations.

### Steps

1.1. **types.py** — All enums and frozen dataclasses (zero deps)

1.2. **procedural/maturity.py** — `get_maturity_stage(db)` counts procedures →
EARLY/GROWING/MATURE. Thresholds: <50, 50-200, >200.

1.3. **procedural/operations.py** — Store, record_success, record_failure (with
conditions + transient flag), record_workaround, update_confidence (Laplace
smoothing: `(successes + 1) / (successes + failures + 2)`).

1.4. **procedural/matcher.py** — `find_best_match(db, task_type, context_tags)`
ranked by confidence × context overlap score. Returns failure_modes and
workarounds alongside success data.

1.5. **triage/summarizer.py** — `build_summary(CCOutput, session, user_text, channel)`
truncates text, extracts tool_calls (regex workaround until GL-3 adds tool_calls
to CCOutput).

1.6. **triage/prefilter.py** — `should_skip(summary)` returns True if
tokens < 100 AND no tools. Zero-cost gate before any LLM call.

1.7. **TRIAGE_CALIBRATION.md** — Hand-crafted initial version: 5-8 few-shot
examples across all depths + empty calibration rules section for daily cycle
to populate.

1.8. **triage/classifier.py** — `TriageClassifier` loads TRIAGE_CALIBRATION.md,
builds prompt with few-shot examples, calls Router("retrospective_triage"),
parses depth + rationale. Batch mode for daily calibration backlog.

1.9. **model_routing.yaml** — Add 3 call sites:
  - `retrospective_triage`: ollama → groq-free → mistral-free
  - `triage_calibration`: lmstudio-30b → mistral-large → deepseek-v4
  - `outcome_classification`: deepseek-v4 → qwen-plus → mistral-large

1.10. **ConversationLoop integration** — Add optional `triage_pipeline` param.
Fire-and-forget `asyncio.create_task()` after response returned. Triage never
blocks the user response.

### Files Modified
- `src/genesis/cc/conversation.py` — triage hook
- `config/model_routing.yaml` — 3 new call sites
- New: `src/genesis/learning/` (types, triage/*, procedural/*)
- New: `src/genesis/identity/TRIAGE_CALIBRATION.md`

---

## Stage 2: Classification Pipeline

**Goal:** Outcome classification, delta assessment, attribution routing, observations.

### Steps

2.1. **classification/outcome.py** — `OutcomeClassifier.classify(trace, summary)`
via Router("outcome_classification"). Prompt includes 5 class definitions with
exhaustion requirements for capability_gap and external_blocker.

2.2. **classification/delta.py** — `DeltaAssessor.assess()` — combined in same
LLM call as outcome to save cost. Produces ScopeEvolution +
DeltaClassification + DiscoveryAttribution[].

2.3. **classification/attribution.py** — `route_learning_signals(db, delta,
outcome, memory_store)` — each attribution maps to concrete write target:
  - `external_limitation` → observation
  - `user_model_gap` → user model update + procedural note
  - `genesis_capability` → capability_gap OR procedural update
  - `genesis_interpretation` → observation (interpretation_correction)
  - `scope_underspecified` → procedural note
  - `user_revised_scope` → no-op (track frequency only)

2.4. **observation_writer.py** — Depth 2+ writes retrospective observation to
observations table + MemoryStore (dual-write, matching Phase 4 ResultWriter
pattern).

2.5. **signal_tiers.py** — Static tier mapping + `PROTECTED_BEHAVIORS` list
(pushback, honesty). Weak signals blocked from eroding protected behaviors.

2.6. **speculative_filter.py** — Filter speculative=1 from retrieval context.
`expire_stale_claims(db)` archives claims past expiry with zero supporting
evidence.

2.7. **Wire full pipeline** — triage → classify → delta → attribute → write.
RetrospectiveResult ties it all together as the pipeline's return value.

### Files Modified
- New: `src/genesis/learning/classification/` (outcome, delta, attribution)
- New: observation_writer.py, signal_tiers.py, speculative_filter.py

---

## Stage 3: Infrastructure

**Goal:** Real signal collectors, perception cleanup, obstacle resolution.

### Steps

3.1-3.5. **Real signal collectors** (replace Phase 1 stubs):
  - **BudgetCollector**: queries cost_events, normalizes against daily budget
  - **ErrorSpikeCollector**: counts ERROR+ events vs 24h baseline, spike if >3×
  - **CriticalFailureCollector**: runs health probes, 1.0 if DOWN, 0.5 DEGRADED
  - **TaskQualityCollector**: queries execution_traces outcome distribution
  - **MemoryBacklogCollector**: counts observations not yet in Qdrant

  All follow Trackio pattern: domain knowledge in collector code, LLM gets
  pre-interpreted signals (not raw data).

3.6. **Update _10_initialize_genesis.py** — Inject db, event_bus, probes into
real collectors. Replace stub registrations.

3.7-3.8. **Perception template externalization** — 6 templates to CAPS markdown
in `src/genesis/identity/`. PromptBuilder loads from files with fallback to
existing hardcoded strings.

3.9. **fallback_chains.py** — Static chains for: web_fetch, api_rate_limit,
model_unavailable, tool_failure, permission_error.
`get_next_method(obstacle, failed_methods)` returns next option or None.

3.10. **tool_discovery.py** — Populate tool_registry with capability metadata.
Content-type routing function. Cross-model routing (YouTube→Gemini,
web→Firecrawl, etc.). Record capability_gap when no tool handles a content type.

### Files Modified
- New: `src/genesis/learning/signals/` (5 collectors)
- Modify: `src/genesis/awareness/signals.py` (replace stubs)
- Modify: AZ extension `_10_initialize_genesis.py`
- Modify: `src/genesis/perception/prompts.py` (load from identity/ with fallback)
- New: 6 CAPS markdown template files in `src/genesis/identity/`
- New: fallback_chains.py, tool_discovery.py

### >>> PHASE 7 UNBLOCKED after Stage 3 verification <<<

---

## Stage 4: CC Integration (parallel with Phase 7)

**Goal:** Sub-agent memory harvesting, skill system.

### Steps

4.1. **harvesting/debrief.py** — Parse `learnings` section from CC session
output (JSON array or markdown list).

4.2. **harvesting/auto_memory.py** — Read CC session `.claude/memory/` dir,
filter CC internals, ingest Genesis-relevant items into MemoryStore.

4.3. **Update CC system prompts** — Add "include a `learnings` section in your
final output" instruction to reflection/task session prompts.

4.4. **Post-session hook** — After CC session completes: parse debrief +
harvest auto-memory → store in MemoryStore.

4.5. **skills/inventory.py** — Enumerate all skills:

| Consumer | Skills (Phase 6) |
|----------|------------------|
| CC background — reflection | `retrospective` |
| CC background — research | `evaluate` (restructured from command), `research` |
| CC background — task | `debugging`, `obstacle-resolution` |
| Daily calibration | `triage-calibration` |
| Phase 7 | `deep-reflection`, `strategic-reflection`, `self-assessment` |
| Phase 8 | `morning-report`, `outreach` |
| Phase 9 | `task-planning`, `verification` |
| Post-Phase 6 | `inbox-classify`, `inbox-research` |

4.6. **Create SKILL.md files** — Phase 6 builds: evaluate, retrospective,
research, debugging, obstacle-resolution, triage-calibration. Each with
progressive disclosure (frontmatter ~100 tokens, body <5000 tokens,
references/ on demand).

4.7. **skills/wiring.py** — Symlink/copy to
`usr/plugins/genesis/skills/<name>/SKILL.md` for AZ discovery.

### Files Modified
- New: `src/genesis/learning/harvesting/` (debrief, auto_memory)
- New: `src/genesis/learning/skills/` (inventory, wiring)
- New: `src/genesis/skills/` (6 skill directories with SKILL.md + references/)
- Modify: CC session system prompts (reflection_bridge, invoker)

---

## Stage 5: Calibration (parallel with Phase 7)

**Goal:** Daily triage calibration, engagement signals.

### Steps

5.1. **triage/calibration.py** — `TriageCalibrator.run_daily_calibration()`:
  1. Sample triage decisions (last 24h, stratified by depth)
  2. Under-classification audit: depth-0 interaction summaries → 30B review
  3. Over-classification audit: check if depth-2+ observations were retrieved
  4. Memory pattern review: topic trends from MemoryStore
  5. Generate updated few-shot examples + calibration rules
  6. Validate output (5+ examples across all depths) then write
     TRIAGE_CALIBRATION.md atomically

  Uses Router("triage_calibration"): lmstudio-30b → mistral-large → deepseek-v4

5.2. **engagement.py** — Fixed per-channel heuristics:
  - WhatsApp: reply <4h = engaged, no read 24h = ignored
  - Telegram: reaction/reply = engaged, nothing 24h = ignored
  - Web UI: click-through = engaged, no interaction = ignored
  - Terminal: substantive reply = engaged, monosyllabic = neutral

5.3. **Wire calibration** — APScheduler job (daily, companion to Morning Report)

5.4. **Observability events** — LEARNING subsystem events for triage,
classification, calibration, harvesting, capability gaps.

### Files Modified
- New: triage/calibration.py, engagement.py
- Modify: observability/types.py (add LEARNING subsystem)
- New: AZ extension `_50_genesis_learning.py`

---

## New Router Call Sites

```yaml
retrospective_triage:
  description: "Step 0 triage — classify interaction depth"
  tier: micro
  chain: [ollama, groq-free, mistral-free]

triage_calibration:
  description: "Daily audit of triage decisions"
  tier: utility
  chain: [lmstudio-30b, mistral-large, deepseek-v4]

outcome_classification:
  description: "Outcome + delta classification for depth 1+ interactions"
  tier: utility
  chain: [deepseek-v4, qwen-plus, mistral-large]
```

---

## GL-3 Coordination Items

1. **CCOutput enrichment** — Add `tool_calls: list[str]` and `tool_call_count: int`
   when CC CLI structured output supports it. Phase 6 workaround: regex tool
   marker detection in output text.
2. **Triage pipeline injection** — ConversationLoop needs `triage_pipeline` param.
   GL-3 wires this when creating the ConversationLoop for Telegram relay.
3. Add these items to `docs/plans/2026-03-07-cc-go-live-design.md` GL-3 section.

---

## AZ Extension: _50_genesis_learning.py

Runs after `_30_genesis_perception.py` (Router) and `_40_genesis_cc_relay.py`
(ConversationLoop).

Responsibilities:
- Import real signal collectors, inject db + event_bus + probes
- Replace stub collectors in AwarenessLoop
- Create TriageClassifier with Router
- Build full triage pipeline (prefilter → classifier → retrospective steps)
- Inject triage_pipeline into ConversationLoop
- Create daily calibration APScheduler job
- Register LEARNING subsystem health probe + observability events

---

## Testing Strategy

~165 new tests, ~908 cumulative.

- **Unit (no LLM):** Types, prefilter, summarizer, procedural ops, matcher,
  maturity, signal tiers, speculative filter, attribution routing, fallback
  chains, engagement heuristics, debrief parser, tool discovery
- **Integration (mock Router):** Full triage pipeline, full classification
  pipeline, procedure lifecycle, observation writer, calibration cycle
- **Real collectors:** Mock db/event_bus/probes, verify normalized signal
  output (0.0-1.0)
- **Golden test cases:** Hand-verified triage depths + outcome classifications
- **LLM testing principle:** Test prompt construction + output parsing + error
  handling. Never test "does the LLM give the right answer."

---

## Deferred Items

| Item | Deferred To | Reason |
|------|-------------|--------|
| Confidence decay | V4 | Needs data to tune rate |
| 3B fine-tuning (LoRA) | V4 | Needs hundreds of audited decisions |
| Signal weight calibration | V4 | Needs interaction data |
| Adaptive obstacle resolution | V4 | V3 builds static; V4 makes adaptive |
| Weekly quality calibration | Phase 7 | Part of Strategic reflection |
| Cross-run context injection | Phase 7 | Needs Phase 6 stored learnings |
| Strategic triage quality review | Phase 7 | Weekly Opus audit of 30B |
| Reflection template expansion | Phase 7+ | Needs outcome data |
| Conversation/Outreach/Recon collectors | Phase 8 | Data sources don't exist yet |
| Full chat log audit (calibration) | Phase 7+ | Conversation history needed |
| Inbox monitor | Post-Phase 6 | Own plan doc exists |

---

## Verification

### Per-Stage Gates

**Stage 1:**
- [ ] Triage runs on every interaction (fire-and-forget, never blocks response)
- [ ] Prefilter correctly skips tokens < 100 + no tools
- [ ] SLM triage assigns depth consistent with golden test set
- [ ] Procedural memory CRUD operations work end-to-end
- [ ] Maturity thresholds switch at correct data volume (0/49/50/199/200/201)

**Stage 2:**
- [ ] Outcome classification matches golden test set
- [ ] capability_gap and external_blocker require exhaustion evidence
- [ ] workaround_success stores BOTH failed path AND working alternative
- [ ] Attribution routing: each type writes to correct target
- [ ] scope_communicated=false flagged regardless of delta
- [ ] Speculative claims quarantined from retrieval
- [ ] Depth 2+ observations written to memory

**Stage 3:**
- [ ] 5 real collectors produce normalized signals (0.0-1.0)
- [ ] 6 perception templates externalized to CAPS markdown
- [ ] PromptBuilder loads from identity/ with fallback
- [ ] Fallback chains return next method, None at end
- [ ] tool_registry populated, content-type routing works
- [ ] capability_gaps updated when no tool handles content type

**Stage 4:**
- [ ] CC session outputs include learnings section
- [ ] Debrief parser extracts learnings from JSON + markdown formats
- [ ] Auto-memory harvest filters CC internals
- [ ] 6 SKILL.md files created with progressive disclosure
- [ ] Skills discoverable via AZ skills_tool:load

**Stage 5:**
- [ ] Daily calibration produces updated TRIAGE_CALIBRATION.md
- [ ] Calibration validates output before write (5+ examples)
- [ ] Engagement heuristics correct per channel
- [ ] LEARNING subsystem events emitted

### Full Suite
```bash
cd ~/genesis && ruff check . && pytest -v
```
All tests pass. Zero lint errors. ~908 tests cumulative.

---

## Implementation Notes

1. **Worktree per stage** — Each stage gets its own worktree branch for isolation.
2. **Subagent verification** — Run `git status --short` after all subagent tasks.
   Stage any `??` files under `src/` or `tests/`.
3. **AZ extension files** — `usr/**` is in .gitignore — must `git add -f`.
4. **Ollama model change** — The local SLM is changing from 3B to a newer model.
   Router config abstracts this — update `model_routing.yaml` provider entry,
   not triage code.
