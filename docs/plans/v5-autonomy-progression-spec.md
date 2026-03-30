# V5 Feature Spec: Autonomy Progression (L5–L7)

**Status:** DESIGNED — not yet implemented. Requires 6+ months V4 operational data.
**Dependency:** V4 Strategic Reflection, V4 Signal/Drive Weight Adaptation
**V3 Groundwork:** autonomy_state table (levels 1–7 in schema, L1–L4 active),
autonomy CRUD (13 functions including regression logic), 2-correction regression
rule, context_ceiling field, `GROUNDWORK(skill-autonomy-graduation)` in
SkillApplicator.
**GWT Integration:** Autonomy levels feed the SELECT step — what the workspace
controller is allowed to approve depends on per-category autonomy permissions.
V5 adds first-principles GWT components (coalition mechanism, learned SELECT
model, adaptive cadence, meta-learning). See
`docs/architecture/genesis-v4-architecture.md` §7.

---

## What This Is

V3 stops at L4 autonomy (proactive outreach). V5 extends to L5 (system
configuration), L6 (learning modification), and L7 (identity evolution). Each
level grants progressively more self-modification authority, with correspondingly
stricter evidence requirements and user oversight.

The core tension: a system that can improve itself is more capable, but a system
that modifies itself without oversight is dangerous. V5 resolves this through
graduated trust with mandatory check-ins, regression triggers, and context-
dependent ceilings.

## Current Autonomy Hierarchy (V3)

From `docs/architecture/genesis-v3-autonomous-behavior-design.md` (lines 1628–1650):

| Level | Name | Action Types | V3 Status |
|-------|------|-------------|-----------|
| L1 | Simple Tool Use | Search, read, compute, basic tool invocation | Active |
| L2 | Known Pattern Execution | Executing procedures that worked before | Active |
| L3 | Novel Task Execution | New tasks not in procedural memory | Active |
| L4 | Proactive Outreach | Unsolicited messaging, suggestions | Active |
| L5 | System Configuration | Adjusting thresholds, weights, parameters | **V5** |
| L6 | Learning Modification | Adjusting review schedules, calibration | **V5** |
| L7 | Identity Evolution | Proposing SOUL.md changes | **V5** |

### Schema (Already Supports L1–L7)

From `src/genesis/db/schema.py` (lines 144–161):

```sql
CREATE TABLE IF NOT EXISTS autonomy_state (
    id                      TEXT PRIMARY KEY,
    person_id               TEXT,           -- GROUNDWORK(multi-person)
    category                TEXT NOT NULL,
    current_level           INTEGER NOT NULL DEFAULT 1
                            CHECK (current_level BETWEEN 1 AND 7),
    earned_level            INTEGER NOT NULL DEFAULT 1
                            CHECK (earned_level BETWEEN 1 AND 7),
    context_ceiling         TEXT CHECK (context_ceiling IN (
        'direct_session', 'background_cognitive', 'sub_agent', 'outreach', NULL
    )),
    consecutive_corrections INTEGER NOT NULL DEFAULT 0,
    total_successes         INTEGER NOT NULL DEFAULT 0,
    total_corrections       INTEGER NOT NULL DEFAULT 0,
    last_correction_at      TEXT,
    last_regression_at      TEXT,
    regression_reason       TEXT,
    updated_at              TEXT NOT NULL
)
```

The schema already allows levels 1–7. The L4 cap is architectural (the code
that awards L5+ doesn't exist), not a runtime constraint.

### Regression Logic (Implemented)

From `src/genesis/db/crud/autonomy.py` (lines 91–118):

```python
if new_consecutive >= 2:
    new_level = max(1, row["current_level"] - 1)  # Drop 1 level, floor at 1
    new_consecutive = 0
    regression_reason = f"2 consecutive corrections at {corrected_at}"
```

Tests at `tests/test_db/test_autonomy.py` (13 tests) verify:
- 2 consecutive corrections → drop one level
- Counter resets after regression
- Success resets consecutive counter
- Cannot regress below L1

## V5 Levels: Detailed Design

### L5 — System Configuration

**Action Types:**
- Adjusting awareness loop thresholds (depth_thresholds table)
- Modifying signal weights (within ±20% bounds)
- Adjusting drive weights (within ±20% bounds)
- Tuning decay rates, salience thresholds, timing parameters

**Default behavior:** Propose only, user approves.

**Grows to:** Can self-adjust bounded parameters (±20% within session) after
high confidence. Changes beyond bounds still require approval.

**Key constraint:** Cannot restructure fundamental mechanisms (e.g., replace
the urgency scorer algorithm) — only tune parameters within existing mechanisms.

### L6 — Learning System Modification

**Action Types:**
- Adjusting review schedules (reflection frequency, calibration timing)
- Modifying salience calibration thresholds
- Tuning drive weight adaptation rates
- Adjusting quarantine thresholds
- Modifying engagement heuristic parameters

**Default behavior:** Propose only, **always user review** — even after high
confidence. L6 is never fully autonomous.

**Grows to:** Bounded self-adjustment (±20%) possible for minor tuning. All
fundamental changes always need approval.

**Rationale for permanent oversight:** Learning modifications affect HOW the
system learns. A feedback loop where the learning system modifies its own
learning rules can diverge. Human oversight breaks potential divergence cycles.

### L7 — Identity Evolution (SOUL.md Changes)

**Action Types:**
- Proposing changes to core values, drives, weaknesses in SOUL.md
- Suggesting new identity dimensions
- Recommending priority reordering

**Default behavior:** Draft only, user decides. **Never autonomous.**

**Grows to:** Maximum proposal authority. Can articulate sophisticated
proposals with evidence and impact analysis. But the user always decides.

**Rationale:** Identity is the foundation that everything else builds on.
A system that autonomously modifies its own identity can drift in ways that
are invisible until they're severe. The user must remain the ultimate
authority on who Genesis is.

## Evidence Requirements for Level Advancement

From the autonomous behavior design (lines 1644–1650):

### Advancement Criteria

| Requirement | L4→L5 | L5→L6 | L6→L7 |
|-------------|-------|-------|-------|
| Successful executions at current level | 20+ | 30+ | 50+ |
| Consecutive corrections at current level | 0 in last 4 weeks | 0 in last 8 weeks | 0 in last 12 weeks |
| Weeks of operation at current level | 8+ | 12+ | 24+ |
| User explicit acknowledgment | Required | Required | Required |
| Shadow mode at next level | 4 weeks | 8 weeks | N/A (L7 is always shadow) |

**Note:** Exact thresholds are designed but may be tuned during V5 implementation
based on V4 operational data. The principle is clear: higher levels require
exponentially more evidence.

### "Silence ≠ Approval" Rule

From the autonomous behavior design (line 1650):

> The system should periodically ask: "I've been handling [category]
> autonomously for [period] with [X% success rate]. Would you like me to
> continue, or do you want to adjust my autonomy for this?"

**Implementation:**
- Monthly check-in per active autonomy category
- Delivered via morning report or direct outreach
- If user does not explicitly confirm within 7 days, autonomy pauses for
  that category (drops to propose-only) until confirmation
- Check-in content includes: category, current level, success rate,
  notable actions taken, any near-misses

## Regression Triggers

From the autonomous behavior design (lines 1652–1659):

| Trigger | Effect | Recovery |
|---------|--------|----------|
| 2 consecutive corrections at a level | Drop one level, require re-earning | Standard advancement criteria |
| 1 user-reported harmful action | Drop to L1 for that category, full re-earn | Requires explicit user restoration |
| System detects systematic error (e.g., 5 ignored outreach in a row) | Self-proposes regression for that category | Standard advancement criteria |
| Monthly check-in not confirmed | Pause to propose-only | User confirms to resume |

## Context-Dependent Trust Ceilings

From the autonomous behavior design (lines 1661–1678):

| Context | Max Effective Autonomy | Rationale |
|---------|------------------------|-----------|
| Direct user session | Earned level (no cap) | User present, can intervene |
| Background cognitive | L3 | No user in loop, keep reversible |
| Sub-agent (irreversible) | L2 | Sub-agents inherit task perms, not global |
| Sub-agent (reversible) | Earned level | Reversible actions are safe |
| Outreach | L2 until calibrated | Wrong outreach erodes trust faster |

**Key principle:** Context restricts but never expands effective autonomy.
A system with L6 earned autonomy still caps at L2 for irreversible sub-agent
actions in background tasks.

## What V5 Must Build

### New Code

1. **`genesis.autonomy.advancement` module:**
   - `AdvancementEvaluator` — checks evidence requirements per category
   - `AdvancementProposal` dataclass — category, current level, proposed level,
     evidence summary, confidence assessment
   - `ShadowModeTracker` — tracks shadow performance at next level

2. **`genesis.autonomy.checkin` module:**
   - `AutonomyCheckIn` — monthly check-in generator
   - Integrates with morning report and outreach pipeline
   - Tracks confirmation status per category
   - Pauses autonomy on non-confirmation

3. **`genesis.autonomy.l5_config` module:**
   - `ParameterModificationExecutor` — applies approved parameter changes
   - Enforces ±20% bounds
   - Logs all modifications with before/after values
   - Rollback capability (store previous values)

4. **`genesis.autonomy.l6_learning` module:**
   - `LearningModificationProposer` — generates learning system change proposals
   - Always requires user review (never auto-applies)
   - Impact analysis: "if I change X, here's what I predict will happen"

5. **`genesis.autonomy.l7_identity` module:**
   - `IdentityProposalDrafter` — generates SOUL.md change proposals
   - Evidence-backed: "based on N interactions, I believe [change] because..."
   - Diff format: shows exactly what would change in SOUL.md
   - Always draft-only, never applies

6. **`AUTONOMY_CHECKIN.md`** — prompt template for monthly check-ins

### Modifications to Existing Code

7. **`autonomy.py` CRUD** — add `advance_level()`, `record_shadow_outcome()`,
   `pause_for_checkin()`, `resume_after_checkin()`
8. **Pre-Execution Assessment** — add L5/L6/L7 action classification
9. **Strategic reflection** — include autonomy state in MANAGER inputs,
   propose level advancements based on evidence
10. **Governance gate** — enforce context ceilings for L5+ actions
11. **ReflectionScheduler** — add monthly check-in job

### GROUNDWORK Already in Place

From `src/genesis/learning/skills/applicator.py` (line 18):

```python
# GROUNDWORK(skill-autonomy-graduation): autonomy_state category skill_evolution, starts L2
_DEFAULT_AUTONOMY_LEVEL = 2
```

This demonstrates the pattern: each capability domain has its own autonomy
category with independent level tracking. V5 extends this to system config,
learning modification, and identity.

## Design Constraints

- **L7 is never autonomous.** Identity evolution is always propose-and-wait.
  No amount of evidence or operational history changes this.
- **L6 is always reviewed.** Learning modifications affect learning itself.
  Bounded self-adjustment (±20%) is possible, but fundamental changes always
  need user approval.
- **Context ceilings are non-negotiable.** A background task cannot escalate
  beyond L3 regardless of earned level. The user's presence is the safety net.
- **Regression is aggressive.** 2 corrections → drop level. 1 harmful action →
  drop to L1. The cost of regression is low (re-earn with evidence). The cost
  of unchecked autonomy failure is high.
- **Monthly check-ins are mandatory.** The system cannot assume continued
  approval from past approval. Autonomy is an ongoing grant, not a permanent
  award.
- **Per-category independence.** L5 for task execution does not imply L5 for
  outreach. Each domain earns autonomy independently based on its own track
  record.
- **Feature-flag per level.** L5, L6, L7 can be independently enabled/disabled
  without affecting lower levels.

## References

- Autonomous behavior design: `docs/architecture/genesis-v3-autonomous-behavior-design.md`
  §Autonomy Hierarchy (lines 1628–1678), §Context Ceilings (lines 1661–1678),
  §Regression (lines 1652–1659), §Meta-Learning Spiral 15 (line 2161)
- Vision: `docs/architecture/genesis-v3-vision.md` §Soft Constraints (line 207)
- Build phases: `docs/architecture/genesis-v3-build-phases.md` §V5 Activation
- Strategic reflection spec: `docs/plans/v4-strategic-reflection-spec.md`
  §Parameter Modification (lines 128–131)
- CLAUDE.md: §V3 Scope Fence (line 195)
- Schema: `src/genesis/db/schema.py` (autonomy_state lines 144–161)
- Autonomy CRUD: `src/genesis/db/crud/autonomy.py`
- Autonomy tests: `tests/test_db/test_autonomy.py` (13 tests)
- Skill applicator GROUNDWORK: `src/genesis/learning/skills/applicator.py` (line 18)
