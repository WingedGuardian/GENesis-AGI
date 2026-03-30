# Behavioral Immune System — Design Exploration

**Date:** 2026-03-27
**Status:** Design thinking, pre-implementation
**Scope:** V4+
**Origin:** "Built without wiring" incident — Genesis built Bayesian regression code
with zero production callers and called wiring "a separate concern," despite CLAUDE.md
explicitly prohibiting this. Exposed that system prompt rules are the weakest form of
behavioral steering.

---

## Problem Statement

Genesis has behavioral rules in CLAUDE.md, STEERING.md, and memory files. These rules
are context tokens that compete for attention with everything else in the prompt. When
the context is large and the task is complex, individual rules get ~2% of behavioral
weight. The model "knows" the rule but doesn't always follow it.

Adding more rules makes each existing rule weaker (attention dilution). The current
approach doesn't scale.

**Core question:** How do we build a behavioral correction system where the right amount
of force is applied to the right problem, proportionally and adaptively?

---

## Fundamental Constraint

Without fine-tuning or activation steering (literal vector modifications to the model's
hidden states at inference time), every mechanism operates through the model's attention
mechanism. We can influence probability distributions but can't force attention.

This means: we optimize the structure and delivery of behavioral signals, not the
fundamental mechanism. The question is whether structural improvements produce
qualitatively better compliance than a longer system prompt.

---

## Treatment Arsenal

### 1. PreToolUse Hooks (Hard Gates)
- **What:** Scripts that run before tool calls. Can BLOCK the action.
- **Token cost:** Zero when not triggered. ~100-200 tokens when they fire.
- **Strength:** Strongest available. External to the model. Cannot be reasoned past.
- **Coverage:** Limited to pattern-matchable violations in tool calls. Can't catch
  reasoning-level patterns directly. LLM-powered judgment hooks expand coverage
  but reintroduce the attention problem for the reviewer.
- **Examples:** Behavioral linter (Write/Edit), pip-editable guard, YouTube blocker.
- **Key property:** Should be the FIRST treatment considered, not the last escalation.
  Zero ongoing cost makes this strictly cheaper than any context injection.

### 2. Stop Hooks (Pre-Response Checks)
- **What:** Scripts that run before final response delivery.
- **Token cost:** Variable. ~100-300 tokens when triggered.
- **Strength:** Moderate. Can flag issues before the user sees them. Lighter than
  a full critic agent.
- **Coverage:** Response-level patterns. Can check for specific language or reasoning
  patterns in the about-to-be-delivered response.

### 3. Memory Files (Retrieval-Based)
- **What:** Files in CC memory, indexed by MEMORY.md, surfaced by proactive hook.
- **Token cost:** ~20 tokens always (index line). ~100-500 tokens when surfaced.
- **Strength:** Weakest on its own. Only surfaces if retrieval system finds it relevant.
- **Coverage:** Any behavioral pattern. Quality depends on embedding match.
- **Best for:** First-time corrections, low severity, building the pattern library.

### 4. Vivid Case Studies (Enhanced Memory Files)
- **What:** Detailed narrative of a specific violation: what happened, what was said,
  what the fix was, why it mattered.
- **Token cost:** ~200-500 tokens when retrieved. Zero when not.
- **Strength:** Moderate-high. Models respond significantly more strongly to specific
  examples than abstract rules. Narrative format engages deeper processing.
- **Coverage:** Specific behavioral patterns with documented history.
- **Best for:** Enhancing rules that exist but aren't being followed.

### 5. STEERING.md Rules
- **What:** Hard constraints in SOUL.md injection. Behavioral guardrails.
- **Token cost:** ~50-100 tokens per rule, every session, every message.
- **Strength:** Moderate. Framed as "MUST NOT violate."
- **Difference from CLAUDE.md:** STEERING.md is behavioral (how to think). CLAUDE.md
  is engineering discipline (how to work). STEERING.md is personal to the user-Genesis
  relationship. CLAUDE.md is shared/repo-level.
- **Best for:** Confirmed behavioral patterns needing persistent vigilance.

### 6. CLAUDE.md Rules
- **What:** Project instructions checked into the repo.
- **Token cost:** ~100-200 tokens per rule, every session, every message. CLAUDE.md
  is already ~4000 tokens.
- **Strength:** Moderate. Well-established convention but subject to attention dilution.
- **Coverage:** Engineering discipline, process rules.
- **Best for:** Rules that apply to ALL sessions including autonomous ones.

### 7. Code Reviewer Agent
- **What:** Separate model invocation reviewing code changes.
- **Token cost:** High (~10-50K tokens per review).
- **Strength:** High for code-level violations. Structural separation (separate context).
- **Coverage:** Code quality, architectural patterns, wiring verification.
- **Best for:** Already in use. Code-specific behavioral enforcement.

### 8. Pre-Response Critic Agent
- **What:** Separate model invocation reviewing response before delivery.
- **Token cost:** Highest. Doubles inference cost per message.
- **Strength:** Highest for reasoning-level violations. Full structural separation.
- **Coverage:** Can catch anything expressible as a rule, including reasoning patterns.
- **Best for:** Chronic, severe reasoning-level violations that survive all other treatments.
  Last resort.

---

## Decision Tree

Treatment selection is based on the NATURE of the problem, not its severity.
Severity determines escalation WITHIN a branch, not which branch to use.

```
New behavioral correction arrives
  |
  +-> Can a hook catch this at the action boundary?
  |     |
  |     YES -> Create hook. Done. Cheapest possible treatment.
  |            (Escalation within: basic pattern match -> LLM-powered hook)
  |
  +-> NO: reasoning-level pattern. Is it contextual or universal?
        |
        +-> CONTEXTUAL (only relevant in certain situations)
        |     -> Retrieval-based: memory file + proactive hook
        |        (Escalation: basic memory -> vivid case study -> enhanced retrieval weight)
        |
        +-> UNIVERSAL (always relevant)
              -> Is severity x frequency high enough for always-in-context tokens?
                 |
                 YES -> STEERING.md (behavioral) or CLAUDE.md (engineering)
                 NO  -> Stay at retrieval level, monitor
```

---

## Escalation Levels (within each branch)

### Hook Branch
- L0: Simple pattern match hook
- L1: LLM-powered judgment hook (higher coverage, adds reviewer inference cost)

### Retrieval Branch
- L0: Basic memory file. Surface on retrieval match.
- L1: Vivid case study with specific examples, quotes, consequences.
- L2: Multiple case studies. Enhanced retrieval weight (boost in proactive hook).
- L3: Promote to always-in-context (move to Universal branch).

### Universal Branch
- L0: Concise STEERING.md rule
- L1: STEERING.md rule + paired case study in retrieval
- L2: CLAUDE.md rule (applies to autonomous sessions too)
- L3: CLAUDE.md rule + hook enforcement + case study (maximum treatment)

---

## Demotion Strategy (Immune Memory Model)

Principle: don't delete, demote. Active immunity -> passive immunity.

**Demotion order — cheapest things stay longest:**
1. Remove CLAUDE.md rule first (costs tokens every message). Keep hook (costs nothing).
2. If hook fires after rule removal -> rule was helping. Re-add it.
3. If hook stays quiet -> behavior has genuinely stopped. Rule was redundant.
4. Only demote hooks if they're actively misfiring (catching legitimate behavior).
5. Never fully delete. Archive to unindexed file (zero cost, recoverable).

**Safeguards:**
- Demotion proposed by system, approved by user (or by reflection with user review).
- If demotion causes recurrence, re-escalate to one level ABOVE prior level (penalty).
- Hooks almost never worth demoting — zero cost when they don't fire.

**Review cadence:** Reflection engine audits treatment registry periodically (monthly?).
Surfaces: "These treatments haven't been triggered in N sessions. Propose demotion?"

---

## Detection: Recognizing Behavioral Corrections

The system must infer from tone and language when the user is giving a strong behavioral
correction. Users don't use explicit commands — they say "stop doing that" or express
frustration about a repeated pattern.

**Signals:**
- Strong negative sentiment directed at Genesis's behavior (not external frustration)
- Imperative language: "stop," "never," "don't," "quit"
- Reference to repeated patterns: "again," "keep doing," "every time"
- Emotional intensity markers

**Key distinction:** Correction about Genesis's behavior vs. frustration about an
external problem. The correction is about something Genesis *did* or *failed to do*.

**Approach:** Same sentiment analysis signals the outreach system uses. LLM-assessed
severity scoring on each detected correction.

---

## Theme Detection (Open Research Question)

How to recognize that two corrections are about the same underlying behavioral pattern?

**Proposed approach: Vector similarity + LLM validation**
1. Embed new correction text + context
2. Search existing corrections by cosine similarity in Qdrant
3. Above threshold (~0.75) -> candidate match to existing theme
4. LLM validates: "Are these about the same behavioral pattern?"
5. Below threshold -> new theme

**Risks:**
- Over-consolidation: similar topics but different reasoning failures get merged
- Under-consolidation: same pattern with different surface language gets split
- LLM validation compensates for embedding imprecision but isn't perfect

**Infrastructure:** Existing Qdrant + embedding provider + retrieval pipeline.
New: behavioral correction collection, theme-clustering logic.

**Needs research:** How are professional LLM researchers handling behavioral correction
clustering? What does the bleeding edge of Constitutional AI, RLHF/DPO, and
representation engineering say about this problem?

---

## Treatment Registry (Database)

```sql
-- Raw correction observations
CREATE TABLE behavioral_corrections (
    id              TEXT PRIMARY KEY,
    raw_user_text   TEXT NOT NULL,
    context         TEXT NOT NULL,    -- what Genesis was doing when corrected
    severity        REAL NOT NULL,    -- LLM-assessed 0.0-1.0
    theme_id        TEXT,             -- FK to behavioral_themes (null if unthemed)
    embedding_id    TEXT,             -- Qdrant point ID for similarity search
    created_at      TEXT NOT NULL
);

-- Clustered behavioral patterns
CREATE TABLE behavioral_themes (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,    -- e.g., "built_without_wiring"
    description       TEXT NOT NULL,    -- human-readable theme description
    correction_count  INTEGER DEFAULT 0,
    last_correction_at TEXT,
    created_at        TEXT NOT NULL
);

-- Active treatments per theme
CREATE TABLE behavioral_treatments (
    id                TEXT PRIMARY KEY,
    theme_id          TEXT NOT NULL,    -- FK to behavioral_themes
    treatment_type    TEXT NOT NULL,    -- hook, steering_rule, claude_rule,
                                       -- memory_file, case_study, critic
    treatment_ref     TEXT NOT NULL,    -- path to hook script, memory file, etc.
    level             INTEGER NOT NULL, -- current escalation level within branch
    branch            TEXT NOT NULL,    -- hook, retrieval, universal
    status            TEXT NOT NULL DEFAULT 'active',  -- active, demoted, archived
    violation_count   INTEGER DEFAULT 0,
    last_violation_at TEXT,
    last_adjusted_at  TEXT,
    adjustment_history TEXT NOT NULL DEFAULT '[]',  -- JSON array of changes
    created_at        TEXT NOT NULL
);
```

The `adjustment_history` JSON records the full treatment timeline:
```json
[
  {"action": "created", "level": 0, "at": "2026-03-27T..."},
  {"action": "escalated", "level": 1, "reason": "recurrence", "at": "2026-03-28T..."},
  {"action": "demoted", "level": 0, "reason": "no violations in 30 sessions", "at": "2026-04-28T..."},
  {"action": "re-escalated", "level": 2, "reason": "recurrence after demotion", "at": "2026-04-29T..."}
]
```

This timeline is what the LLM reasons over when assessing treatment efficacy.

---

## Failure Modes to Design Against

1. **Over-sensitivity** — Every minor correction triggers escalation. System becomes
   paranoid, hedges everything, asks permission constantly. The pathology of preservation
   unchecked.

2. **Attention bloat** — Escalated rules accumulate. Eventually 30 vivid case studies
   compete for attention at higher token cost. Back to the same dilution problem.
   Mitigation: aggressive demotion of universal-branch rules, preference for
   hook-branch solutions.

3. **Theme misclassification** — Over-consolidation (different failures merged) or
   under-consolidation (same pattern split). Undermines frequency tracking.
   Mitigation: LLM validation step, user review of theme assignments.

4. **Self-reinforcing rigidity** — System so focused on not doing X that it over-corrects.
   "Don't call things out of scope" becomes "try to wire every hypothetical feature."
   The immune system becomes autoimmune. Mitigation: the LLM assessing treatments
   should watch for over-correction signals.

5. **Premature demotion** — Removing the treatment that's actually preventing the
   behavior. Mitigation: demote cheap things last, track what happens after demotion,
   auto-re-escalate on recurrence.

6. **Immune system misfires** — Hooks catching legitimate behavior, theme assignments
   drifting. Mitigation: user is ultimate debugger, system surfaces treatment data
   (costs, firing rates, efficacy) for inspection.

---

## Integration Points

- **Observation system** — Behavioral corrections create a new observation type
- **Reflection engine** — Periodic treatment audit, theme review, demotion proposals
- **Proactive memory hook** — Delivery vehicle for retrieval-based treatments
- **PreToolUse hooks** — Hard gate treatments
- **Drive weights** — Severity x frequency could influence drive weight adaptation
- **Outreach system** — Sentiment detection signals reusable for correction detection
- **Learning pipeline** — Outcome classification feeds into efficacy measurement

---

## Open Questions

1. **Theme clustering threshold** — What cosine similarity threshold balances
   over/under-consolidation? Needs empirical tuning.
2. **External research** — What does the LLM behavioral correction literature say?
   Constitutional AI extensions, representation engineering, RLHF/DPO approaches.
3. **Efficacy measurement** — How to distinguish "treatment is working" from "situation
   stopped arising"? Demotion experiments are the most rigorous approach but imperfect.
4. **Autoimmune prevention** — How to detect and correct over-correction?
5. **Severity calibration** — LLM-assessed severity needs grounding. What's the scale?
   How do we prevent severity inflation?
6. **Interaction with V3 scope fence** — This is V4+ work. What V3 groundwork can be
   laid without crossing the fence?

---

## Relationship to Existing Systems

This system doesn't replace CLAUDE.md, STEERING.md, or memory files. It provides the
intelligence layer that decides WHICH mechanism to use for each behavioral issue, tracks
whether treatments are working, and proposes escalation/demotion.

The existing mechanisms are the medicine. This system is the doctor.
