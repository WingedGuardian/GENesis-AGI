# Call Site Audit — 2026-04-09

Comprehensive audit of all 44 neural monitor call sites. Verified against
live code traces, routing config, and last_run database records.

**2026-05-10 update:** Tier 3 — confusable call-site IDs renamed.
- `17_fresh_eyes_review` → `17_executor_review` (executor Gate 2).
- `23_fresh_eyes_review` → `23_outreach_review` (outreach pre-send check).
- `email_triage` → `outreach_email_triage` (disambiguates the triage namespace).
- DB rows in `call_site_last_run` and `deferred_work_queue` are renamed by migration `0015_rename_confusable_call_sites`. `cost_events.metadata` historical entries are intentionally left as-written (audit log fidelity).
- The old IDs survive only as `Renamed from …` annotations in `_call_site_meta.py` and this doc. Historical planning docs under `docs/plans/2026-03-*` reference the old names by design (snapshots in time).

**2026-05-10 update:** Silent-drop fix for keyless call sites.
- Partial API-key configuration is now treated as a first-class normal state, not a load-time filter condition. Previously, the config loader auto-disabled any provider whose API key env var was unset/empty AND filtered those providers out of every chain. Sites whose entire chain was keyless were dropped from `cfg.call_sites` entirely — invisible everywhere downstream (neural monitor, routing API, health snapshot). On a partially-configured install (the normal install state), this masked which sites were unreachable and what env vars would unblock them.
- New behaviour: keyless providers stay registered in `cfg.providers` with `has_api_key=False`. The router skips them in chain walk the same way it skips a tripped breaker — no LiteLLM call, no failure record, no CB trip. The snapshot surfaces `has_api_key=False` + `missing_env_var` on each chain entry; when every entry in a chain is keyless, the site cascades to `status="disabled"` with `status_reason="NO_API_KEYS"`, rendered as a red badge in the neural monitor with a banner naming the env vars (`API_KEY_<TYPE>`) that would enable it.
- `ProviderConfig` gains a `has_api_key: bool = True` field, set at parse time. The `_provider_health()` snapshot helper returns `"disabled"` for `has_api_key=False` (defense-in-depth alongside the existing `probe_status="not_configured"` path that fires once probes have run).
- Sentinel does NOT alert on `NO_API_KEYS` sites — the existing Tier 1 filter for `wired:False / disabled / no last_run` covers it.

**2026-05-10 update:** Routing-config cleanup pass.
- Removed `2_triage` from YAML (awareness loop's `classify_depth()` superseded it; meta entry retained as historical reference).
- Removed `7_task_retrospective` from YAML — confirmed duplicate; the executor went live with `43_task_retrospective` and `7_*` was forgotten.
- Removed `"2_triage"` from `routing/degradation.py:_L3_KEEP` (dead reference after YAML delete).
- Added clarifying comment blocks above `10_cognitive_state`, `18_meta_prompting`, `22_tagging`, `28_observation_sweep`, `37_infrastructure_monitor` in YAML.
- `37_infrastructure_monitor` recommendation changed from "wire" to "temp disabled pending rework" — commit ff2198c (2026-05-03) deliberately disabled it; rework requires higher-tier providers + tighter prompts.
- `10_cognitive_state` status clarified: this YAML entry is V4 placeholder; cognitive state IS actively maintained today via direct DB writes (awareness/loop.py, cc/reflection_bridge*.py), not this call site.
- Cleaned up ghost reference: removed "Supersedes 15_triage_calibration" from `30_triage_calibration` meta description. `15_triage_calibration` exists nowhere in the live code.
- Added `status_reason` + `see_also` metadata fields to confusable-family entries; neural monitor renders these as a colored badge + cross-reference section.
- Added master cross-reference docstring to `_call_site_meta.py` documenting the four confusable families (triage, fresh_eyes_review, task_retrospective, ego_compaction) and ~25 cross-reference comments at call-site reference points.

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
| `23_outreach_review` | Outreach message cross-vendor review | Per outreach |
| `29_retrospective_triage` | Re-evaluate past triage after outcome | Per outcome |
| `30_triage_calibration` | Update triage rules (supersedes 15) | Weekly |
| `31_outcome_classification` | Classify task outcomes for learning | Per outcome |
| `32_delta_assessment` | Cognitive state snapshot deltas | Daily |
| `35_content_draft` | Draft content for platforms | On demand |
| `36_code_auditor` | Surplus codebase review | Idle time |
| `cc_update_analysis` | Analyze CC version changes | On CC update |
| `outreach_email_triage` | Weekly email batch filter | Per email batch |

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

| Site | Superseded By | Status |
|------|--------------|--------|
| `2_triage` | Awareness loop's `classify_depth()` (deterministic threshold math, no LLM) | **REMOVED FROM YAML 2026-05-10** |
| `15_triage_calibration` | `30_triage_calibration` | Ghost ID — was never in `_call_site_meta.py` or current YAML; meta description reference cleaned 2026-05-10 |

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
| `7_task_retrospective` | Task outcome root-cause analysis | **REMOVED FROM YAML 2026-05-10** — duplicate; executor went live with `43_task_retrospective` (`autonomy/executor/trace.py:24`). Meta entry retained as historical. |
| `autonomous_executor_reasoning` | Executor non-tooling reasoning | **Keep** — wire when executor goes live |

### Never Wired — Planned Features (8)

| Site | Intended Purpose | Recommendation |
|------|-----------------|----------------|
| `10_cognitive_state` | LLM regeneration of cognitive state narrative | **Keep for V4** — note: cognitive state is *actively* maintained today via direct DB writes (`awareness/loop.py`, `cc/reflection_bridge*.py`); this YAML entry reserves the chain for the future LLM-summary feature, not the live mechanism |
| `17_executor_review` | Executor cross-vendor review (Gate 2, paid) | **Active** — distinct from `23_outreach_review` (outreach pre-send, free) |
| `18_meta_prompting` | Pre-reflection prompt engineering | **Keep** for V4 adaptive prompting |
| `22_tagging` | Entity extraction / metadata tagging | **Keep** for V4 knowledge graph |
| `28_observation_sweep` | Environment change scanning | **Functionally replaced by awareness loop** signal collection (`awareness/loop.py:perform_tick`). Kept in YAML with comment; can remove on next sweep. |
| `34_research_synthesis` | Multi-source research synthesis | **Keep** for V4 autonomous research |
| `37_infrastructure_monitor` | Surplus infrastructure monitoring | **TEMPORARILY DISABLED** (commit ff2198c, 2026-05-03). Sentinel infra (`src/genesis/sentinel/monitor.py`) intact. Disabled because free-model output quality was too low for surplus dispatch. Rework pending: needs higher-tier providers + tighter prompts. |

### Never Wired — Routing Duplicates (3)

| Site | What Routes Instead | Recommendation |
|------|--------------------|----------------|
| `19_outreach_draft` | Outreach uses `35_content_draft` directly | **Remove** or **wire** if outreach needs own chain |
| `20_adversarial_counterargument` | Function defined but never called | **Keep** for V4 decision quality |
| `contingency_deep_reflection` | Never triggered; contingency_foreground handles it | **Review** — may be redundant with foreground contingency |

## Not Duplicates (Confirmed Distinct)

| Pair | Distinction |
|------|------------|
| `17_executor_review` vs `23_outreach_review` | 17 = autonomy executor (cross-vendor, paid). 23 = outreach message review (free). Different domains. |
| `29_retrospective_triage` vs `30_triage_calibration` | 29 = re-evaluate past decisions (learning). 30 = update calibration rules. Both active. |

## Cleanup Recommendations

1. ~~**Remove** `2_triage` from routing config~~ **DONE 2026-05-10** (also removed from `routing/degradation.py:_L3_KEEP`).
2. `15_triage_calibration` — ghost ID; meta description reference cleaned 2026-05-10. Nothing else to do.
3. ~~**Review** `8_memory_consolidation` — remove if consolidation doesn't need LLM~~ **DONE 2026-04-11**: renamed to `8_ego_compaction`, verified as a real LLM call site via EgoCompactor.
4. ~~**Wire** `37_infrastructure_monitor` to surplus scheduler~~ **REVISED 2026-05-10**: temp disabled in commit ff2198c (2026-05-03); rework pending higher-tier providers + tighter prompts.
5. ~~**Review** `28_observation_sweep` — awareness loop may fully replace it~~ **CONFIRMED 2026-05-10**: functionally replaced; kept in YAML with clarifying comment.
6. **Review** `contingency_deep_reflection` — may be redundant
7. **Review** `19_outreach_draft` — may be redundant with 35_content_draft
8. **2026-05-10**: removed `7_task_retrospective` from YAML (duplicate; live one is `43_task_retrospective`).
