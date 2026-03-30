# V4 Feature Spec: Signal & Drive Weight Adaptation

**Status:** DESIGNED — repositioned within GWT architecture. Activates after
4+ weeks V3 operation.
**Dependency:** Phase 7 (complete), V4 Strategic Reflection
**V3 Groundwork:** signal_weights table (9 signals with bounds), drive_weights table
(4 drives with bounds), signal_weights CRUD (clamped updates), depth_thresholds
table, UrgencyScorer time multiplier curves.
**GWT Integration:** Signal weights feed the ATTEND step's salience competition.
Drive weights provide goal-modulated arbitration (GWT marker #6). Weight
adaptation is calibrated by the LEARN step. See
`docs/architecture/genesis-v4-architecture.md` §8.

---

## What This Is

Signal and drive weight adaptation replaces V3's fixed signal/drive weights with
evidence-driven tuning loops. The Self-Learning Loop observes which signals
actually predict valuable reflections and which drive-aligned actions produce
good outcomes, then adjusts weights accordingly.

Two distinct loops (from the autonomous behavior design):

- **Loop 9 (Drive Weight Loop):** Weeks timescale. Drives shape Reflection Engine
  focus → actions taken → outcomes tracked → positive outcomes on cooperation-driven
  actions → cooperation sensitivity rises → more cooperation actions.
- **Loop 10 (Signal Weight Adaptation):** Days timescale. Signal X triggers
  reflection → reflection produces value (or doesn't) → Self-Learning Loop adjusts
  signal X's weight → signal X is more/less likely to trigger future reflections.

## Current V3 State (Fixed Weights)

### Signal Weights (9 signals)

From `src/genesis/db/schema.py` seed data:

| Signal | Initial Weight | Feeds Depths |
|--------|---------------|--------------|
| conversations_since_reflection | 0.40 | Micro, Light |
| task_completion_quality | 0.50 | Micro, Light |
| outreach_engagement_data | 0.45 | Micro, Deep |
| recon_findings_pending | 0.35 | Light, Deep |
| unprocessed_memory_backlog | 0.30 | Deep |
| budget_pct_consumed | 0.40 | Light, Deep |
| software_error_spike | 0.70 | Micro, Light |
| critical_failure | 0.90 | Light |
| time_since_last_strategic | 0.50 | Strategic |

All signals bounded `[0.0, 1.0]`. CRUD at `src/genesis/db/crud/signal_weights.py`
enforces clamping: `MAX(min_weight, MIN(max_weight, ?))`.

### Drive Weights (4 drives)

| Drive | Initial Weight | Bounds |
|-------|---------------|--------|
| preservation | 0.35 | 0.10–0.50 |
| curiosity | 0.25 | 0.10–0.50 |
| cooperation | 0.25 | 0.10–0.50 |
| competence | 0.15 | 0.10–0.50 |

Drive weights are **independent sensitivity multipliers**, NOT a normalized
budget. Sum=1.0 is coincidental. No drive may drop below 0.10 or rise above
0.50. Used in `src/genesis/surplus/queue.py` for priority calculation.

### Depth Thresholds (4 depths)

| Depth | Threshold | Floor | Ceiling |
|-------|-----------|-------|---------|
| Micro | 0.50 | 30min | 2/hr |
| Light | 0.80 | 6h | 1/hr |
| Deep | 0.55 | 48h | 1/day |
| Strategic | 0.40 | 7d | 1/week |

Scoring pipeline: `raw_score = Σ(signal_value × weight)` → time multiplier →
`final_score >= threshold[depth]` triggers reflection.

### Schema Forward-Compatibility

The signal_weights table already has `last_adapted_at` and `adaptation_notes`
columns (NULL in V3) — ready for V4 adaptation tracking.

## V4 Algorithm: Loop 10 (Signal Weight Adaptation)

```
Every Deep/Strategic reflection:
  1. Record which signals contributed to the trigger (above-threshold signals)
  2. After reflection completes, classify output quality:
     - "actionable": produced observations that were subsequently used
     - "noise": produced observations that were ignored or discarded
     - "mixed": some actionable, some noise
  3. For each contributing signal:
     If reflection was actionable:
       new_weight = current_weight × (1 + learning_rate)  # e.g., learning_rate = 0.05
     If reflection was noise:
       new_weight = current_weight × (1 - learning_rate)
     Clamp to [min_weight, max_weight]
  4. Update signal_weights table with new weight + timestamp + notes
```

**Learning rate:** Start at 0.02 (conservative). Strategic reflection can
propose adjustments.

**Quality classification:** Observation usage tracking already exists in the
Self-Learning Loop (Phase 6). V4 adds a retroactive quality label per
reflection session based on how many of its outputs were acted upon.

## V4 Algorithm: Loop 9 (Drive Weight Adaptation)

```
Every 2 weeks (Strategic reflection cadence):
  1. For each drive, compute outcome quality:
     - Actions aligned with drive D over the past 2 weeks
     - What fraction produced positive outcomes?
  2. Adjustment:
     If positive_outcome_rate > 0.6:
       new_weight = current_weight × (1 + adjustment_rate)
     If positive_outcome_rate < 0.3:
       new_weight = current_weight × (1 - adjustment_rate)
     Else: no change
     Clamp to [0.10, 0.50]
  3. Update drive_weights table
```

**Adjustment rate:** 0.03 per cycle (slower than signal weights — drives are
more fundamental).

**Writer:** The Self-Learning Loop is the **sole writer** to both Loop 9 and
Loop 10 (design doc lines 2231–2239). Strategic reflection can propose
overrides but does not directly modify weights.

## Bounded Self-Adjustment Rule (±20%)

From `genesis-v3-vision.md` line 207:

> System configuration changes: bounded self-adjustment ±20%, larger changes
> proposed.

**Implementation:**
- Track `initial_weight` for every signal and drive
- Any autonomous adjustment must stay within `[initial × 0.8, initial × 1.2]`
- Adjustments beyond ±20% require user approval via Strategic reflection proposal
- Depth threshold adjustments follow the same rule

**Exception:** At L6 autonomy (V5), bounded self-adjustment expands. V4 stays
at L4 — all parameter modifications are propose-and-wait.

## Shadow Mode Requirements

| Prerequisite | Threshold |
|---|---|
| Weeks of V3 data | 4+ weeks |
| Shadow mode period | Required — log proposed adjustments without applying |
| Reflection quality labels | Sufficient actionable/noise classifications |
| User approval | Required before transitioning from shadow to active |

**Shadow mode execution:**
1. Loop 10 computes proposed weight changes after each reflection
2. Logs to observations (type=`weight_proposal`) with proposed vs current values
3. Does NOT update signal_weights or drive_weights tables
4. Strategic reflection reviews proposal history and recommends activation
5. User approves → weights begin updating

## What V4 Must Build

### New Code

1. **`genesis.reflection.weight_adapter` module:**
   - `SignalWeightAdapter` — Loop 10 implementation (per-reflection quality tracking,
     weight update proposals, shadow mode logging)
   - `DriveWeightAdapter` — Loop 9 implementation (2-week outcome aggregation,
     weight update proposals)
   - `WeightProposal` dataclass — proposed change with rationale and evidence

2. **Drive weights CRUD module:**
   - `src/genesis/db/crud/drive_weights.py` — currently missing (only read in
     SurplusQueue). Needs: `get`, `list_all`, `update_weight` (clamped),
     `reset_to_initial`

3. **Reflection quality labeling:**
   - Track which observations from each reflection session were subsequently
     used (acted on, referenced, promoted)
   - Retroactive label: `actionable` / `noise` / `mixed`
   - Store on cc_sessions or as tagged observations

4. **Depth threshold adaptation:**
   - Extend Loop 10 to also propose threshold adjustments
   - If a depth level triggers but consistently produces noise → raise threshold
   - If a depth level rarely triggers but produces high-quality output when
     forced → lower threshold

### Modifications to Existing Code

5. **UrgencyScorer** — record which signals contributed to each trigger
6. **AwarenessLoop** — pass trigger-signal metadata to reflection sessions
7. **OutputRouter** — tag reflection outputs with session ID for quality tracking
8. **StrategicReflectionOrchestrator** — review weight proposals, recommend
   activation/deactivation of adaptation
9. **ReflectionScheduler** — optional: periodic weight proposal review job

## Data Prerequisites

Before enabling adaptation:
- 50+ Deep reflections with quality labels
- 20+ Strategic reflections
- 100+ surplus outreach events with engagement data (for drive weight evidence)
- 4+ weekly self-assessments (input to quality trend analysis)

## Design Constraints

- **Self-Learning Loop is sole writer.** Strategic reflection proposes; the
  learning loop executes. No direct weight modification from reflection output.
- **Drive weights are NOT normalized.** Raising one drive does not lower others.
  The sum can exceed or fall below 1.0.
- **Feature-flag disableable.** If adaptation degrades system quality, revert
  to V3 fixed weights without affecting functionality.
- **Depth thresholds and signal weights are separate concerns.** They interact
  (signals determine score, thresholds gate triggering) but are adapted by
  different evidence (signal relevance vs depth appropriateness).
- **Time multiplier curves are NOT adapted in V4.** These encode urgency
  semantics (overdue work escalates) and should remain fixed unless V5
  meta-learning identifies systematic problems.

## Deferred from Operational Vitals (2026-03-22)

The following items were identified during the Operational Vitals plan review
but deferred to V4 as they require adaptive baseline infrastructure:

- **Adaptive anomaly detection on provider activity metrics**: Detect rate
  changes relative to learned baselines (e.g., "embedding calls dropped from
  50/hr to 0"). Requires user-activity-aware baselines since Genesis activity
  varies with user presence. V3 `ProviderActivityTracker` now provides the raw
  data; V4 adds the anomaly detection layer on top.
- **`last_success_at` field in ProviderActivityTracker**: Timestamp that
  survives rolling window eviction, enabling "haven't succeeded in N hours"
  alerts even during idle periods. More precise than error-rate-only alerting.

## References

- Autonomous behavior design: `docs/architecture/genesis-v3-autonomous-behavior-design.md`
  §Loop 9 (lines 2103–2107), §Loop 10 (lines 2109–2113), §Drive Weighting
  (lines 388–399), §Signal Weight Tiers (lines 722–770)
- Vision: `docs/architecture/genesis-v3-vision.md` §Soft Constraints (line 207)
- Build phases: `docs/architecture/genesis-v3-build-phases.md` §V4 Activation
  (lines 1380–1422)
- Strategic reflection spec: `docs/plans/v4-strategic-reflection-spec.md`
  §MANAGER Outputs (lines 35–41), §Parameter Modification (lines 128–131)
- Model routing registry: `docs/architecture/genesis-v3-model-routing-registry.md`
- Schema: `src/genesis/db/schema.py` (signal_weights lines 96–107, drive_weights
  lines 319–327, depth_thresholds lines 291–299)
- Signal weights CRUD: `src/genesis/db/crud/signal_weights.py`
- Depth thresholds CRUD: `src/genesis/db/crud/depth_thresholds.py`
- Urgency scorer: `src/genesis/awareness/scorer.py`
