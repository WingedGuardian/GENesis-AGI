# Call Site Audit — 2026-04-09

Comprehensive audit of all 44 neural monitor call sites. Verified against
live code traces, routing config, and last_run database records.

**2026-04-09 update:** Call site `11_user_model_synthesis` was originally
marked wired (commit eb5c350, 2026-04-06) when the surrounding pipeline
got wired, but the actual `router.route_call()` invocation was missing —
the synthesis path used pure-Python rules-based dict rendering instead.
This was caught during the Sentinel spam investigation: ghost-down status
on call site 11 (because Anthropic providers report unreachable without an
ANTHROPIC_API_KEY) was waking the Sentinel every 5 minutes. The fix:
(1) actually wire the LLM call in `runtime/init/learning.py` via
`UserModelEvolver.synthesize_narrative()` with a free-first chain
(mistral-small → groq → gemini → openrouter), (2) teach the call_sites
snapshot to distinguish "not_configured" providers from "unreachable" ones,
(3) skip alerts for `disabled` and `wired=False` sites in `health_alerts()`.
Call site 11 now does what its name says.

## Summary

| Category | Count | Status |
|----------|-------|--------|
| Wired (actively executing) | 20 | Green |
| Partially wired (conditions not yet triggered) | 5 | Green (standby) |
| Groundwork / disabled | 16 | Gray (idle) |
| Non-routing (CC core) | 3 | Separate status |
| **Total** | **44** | |

## Wired (19) — Actively Executing

These call sites have `last_run` records and are confirmed invoked in the
runtime path.

| Site | What It Does | Trigger |
|------|-------------|---------|
| `11_user_model_synthesis` | LLM narrative synthesis of user model → USER_KNOWLEDGE.md | Every 48h |
| `13_morning_report` | Daily morning report via ContentDrafter | Daily |
| `3_micro_reflection` | Fast pattern check on signals | Every awareness tick |
| `4_light_reflection` | Flagged signal assessment (dual: API + CC/Haiku) | Elevated urgency |
| `5_deep_reflection` | Journal-quality analysis (CC/Sonnet) | Weekly + high urgency |
| `6_strategic_reflection` | Strategic planning (CC/Opus) | 4-8/month |
| `9_fact_extraction` | Structured facts from unstructured input | Per ingestion |
| `12_surplus_brainstorm` | Creative exploration during idle time | Opportunistic |
| `14_weekly_self_assessment` | Self-evaluation (CC/Sonnet) | Weekly |
| `16_quality_calibration` | Output quality audit (CC/Sonnet) | Weekly |
| `21_embeddings` | 1024-dim vectors for Qdrant | On memory write |
| `23_fresh_eyes_review` | Outreach message cross-vendor review | Per outreach |
| `29_retrospective_triage` | Re-evaluate past triage after outcome | Per outcome |
| `30_triage_calibration` | Update triage rules (supersedes 15) | Weekly |
| `31_outcome_classification` | Classify task outcomes for learning | Per outcome |
| `32_delta_assessment` | Cognitive state snapshot deltas | Daily |
| `35_content_draft` | Draft content for platforms | On demand |
| `36_code_auditor` | Surplus codebase review | Idle time |
| `cc_update_analysis` | Analyze CC version changes | On CC update |
| `email_triage` | Weekly email batch filter | Per email batch |

## Partially Wired (5) — Code Exists, Not Yet Triggered

These have runtime code paths but the triggering conditions haven't occurred
yet (e.g., CC rate limit, executor activation).

| Site | What It Does | Why Not Triggered |
|------|-------------|-------------------|
| `27_pre_execution_assessment` | Pre-task sanity check | Executor decomposer not yet active |
| `33_skill_refiner` | Skill improvement proposals | Learning pipeline partial |
| `38_procedure_extraction` | Extract reusable procedures | Learning pipeline partial |
| `contingency_foreground` | API foreground when CC rate-limited | CC hasn't hit rate limit |
| `contingency_micro` | API Micro reflection when CC rate-limited | CC hasn't hit rate limit |

## Groundwork / Disabled (17)

Config exists in `model_routing.yaml` but no code invokes these call sites.
Shown as gray "Disabled" in the neural monitor.

### Superseded (2)

| Site | Superseded By | Recommendation |
|------|--------------|----------------|
| `2_triage` | Awareness loop built-in classification | **Remove** from routing config |
| `15_triage_calibration` | `30_triage_calibration` (Phase 6, Mar 9) | **Remove** from routing config |

### Routing Bypass (0)

`8_memory_consolidation` was previously listed here. As of 2026-04-11 it has
been renamed to `8_ego_compaction` and verified as a real route_call site
(`src/genesis/ego/compaction.py:319` calls `router.route_call("8_ego_compaction", ...)`).
It is not a routing bypass; it routes normally via the mistral-large-free
chain. Zero live calls because its only caller (EgoCompactor) is in the ego
subsystem, which is inert until beta per CLAUDE.md. This call site is
ego-internal rolling-summary compaction — NOT Genesis-wide memory consolidation
(dream cycle), which remains unbuilt.

### Never Wired — Ego/Executor (3)

| Site | Intended Purpose | Recommendation |
|------|-----------------|----------------|
| `7_ego_cycle` | Ego cycle CLI reasoning | **Active** — persistent session with --resume |
| `7_task_retrospective` | Task outcome root-cause analysis | **Keep** — wire when executor goes live |
| `autonomous_executor_reasoning` | Executor non-tooling reasoning | **Keep** — wire when executor goes live |

### Never Wired — Planned Features (8)

| Site | Intended Purpose | Recommendation |
|------|-----------------|----------------|
| `10_cognitive_state` | Cognitive state summary regeneration | **Keep** for V4 |
| `17_fresh_eyes_review` | Executor cross-vendor review | **Keep** — distinct from 23_fresh_eyes |
| `18_meta_prompting` | Pre-reflection prompt engineering | **Keep** for V4 adaptive prompting |
| `22_tagging` | Entity extraction / metadata tagging | **Keep** for V4 knowledge graph |
| `28_observation_sweep` | Environment change scanning | **Review** — awareness loop may make this redundant |
| `34_research_synthesis` | Multi-source research synthesis | **Keep** for V4 autonomous research |
| `37_infrastructure_monitor` | Surplus infrastructure monitoring | **Wire** to surplus scheduler |

### Never Wired — Routing Duplicates (3)

| Site | What Routes Instead | Recommendation |
|------|--------------------|----------------|
| `19_outreach_draft` | Outreach uses `35_content_draft` directly | **Remove** or **wire** if outreach needs own chain |
| `20_adversarial_counterargument` | Function defined but never called | **Keep** for V4 decision quality |
| `contingency_deep_reflection` | Never triggered; contingency_foreground handles it | **Review** — may be redundant with foreground contingency |

## Not Duplicates (Confirmed Distinct)

| Pair | Distinction |
|------|------------|
| `17_fresh_eyes_review` vs `23_fresh_eyes_review` | 17 = autonomy executor (cross-vendor, paid). 23 = outreach message review (free). Different domains. |
| `29_retrospective_triage` vs `30_triage_calibration` | 29 = re-evaluate past decisions (learning). 30 = update calibration rules. Both active. |

## Cleanup Recommendations (Separate Task)

1. **Remove** `2_triage` and `15_triage_calibration` from routing config
2. ~~**Review** `8_memory_consolidation` — remove if consolidation doesn't need LLM~~ **DONE 2026-04-11**: renamed to `8_ego_compaction`, verified as a real LLM call site via EgoCompactor.
3. **Wire** `37_infrastructure_monitor` to surplus scheduler
4. **Review** `28_observation_sweep` — awareness loop may fully replace it
5. **Review** `contingency_deep_reflection` — may be redundant
6. **Review** `19_outreach_draft` — may be redundant with 35_content_draft

These are documentation-only recommendations. No code changes in this PR.
