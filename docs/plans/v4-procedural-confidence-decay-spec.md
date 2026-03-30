# V4 Feature Spec: Procedural Confidence Decay

**Status:** DESIGNED — not yet implemented. Requires 50+ procedures with age variance.
**Dependency:** Phase 6 (complete), Phase 7 (complete — quarantine mechanism)
**V3 Groundwork:** procedural_memory table (confidence, success_count, failure_count,
last_used, created_at columns), Laplace smoothing formula, quarantine mechanism
(Phase 7), maturity model (early/growing/mature stages).

---

## What This Is

Procedural confidence decay prevents stale knowledge from persisting at high
confidence indefinitely. V3 uses Laplace smoothing — a static estimator based
only on success/failure counts, with no time dimension. A procedure that worked
perfectly 6 months ago but hasn't been used since retains its high confidence
forever. V4 adds exponential time decay so unused procedures gradually lose
confidence, requiring reinforcement through successful use to stay relevant.

This is **distinct from quarantine** (Phase 7). Quarantine is binary exclusion
based on low success rate. Decay is continuous confidence reduction based on
elapsed time. They coexist: a procedure can decay to low confidence AND be
quarantined if it also has poor success rates.

## Current V3 State

### Laplace Smoothing Formula

From `src/genesis/learning/procedural/operations.py` (lines 40–46, 63–69, 117–126):

```python
confidence = (success_count + 1) / (success_count + failure_count + 2)
```

Properties:
- No time dimension — purely statistical
- Converges to true success rate with sufficient data
- Starts at 0.5 with zero observations (uninformative prior)
- 1 success, 0 failures → 2/3 ≈ 0.667
- 0 successes, 1 failure → 1/3 ≈ 0.333

### Quarantine Mechanism (Phase 7)

From `src/genesis/reflection/stability.py` (lines 21–23):

| Parameter | Value |
|-----------|-------|
| Min uses for quarantine | 3 |
| Max success rate for quarantine | 40% |

Quarantine is **binary** (on/off flag), detected weekly in quality calibration,
and excludes procedures from retrieval but does not delete them.

### Procedural Memory Schema

From `src/genesis/db/schema.py` (lines 12–36), confidence-related columns:

```sql
confidence        REAL NOT NULL DEFAULT 0.0,
success_count     INTEGER NOT NULL DEFAULT 0,
failure_count     INTEGER NOT NULL DEFAULT 0,
last_used         TEXT,           -- ISO datetime, NULL if never used
last_validated    TEXT,           -- ISO datetime
created_at        TEXT NOT NULL,
quarantined       INTEGER NOT NULL DEFAULT 0  -- Phase 7 addition
```

### Current Retrieval Scoring

From `src/genesis/learning/procedural/matcher.py` (line 38):

```python
score = row["confidence"] * overlap
```

Where `overlap` is Jaccard similarity of context tags. No time factor.

### Maturity Model

From `src/genesis/learning/types.py` (lines 59–62):

| Stage | Threshold | Implication for Decay |
|-------|-----------|----------------------|
| EARLY | < 50 procedures | Decay disabled or very slow (preserve learning) |
| GROWING | 50–200 procedures | Decay enabled at standard rate |
| MATURE | > 200 procedures | Decay may accelerate (more competition for relevance) |

## V4 Algorithm: Exponential Decay with Floor

From `docs/architecture/genesis-v3-dual-engine-plan.md` (lines 708–713):

```
Design: exponential decay with floor
  confidence *= decay_rate per week
  min floor = 0.1 (prevents deletion)
```

### Full Algorithm

```python
def compute_decayed_confidence(
    base_confidence: float,    # Laplace-smoothed value
    last_used: datetime | None,
    created_at: datetime,
    now: datetime,
    decay_rate: float = 0.95,  # 5% loss per week
    min_floor: float = 0.1,
) -> float:
    """Apply exponential time decay to procedure confidence.

    Uses last_used if available, otherwise created_at as the reference point.
    """
    reference = last_used or created_at
    elapsed_seconds = (now - reference).total_seconds()
    weeks = elapsed_seconds / (7 * 24 * 3600)

    decay_factor = decay_rate ** weeks
    decayed = base_confidence * decay_factor

    return max(min_floor, decayed)
```

### Decay Curve (at 0.95/week, starting confidence 0.80)

| Weeks Since Use | Decayed Confidence | Status |
|----------------|--------------------|--------|
| 0 | 0.800 | Fresh |
| 4 | 0.654 | Active |
| 8 | 0.534 | Staling |
| 12 | 0.436 | Low |
| 16 | 0.356 | Very low |
| 26 | 0.210 | Near floor |
| 52 | 0.100 | At floor (min) |

### Reinforcement (Counter-Decay)

Each successful use resets the decay clock:
1. `record_success()` updates `last_used` to now
2. Laplace smoothing recalculates base confidence upward
3. Decay factor resets to 1.0 (weeks_since_use = 0)

Each failed use also resets the clock but confidence drops via Laplace:
1. `record_failure()` updates `last_used` to now
2. Laplace smoothing recalculates base confidence downward
3. Decay factor resets to 1.0

**Net effect:** Actively-used procedures maintain confidence. Unused procedures
decay. Procedures that are used but fail drop via both Laplace and eventual
decay if abandoned.

## Integration with Existing Systems

### Retrieval Scoring (Matcher)

Current: `score = confidence * overlap`

V4: `score = decayed_confidence * overlap`

The decay computation happens at **query time** (not via periodic batch job).
This is simpler, always current, and avoids the need for a scheduled decay cron.

### Quarantine Interaction

Decay and quarantine are complementary:

| Scenario | Quarantine | Decayed Confidence | Result |
|----------|------------|-------------------|--------|
| Recent, successful | No | High (0.7+) | Normal retrieval |
| Recent, failing | Yes (if <40% rate) | Medium-high | Excluded by quarantine |
| Old, no recent use | No | Low (approaching floor) | Retrieved but deprioritized |
| Old, failing | Yes | Very low | Excluded by quarantine AND low score |
| Old, recently revived | No | Refreshed (high) | Normal retrieval |

### Maturity-Gated Activation

Decay should be gentle or disabled during EARLY maturity (< 50 procedures):
- The system is still learning; premature decay erases scarce knowledge
- Once GROWING (50+), decay activates at the standard rate
- At MATURE (200+), decay may optionally accelerate (`decay_rate = 0.93`)
  to increase competition for relevance

## What V4 Must Build

### New Code

1. **`genesis.learning.procedural.decay` module:**
   - `compute_decayed_confidence()` — pure function (algorithm above)
   - `DecayConfig` dataclass — `decay_rate`, `min_floor`, `maturity_overrides`
   - `should_apply_decay(maturity_stage) -> bool` — maturity gate

2. **Decay tuning data collection (shadow mode):**
   - Log what confidence values WOULD be with decay applied
   - Compare: would decayed scores have produced better retrieval rankings?
   - Metric: did procedures that were actually used have higher decayed scores
     than those that weren't?

### Modifications to Existing Code

3. **`procedural/matcher.py`** — apply `compute_decayed_confidence()` at query time
4. **`procedural/operations.py`** — ensure `last_used` is always updated on
   `record_success()` and `record_failure()` (already done, but verify)
5. **`reflection/stability.py`** — quarantine candidate detection should use
   decayed confidence, not raw confidence, for threshold comparison
6. **ContextGatherer** — include decay statistics in Deep reflection context
   (how many procedures near floor, how many recently reinforced)

### Schema Changes

None required. All necessary columns (`last_used`, `created_at`, `confidence`)
already exist. Decay is computed at query time, not stored.

**Optional:** Add `confidence_decayed REAL` computed column or view for
dashboard/reporting purposes.

## Data Prerequisites

| Prerequisite | Threshold |
|---|---|
| Procedure corpus size | 50+ procedures with varying ages |
| Age variance | Procedures spanning 2+ months of creation dates |
| Usage data | `last_used` populated for most procedures |
| Shadow mode | Required — track what would change before applying |

## Open Design Questions

From `genesis-v3-autonomous-behavior-design.md` line 3021:

> "Procedural memory confidence decay: How does confidence decay without creating
> amnesia? Deferred — known to be complex, needs its own design session."

Key questions to resolve during V4 implementation:

1. **Decay rate tuning:** Is 0.95/week right? Need V3 data to calibrate.
2. **Access-count weighting:** Should procedures used 100 times decay slower
   than those used 3 times? (Current design: no, only last_used matters.)
3. **Domain-specific rates:** Should technical procedures (likely stable) decay
   slower than social/contextual ones (likely to become stale)?
4. **Floor value:** Is 0.1 the right floor? Too low and procedures are
   effectively invisible. Too high and truly stale knowledge competes unfairly.
5. **Amnesia prevention:** How to distinguish "unused because irrelevant" from
   "unused because opportunity hasn't arisen"? The latter shouldn't decay.

## Design Constraints

- **Query-time computation, not batch job.** Decay is a pure function of
  `(base_confidence, last_used, now)`. No periodic update needed. This
  eliminates scheduling complexity and ensures values are always current.
- **Laplace smoothing preserved.** Decay multiplies the Laplace-smoothed
  confidence, not the raw counts. The statistical estimate remains the
  foundation; decay adds a time dimension on top.
- **Feature-flag disableable.** If decay causes retrieval quality problems,
  revert to V3 static confidence without data loss.
- **No procedure deletion.** The floor (0.1) ensures procedures are never
  invisible. They can be revived by successful use at any time.
- **Maturity gate is non-negotiable.** Do not decay early-stage knowledge.
  The system needs to accumulate before it can afford to forget.

## References

- Dual engine plan: `docs/architecture/genesis-v3-dual-engine-plan.md`
  §Confidence Decay (lines 708–713)
- Autonomous behavior design: `docs/architecture/genesis-v3-autonomous-behavior-design.md`
  §Procedural Memory Confidence (lines 614, 692, 735–736), §Open Design
  Questions (line 3021)
- Build phases: `docs/architecture/genesis-v3-build-phases.md` §V3 Scope
  (line 779), §V4 Activation (line 1419)
- CLAUDE.md: §V3 Scope Fence (line 193)
- Procedural operations: `src/genesis/learning/procedural/operations.py`
- Procedural matcher: `src/genesis/learning/procedural/matcher.py`
- Procedural maturity: `src/genesis/learning/procedural/maturity.py`
- Stability monitor: `src/genesis/reflection/stability.py`
- Schema: `src/genesis/db/schema.py` (procedural_memory lines 12–36)
