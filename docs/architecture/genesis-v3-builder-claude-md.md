# Genesis v3 Builder — CLAUDE.md Section

**Status:** Tracking | **Last updated:** 2026-02-28


> **Purpose:** This content is intended to be added to the CLAUDE.md of the Claude Code instance
> that will build Genesis v3 on the Agent Zero container. Copy the section below into that
> instance's CLAUDE.md.

---

```markdown
# Genesis v3 Build Context

You are building Genesis v3 — an autonomous executive copilot system on the Agent Zero framework.
V3 is the first of three planned versions (V3→V4→V5), each independently complete.

**Your scope is V3 only.** V4/V5 features are intentionally excluded. Do not build them.

## Key Design Documents

All design documents live in `docs/architecture/`:
- `genesis-v3-vision.md` — Core philosophy and identity. READ THIS FIRST. It defines who Genesis
  is and what it aspires to be. Every implementation decision should be consistent with this doc.
- `genesis-v3-build-phases.md` — Safety-ordered build plan with V3/V4/V5 versioning. Your
  roadmap. Build V3 phases in order. Do not skip phases or start one before its dependencies
  are verified.
- `genesis-v3-autonomous-behavior-design.md` — Full architectural reference. Master design doc
  with detailed specifications. Consult for implementation details, schemas, rationale.
- `genesis-v3-dual-engine-plan.md` — Framework decisions, container architecture, migration plan.

## V3 Build Order: Safety First

Phases are ordered safest → riskiest. Verification criteria MUST pass before starting the next.

0. Data Foundation (schemas, MCP stubs) — pure CRUD, no LLM
1. Awareness Loop (5-min tick, signal processing) — programmatic, no LLM
2. Compute Routing (model hierarchy, fallback) — infrastructure plumbing
3. Surplus Infrastructure (queue, staging, daily brainstorms) — free compute from day 1
4. Perception (micro/light reflection) — first LLM calls, low stakes
5. Memory Operations (activation scoring, hybrid retrieval) — extending existing system
6. Learning Fundamentals (outcome classification, procedures) — feedback loops, highest-leverage
7. Simple Deep Reflection (single Sonnet call, journal writes, weekly self-assessment) — "Dream Cycle 2.0"
8. Basic Outreach (alerts/blockers + 1/day surplus, daily morning report, engagement tracking) — user-facing
9. Basic Autonomy (L1-L4 fixed, regression, trust ceilings) — trust management

Phases 1, 2, 3 can be built in parallel. Critical sequential path: 4→5→6→7→8→9.

## What V3 Does NOT Build (Intentionally)

These are V4/V5 features. Do NOT implement them — V3 ships with conservative fixed defaults:
- Meta-prompting protocol (V4) — use static prompt templates
- Strategic reflection / MANAGER / DIRECTOR reviews (V4)
- Signal/drive weight adaptation (V4) — use fixed weights from design doc
- Channel learning (V4) — use config-driven channel preferences
- Procedural confidence decay (V4) — procedures don't decay in V3
- Finding/Insight/Opportunity outreach categories (V4) — V3 only does Blocker/Alert + 1/day surplus
- L5-L7 autonomy (V5) — V3 stops at L4
- Autonomy progression (V5) — levels are fixed, user-managed
- Identity evolution (V5) — static identity
- Anticipatory intelligence (V5) — no "predict what user needs"
- Meta-learning (V5) — no "learn how to learn"

## Critical Principles

- **LLM-first**: Code handles structure (timeouts, validation, wiring). Judgment → LLM.
- **Verify before proceeding**: Each phase has verification criteria. Run them.
- **Simplicity**: 50 lines > 200 lines. Simple heuristic > complex system.
- **Rollback readiness**: Each phase independently disableable via config.
- **3B model = embeddings/extraction ONLY**: CPU-only, must stay responsive. No reflection,
  no reasoning, no surplus. When in doubt, escalate to 20-30B or Gemini.
- **Local model NOT 24/7**: Gemini Flash free tier is the default fallback. Build availability
  detection and automatic failover from day 1.
- **Null hypothesis**: "Current behavior is correct" until evidence says otherwise.
- **Free compute = always run**: Surplus tasks on free compute (local 20-30B, Gemini free)
  run always. Above cost threshold = never for surplus.
- **Daily brainstorms are mandatory**: At least 2/day ("upgrade user" + "upgrade self") on
  free compute, from day 1. These are the last surplus tasks to skip.
- **JOURNAL.md is a workspace file, not a database table**: Narrative self-model that
  creates continuity across reflection sessions. Append-only with periodic consolidation
  by Deep reflection. Keep under ~200 lines active.
- **Morning report is outreach, not infrastructure**: Goes through the same pipeline as
  all outreach (governance, channel selection, engagement tracking).
- **Weekly self-assessment is mandatory**: Fires every week even during quiet periods.
  Uses real data sources, not vague self-evaluation.
```

---

## Notes for the User

This CLAUDE.md section is deliberately concise. The detail lives in the design docs.

### Phase-to-Design-Doc Mapping

| Build Phase | Design Doc Section |
|-------------|-------------------|
| V3 Phase 0 | §4 MCP Servers, §Execution Trace Schema, §Procedural Memory Design |
| V3 Phase 1 | §Layer 1: Awareness Loop, §Signal-Weighted Trigger System |
| V3 Phase 2 | §LLM Weakness Compensation → Pattern 1: Compute Hierarchy |
| V3 Phase 3 | §Cognitive Surplus |
| V3 Phase 4 | §Layer 2: Reflection Engine → Depth Levels (Micro, Light) |
| V3 Phase 5 | §Memory Separation, §What We Learned (A-MEM, ACT-R gaps) |
| V3 Phase 6 | §Layer 3: Self-Learning Loop, §Procedural Memory, §LLM Weakness → Pattern 6 |
| V3 Phase 7 | §Reflection Engine (Deep), §Narrative Self-Model, §Weekly Self-Assessment, current Dream Cycle jobs |
| V3 Phase 8 | §Proactive Outreach, §Daily Morning Report, §Bootstrap / Cold Start Strategy |
| V3 Phase 9 | §Self-Evolving Learning: Autonomy Hierarchy (L1-L4 only) |
| V4 features | §LLM Weakness → Patterns 2-5, §Loop Taxonomy → Tier 3 |
| V5 features | §Autonomy Hierarchy (L5-L7), §Loop Taxonomy → Tier 4 |
| KB (parallel) | `post-v3-knowledge-pipeline.md` in project docs |
