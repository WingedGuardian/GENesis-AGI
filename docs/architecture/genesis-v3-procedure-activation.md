# Procedure Activation Architecture

**Status:** Active | **Last updated:** 2026-06-24


## Problem

Genesis's Phase 6 procedure learning system stores procedures (task_type,
steps, context_tags, confidence) but nothing creates them and nothing
retrieves them at decision points. Learned knowledge doesn't translate
to behavior across sessions.

## Solution: Four Activation Layers

Reliability decreases as scope increases. Layers reinforce each other.

### Layer 1: PreToolUse Procedure Advisor (highest reliability)
- `scripts/procedure_advisor.py` — fires on every CC tool call
- Reads YAML trigger cache (`config/procedure_triggers.yaml`)
- Outputs JSON with `additionalContext` for matching procedures
- Only CORE/ADVISORY-tier procedures with tool triggers (trigger cache built from both)
- ~10ms overhead per non-matching call

### Layer 2: Skill-Embedded Procedures (active when skill invoked)
- Skills contain `## Known Procedures` section
- Procedures synced from ADVISORY-tier via `skills/procedure_sync.py`
- Auto-applied as MINOR changes at L2+ autonomy

### Layer 3: SessionStart Injection (active per session)
- `scripts/genesis_session_context.py` → `session_inject.load_active_procedures`
- Injects CORE-tier procedures only, 200-word budget. **v2** narrowed this
  from LIBRARY+: blind session-start injection runs before the session topic is
  known, so it carries only the most-proven, topic-independent procedures.
- Positioned after cognitive state, before MCP tools hint

### Layer 4: CLAUDE.md / STEERING.md Rule (advisory)
- "Check procedures before multi-step tasks"
- Weakest layer but broadest scope

### Surfacing by tier (v2 re-gating)

Beyond the activation layers, the **proactive memory hook**
(`scripts/proactive_memory_hook.py` → `_search_procedures`) surfaces the single
best embedding match on each user message. v2 gates surfacing so each tier is an
*additive* channel — a higher tier gets everything the lower tiers do, plus one
more surface:

| Tier | recall (MCP) | proactive hook | tool advisor | session inject |
|------|:---:|:---:|:---:|:---:|
| DORMANT  | ✓ |   |   |   |
| LIBRARY  | ✓ | ✓ |   |   |
| ADVISORY | ✓ | ✓ | ✓ |   |
| CORE     | ✓ | ✓ | ✓ | ✓ |

So unproven DORMANT drafts are **recall-only** (never auto-injected), and only the
proven CORE set is injected blindly at session start. The tiers are ranked
CORE > ADVISORY > LIBRARY > DORMANT (CORE = most-proven; see `_TIER_RANK` in
`promoter.py`).

## Procedure Lifecycle

```
Auto-extracted (triage / extractor pipeline):
  → DORMANT (draft=1, success_count=0, conf=0.0, advisory only)
  → LIBRARY (3+ successes, conf >= 0.65, draft=0)
  → ADVISORY (5+ successes, conf >= 0.75, embedded in skills)
  → CORE (8+ successes, conf >= 0.85, tool trigger set)

Explicit user teach (procedure_store MCP tool):
  → LIBRARY (draft=0, success_count=1, conf=2/3) — recallable and
       eligible for proactive-hook surfacing from the moment it is stored.
       Earns further promotion to ADVISORY/CORE organically via record_success.
```

### Reads as a usage signal (effective confidence)

Recorded outcomes are sparse — `record_success`/`record_failure` only fire from
the autonomy executor's task retrospective, never from the dominant
`procedure_recall` path. So a procedure can be recalled (read) dozens of times
yet never earn confidence. To stop that, a **read** (a deliberate
`procedure_recall` that surfaces a procedure, tracked on `invocation_count`) is
treated as a *dampened* positive signal:

- `effective_confidence(success, failure, invocation_count)` folds reads in as
  fractional successes: every `READ_CONFIDENCE_DISCOUNT` (=5) reads count as one
  effective success, with recorded failures as the counterweight.
- **Stored `confidence` stays real Laplace** (success/failure only). Effective
  confidence is *derived* and used ONLY for (a) recall ranking and (b) tier
  promotion — so the j9 metric, quarantine, and demotion stay honest.
- **Hybrid promotion guard:** reads alone may promote to **LIBRARY** (passive
  surfacing); **ADVISORY** (advisory-eligible) requires ≥1 *real* success; **CORE**
  (always-on) is never reachable from reads. Promotion is additive — the target
  tier is the higher of the real-metric and read-eligible tiers (`_compute_tier`
  vs `_read_eligible_tier`), still promote-only.
- **Recall ranking:** `procedure_recall` widens the candidate pool then re-ranks
  by `effective_confidence` (read-heavy procedures surface first; ties keep
  relevance order). Isolated to the recall path — `find_relevant`'s global
  behavior is unchanged (it has 6 callers, incl. autonomy outcome-attribution).
- **Draft clearing:** a draft (`draft=1`) graduates to validated
  (`draft=0`) on its first *real* success with no failures — closing the
  prior gap where nothing ever cleared the flag. Reads alone do not clear the draft flag.

The one-time migration `0035_backfill_procedure_invocation_count` seeds
`invocation_count` from historical `procedure_invoked` events in `eval_events`
so the signal doesn't start at zero.

> **Limitation:** reads are a *proxy* for usefulness, not proof a procedure
> worked. The discount, the ≥1-real-success gate for ADVISORY, and the FailureDetector
> are the counterweights. A real recall-path outcome signal remains future work.

Demotion is **evidence-driven only** — never metric drift:
- 3+ failure-mode hits AND failure_count >= success_count + 3 → tier - 1
- confidence < 0.3 AND total samples >= 3 → quarantine (excluded everywhere)

> **Eval note:** the J-9 system composite's procedure-confidence signal and the
> procedure dimension's `mean_confidence` average **validated** procedures
> (draft=0) only. Draft candidates start at conf≈0 and would measure
> extraction *volume*, not knowledge *quality* (`total_procedures` still counts
> the whole store, consistent with `tier_distribution`).

The `_compute_tier` function in `promoter.py` is strict promote-only: it
returns the highest tier the row's metrics qualify for, but never returns
a lower rank than the row's current tier. A procedure whose confidence
drifts (e.g., CORE dropping from 0.86 → 0.83) is held at its existing tier
unless `_check_demotion` or quarantine fires. This prevents seed and
explicit-teach procedures from being silently downgraded between hourly
promoter runs.

## Ingestion Paths

1. **Triage / extraction pipeline** — extracts procedures from
   APPROACH_FAILURE and WORKAROUND_SUCCESS outcomes via LLM (call site 34),
   plus the per-session extraction + struggle streams. Defaults to DORMANT /
   draft=1 — the LLM hypothesis must earn trust through real
   organic successes before promotion. **Capped at 3 new draft
   procedures per session** (`max_procedures_per_session`, shared across the
   extraction and struggle streams) so a single session cannot flood the store.
   **Scoping gate:** before storage, an LLM classifies each extracted procedure as a
   reusable *task procedure* (how a specific external system works) vs a general
   *behavioral directive* (working-style rules — confidence, due diligence, planning
   cadence — that belong in CLAUDE.md). Directives are not stored, removing the dominant
   near-duplicate source. The gate **fails open** (keeps) on any classifier error, so a
   real procedure is never suppressed (`scoping.py`).
2. **MCP tool** — `procedure_store` for explicit user teaching. Treated
   as one Laplace-equivalent confirmed success — seeds at LIBRARY with
   draft=0, success_count=1, confidence=2/3. The caller asserting
   "this procedure works" is the evidence; the system trusts that
   assertion enough to make the procedure immediately recallable and
   eligible for proactive-hook surfacing.
3. **Seed script** — `scripts/seed_procedures.py` for battle-tested
   procedures. Uses raw SQL upsert with hand-tuned counts and
   confidence (e.g., success_count=10, confidence=0.92, ADVISORY). Bypasses
   the operations / CRUD layer entirely.

## Key Files

| File | Purpose |
|------|---------|
| `src/genesis/learning/procedural/extractor.py` | LLM procedure extraction |
| `src/genesis/learning/procedural/scoping.py` | Scoping gate: keep behavioral directives out of the store |
| `src/genesis/learning/procedural/trigger_cache.py` | YAML cache generation |
| `src/genesis/learning/procedural/session_inject.py` | SessionStart injection |
| `src/genesis/learning/procedural/promoter.py` | Tier promotion/demotion |
| `scripts/procedure_advisor.py` | PreToolUse hook |
| `scripts/seed_procedures.py` | Known procedure seeding |
| `config/procedure_triggers.yaml` | CORE/ADVISORY trigger cache |

## Hook Registration

The PreToolUse advisor hook must be registered in `.claude/settings.json`:
```json
{
  "matcher": ".*",
  "hooks": [{
    "type": "command",
    "command": "${HOME}/agent-zero/.venv/bin/python ${HOME}/genesis/scripts/procedure_advisor.py",
    "timeout": 2000
  }]
}
```
This is a CRITICAL protected path — requires direct CLI or user action.

---

## Related Documents

- [genesis-v3-autonomous-behavior-design.md](genesis-v3-autonomous-behavior-design.md) — Learning and procedure pipeline
- [genesis-v3-build-phases.md](genesis-v3-build-phases.md) — Phase 6: learning fundamentals
