# V4 Feature Spec: Strategic Reflection (MANAGER/DIRECTOR)

**Status:** DESIGNED — repositioned within GWT architecture. Activates after
4+ weeks V3 operation.
**Dependency:** Phase 7 (complete), Phase 9 (autonomy framework)
**V3 Groundwork:** Call site #6 (Opus), REFLECTION_STRATEGIC.md, StrategicTimerCollector
stub, CCReflectionBridge dispatch, awareness_ticks schema.
**GWT Integration:** MANAGER/DIRECTOR roles merge into the workspace
controller. MANAGER runs as periodic ATTEND enhancement (weekly). DIRECTOR
runs as periodic SELECT enhancement (monthly). See
`docs/architecture/genesis-v4-architecture.md` §8.

---

## What This Is

Strategic reflection is a periodic, system-level review that asks "Is my
architecture working? Am I working on what I'm supposed to?" — distinct from
V3's weekly self-assessment which asks "Am I improving at learning?"

It replaces v2's weekly + monthly cron-scheduled MANAGER/DIRECTOR prompts with
a single Strategic depth level that contains both roles on a counter-based
cadence.

## Roles

### MANAGER (weekly)

Runs on every Strategic reflection tick (~weekly cadence).

**Scope:** Architecture review, cross-referencing, parameter proposals.

**Inputs:**
- Weekly self-assessment (from Phase 7) — trends across 6 dimensions
- System metrics: procedure success rates, cost data, signal distributions
- Memory quality: consolidation stats, contradiction count, backlog size
- Active observations: unresolved count, staleness, priority distribution
- Skill effectiveness reports (from Phase 7 SkillEffectivenessAnalyzer)

**Outputs:**
- Proposed parameter adjustments (drive weights, salience thresholds,
  Awareness Loop depth thresholds)
- Architecture observations ("Memory consolidation is running but procedure
  quarantine isn't triggering — threshold may be too aggressive")
- Capability gap identification with frequency and feasibility assessment
- Outreach preference adjustments

**Key question:** "What should change about how the system operates?"

### DIRECTOR (monthly — every 4th Strategic tick)

Runs alongside MANAGER every 4th Strategic reflection (or >28 days since last).

**Scope:** Goal drift audit, budget review, strategic assessment.

**Inputs:**
- All MANAGER inputs + last 4 MANAGER outputs
- 30-day time allocation data (what did Genesis actually spend compute on?)
- SOUL.md stated priorities vs actual behavior
- Capability gap ROI analysis (which gaps are most impactful to close?)
- Cost trends over 30 days

**Outputs:**
- Goal-vs-reality drift report: which stated goals got zero attention?
- Budget adjustment proposals
- Capability acquisition proposals (user-approval gated)
- Strategic priority reordering
- Dormant goal audit with recommended actions

**Key question:** "Am I heading where I should be heading?"

## Execution Model

```
Every Strategic tick:
  1. Run MANAGER scope (always)
  2. Check counter: is this the 4th since last DIRECTOR? Or >28 days?
     → If yes, also run DIRECTOR scope
  3. Proposals logged to observations (type=strategic_proposal)
  4. Shadow mode: proposals logged but NOT auto-applied
  5. User reviews proposals via morning report or dashboard
```

## Activation Criteria

From build phases doc (V4 activation model):

| Prerequisite | Threshold |
|---|---|
| Weeks of full-stack V3 data | 4+ weeks |
| Shadow mode period | Required — log proposals without acting |
| Self-assessment data points | 4+ weekly assessments (input to MANAGER) |
| Procedure data | Meaningful success/failure counts |

Feature-flag gated. Can be disabled if degrading quality.

## V3 Groundwork Already In Place

| Component | Location | Status |
|---|---|---|
| Call site #6 (Opus) | config/model_routing.yaml | Active |
| REFLECTION_STRATEGIC.md | src/genesis/identity/ | Identity + JSON schema |
| CCReflectionBridge dispatch | src/genesis/cc/reflection_bridge.py | Handles Depth.STRATEGIC |
| StrategicTimerCollector | src/genesis/awareness/signals.py | Stub (returns 0.0) |
| awareness_ticks schema | src/genesis/db/schema.py | Table exists |
| time_since_last_strategic | depth_thresholds seed data | Signal defined |

## What V4 Must Build

### New Code

1. **`genesis.reflection.strategic` module:**
   - `StrategicReflectionOrchestrator` — MANAGER/DIRECTOR counter logic
   - `ManagerScope` — assembles MANAGER-specific context (system metrics,
     self-assessment trends, skill reports)
   - `DirectorScope` — assembles DIRECTOR-specific context (30-day allocation,
     goal drift, capability gaps)
   - Proposal types: `ParameterProposal`, `CapabilityProposal`,
     `BudgetProposal`, `PriorityProposal`

2. **REFLECTION_STRATEGIC.md rewrite:**
   - Conditional sections (MANAGER-only vs MANAGER+DIRECTOR)
   - Explicit parameter adjustment format
   - Goal-vs-reality drift analysis instructions
   - Anti-sycophancy emphasis (critical for strategic depth)

3. **Shadow mode infrastructure:**
   - `strategic_proposals` table or observation type
   - Proposal lifecycle: proposed → reviewed → approved/rejected → applied
   - Dashboard or morning report integration for user review

4. **System parameter modification:**
   - Drive weight adjustment API
   - Awareness Loop threshold adjustment API
   - Outreach preference adjustment API
   - All gated by user approval (V4 stays at L4 autonomy for this)

### Modifications to Existing Code

5. **StrategicTimerCollector** — implement actual timer (query awareness_ticks)
6. **CCReflectionBridge** — extend enriched context path to Strategic depth
   (currently Deep-only; V4 removes the `depth == Depth.DEEP` guard)
7. **ContextGatherer** — add `gather_for_strategic(db)` method
8. **OutputRouter** — add `route_strategic()` for proposal storage
9. **ReflectionScheduler** — optional: add Strategic cadence tracking
   (or keep it purely signal-driven via AwarenessLoop)

## Design Constraints

- **Anti-sycophancy is critical.** Strategic reflection must challenge its own
  prior recommendations. The Opus prompt must explicitly instruct self-critique.
- **Shadow mode is non-negotiable.** First 4 weeks of V4 strategic reflection
  must log proposals without acting. User reviews and approves the pattern
  before auto-application begins.
- **User approval for parameter changes.** Even after shadow mode, parameter
  modifications require user confirmation. This is L4 autonomy — propose and
  wait.
- **Feature-flag disableable.** If strategic reflection degrades system quality,
  it can be turned off without affecting V3 functionality.
- **Cost budget.** ~$0.30/call, ~$3.30-4.80/month for 4-8 calls/month.
  Opus-only, no fallback (strategic depth requires frontier judgment).

## Deferred from Operational Vitals (2026-03-22)

- **Reflection-to-action bridge**: When a reflection cycle identifies a broken
  subsystem, create a prioritized action item (not just an observation) that
  surfaces in the next session's cognitive state and cannot be ignored. Requires
  either a new `action_items` DB table or a priority field on observations, plus
  wiring into the cognitive state assembly. Currently reflection produces
  observations that inform future reflections, but the path to *action* is thin.

## References

- Autonomous behavior design: `docs/architecture/genesis-v3-autonomous-behavior-design.md`
  §Strategic Reflection (lines 355-363, 2959-3015)
- Build phases: `docs/architecture/genesis-v3-build-phases.md` §V4 Activation
  (lines 1399-1422)
- Agentic runtime: `docs/plans/2026-03-07-agentic-runtime-design.md` §5.2
  (lines 117-226)
- Model routing registry: call site #6
