"""Call site metadata — descriptions, categories, cost policy, wiring status.

Sourced from docs/architecture/genesis-v3-model-routing-registry.md and
verified against live code traces (2026-04-05 audit).

Fields:
  description: What this does + when it runs (1-2 sentences)
  category:    Subsystem grouping for neural monitor ring/color
  frequency:   How often this fires
  cost_policy: Auto-derived for most; manual for CC-dispatched sites
  dispatch:    "cc" (CC background only), "dual" (API-first, CC fallback)
  cc_model:    Which CC model (Haiku/Sonnet/Opus) for dispatch=cc/dual
  wired:       False if config exists but no code invokes this call site
  model_tier:  What class of model this needs (embedding/slm/mid/frontier/cc)
"""

from __future__ import annotations

_CALL_SITE_META: dict[str, dict] = {
    # ── WIRED: actively executing, have last_run records ──────────────
    "3_micro_reflection": {
        "description": "Fast pattern check on the latest signals. Runs every awareness tick using free models.",
        "category": "reflection",
        "frequency": "Every 5 min",
        "cost_policy": "Free primary",
        "model_tier": "slm",
    },
    "4_light_reflection": {
        "description": "Assesses flagged signals when urgency is elevated. Free API chain with CC/Haiku fallback.",
        "category": "reflection",
        "frequency": "On elevated urgency",
        "cost_policy": "Free only (never pays)",
        "dispatch": "dual",
        "cc_model": "Haiku",
        "model_tier": "slm",
    },
    "5_deep_reflection": {
        "description": "Journal-quality analysis of patterns, trends, and user context. Dispatched via CC background session (Sonnet).",
        "category": "reflection",
        "frequency": "Weekly + high urgency",
        "cost_policy": "CC background (Sonnet)",
        "dispatch": "cc",
        "cc_model": "Sonnet",
        "model_tier": "cc",
    },
    "6_strategic_reflection": {
        "description": "Quarterly-depth strategic analysis and long-term planning. Dispatched via CC background session (Opus).",
        "category": "reflection",
        "frequency": "4-8/month",
        "cost_policy": "CC background (Opus)",
        "dispatch": "cc",
        "cc_model": "Opus",
        "model_tier": "cc",
    },
    "8_ego_compaction": {
        "description": "Ego-internal rolling summary compactor. Folds old ego_cycles outputs into a single compacted_summary in ego_state so long-running ego memory stays bounded. NOT Genesis-wide memory consolidation — that pipeline (dream cycle) is not yet built. Only caller: ego/compaction.py (inert until ego sessions go live).",
        "category": "processing",
        "frequency": "Daily",
        "model_tier": "slm",
    },
    "9_fact_extraction": {
        "description": "Extracts structured facts (entities, dates, relationships) from unstructured input during ingestion.",
        "category": "processing",
        "frequency": "Per ingestion",
        "model_tier": "slm",
    },
    "11_user_model_synthesis": {
        "description": "LLM-narrative user knowledge synthesis. Every 48h the user_model_evolver job in runtime/init/learning.py calls router.route_call('11_user_model_synthesis', ...) with the current model dict + recent delta evidence, and writes the narrative to USER_KNOWLEDGE.md. Falls back to rules-based dict rendering when all free providers are exhausted.",
        "category": "reasoning",
        "frequency": "Every 48h (via user_model_evolution job)",
        "model_tier": "slm",
    },
    "12_surplus_brainstorm": {
        "description": "Brainstorm sessions during idle compute. Uses reflection pipeline (Depth.LIGHT) with free APIs. Tracks separately from light reflections.",
        "category": "content",
        "frequency": "Opportunistic",
        "cost_policy": "Free only (never pays)",
        "model_tier": "slm",
    },
    "13_morning_report": {
        "description": "Compiles overnight system health, observations, and pending items into a daily morning report.",
        "category": "content",
        "frequency": "Daily",
        "model_tier": "slm",
    },
    "14_weekly_self_assessment": {
        "description": "Honest self-evaluation of Genesis's recent performance. Dispatched via CC background session (Sonnet).",
        "category": "reasoning",
        "frequency": "Weekly",
        "cost_policy": "CC background (Sonnet)",
        "dispatch": "cc",
        "cc_model": "Sonnet",
        "model_tier": "cc",
    },
    "16_quality_calibration": {
        "description": "Audits recent LLM outputs for quality regression and consistency. Dispatched via CC background session (Sonnet).",
        "category": "calibration",
        "frequency": "Weekly",
        "cost_policy": "CC background (Sonnet)",
        "dispatch": "cc",
        "cc_model": "Sonnet",
        "model_tier": "cc",
    },
    "21_embeddings": {
        "description": "Write-path: embeds text via local Ollama before storing to Qdrant. Uses qwen3-embedding (0.6B).",
        "category": "embedding",
        "frequency": "On memory store",
        "cost_policy": "Free (local)",
        "model_tier": "embedding",
    },
    "21b_query_embedding": {
        "description": "Read-path: embeds search queries via cloud provider for Qdrant recall. DeepInfra-primary, DashScope/Ollama fallback.",
        "category": "embedding",
        "frequency": "On memory recall",
        "cost_policy": "Cloud embedding API",
        "model_tier": "embedding",
    },
    "23_fresh_eyes_review": {
        "description": "Cross-vendor review of outreach messages before sending. Uses free models for independent second opinion.",
        "category": "assessment",
        "frequency": "Per outreach message",
        "model_tier": "slm",
    },
    "29_retrospective_triage": {
        "description": "Re-evaluates past triage decisions after the outcome is known, feeding the learning pipeline.",
        "category": "classification",
        "frequency": "Per outcome",
        "model_tier": "slm",
    },
    "30_triage_calibration": {
        "description": "Updates triage calibration rules using local/paid models. Supersedes 15_triage_calibration.",
        "category": "calibration",
        "frequency": "Weekly",
        "model_tier": "mid",
    },
    "31_outcome_classification": {
        "description": "Classifies task outcomes (success/partial/failure) for the learning and retrospective pipeline.",
        "category": "processing",
        "frequency": "Per outcome",
        "model_tier": "mid",
    },
    "32_delta_assessment": {
        "description": "Assesses changes between cognitive state snapshots to detect drift and track evolution.",
        "category": "processing",
        "frequency": "Daily",
        "model_tier": "mid",
    },
    "34_research_synthesis": {
        "description": "Synthesizes multi-source research results into concise summaries. Called by research orchestrator.",
        "category": "content",
        "frequency": "On demand",
        "model_tier": "slm",
    },
    "35_content_draft": {
        "description": "Drafts content for Telegram, email, and other platforms via ContentDrafter. Free models.",
        "category": "content",
        "frequency": "On demand",
        "model_tier": "slm",
    },
    "36_code_auditor": {
        "description": "Surplus task: reviews codebase for bugs, quality issues, and improvement opportunities during idle time.",
        "category": "surplus",
        "frequency": "Opportunistic (idle time)",
        "model_tier": "mid",
    },
    "cc_update_analysis": {
        "description": "Analyzes Claude Code version changelogs for impact on Genesis integrations and hooks.",
        "category": "processing",
        "frequency": "On CC update detection",
        "model_tier": "slm",
    },
    "email_triage": {
        "description": "Gemini-primary light filter for weekly email batch. Classifies emails by relevance and urgency.",
        "category": "classification",
        "frequency": "Per email batch",
        "cost_policy": "Free only (never pays)",
        "model_tier": "slm",
    },
    "bookmark_enrichment": {
        "description": "Generates rich summaries of shelved sessions for bookmarks. Routes through 33_skill_refiner chain (free APIs). Runs during surplus idle time.",
        "category": "content",
        "frequency": "On bookmark enrichment",
        "model_tier": "slm",
    },
    "embedding_recovery": {
        "description": "Drains pending FTS5-only memories to Qdrant when embedding provider recovers. Background recovery worker.",
        "category": "embedding",
        "frequency": "On provider recovery",
        "model_tier": "embedding",
    },
    # ── PARTIALLY WIRED: code exists, conditions haven't triggered yet ─
    "27_pre_execution_assessment": {
        "description": "Sanity-checks proposed task execution plans before committing resources. Triggers from autonomous executor.",
        "category": "reasoning",
        "frequency": "Per task",
        "model_tier": "frontier",
    },
    "33_skill_refiner": {
        "description": "Proposes improvements to Genesis's learned skills based on recent outcomes. Part of the learning pipeline.",
        "category": "content",
        "frequency": "Periodic",
        "model_tier": "slm",
    },
    "38_procedure_extraction": {
        "description": "Extracts reusable procedures from successful interaction patterns. Part of the learning pipeline.",
        "category": "content",
        "frequency": "On demand",
        "model_tier": "slm",
    },
    "contingency_foreground": {
        "description": "API-based foreground conversation fallback when CC is rate-limited or unavailable.",
        "category": "reasoning",
        "frequency": "On CC rate limit",
        "model_tier": "frontier",
    },
    "contingency_micro": {
        "description": "Free API-based Micro reflection fallback when CC is rate-limited or unavailable. Used only for cheap periodic awareness-loop ticks where free SLMs are acceptable.",
        "category": "processing",
        "frequency": "On CC rate limit",
        "cost_policy": "Free only (never pays)",
        "model_tier": "slm",
    },
    # ── GROUNDWORK: config exists, no code invokes them yet ───────────
    "2_triage": {
        "description": "Disabled. Original threshold-based triage. Superseded by awareness loop's built-in classification.",
        "category": "classification",
        "frequency": "Every 5 min",
        "model_tier": "slm",
        "wired": False,
    },
    "7_ego_cycle": {
        "description": "CC-based ego cycle reasoning session. Dispatches via CC background when ego cadence triggers.",
        "category": "reasoning",
        "frequency": "Per ego cycle",
        "cost_policy": "CC background (Sonnet)",
        "dispatch": "cc",
        "cc_model": "Sonnet",
        "model_tier": "cc",
        "wired": False,
    },
    "7_ego_cycle_api": {
        "description": "Disabled. API-based ego cycle reasoning. Ego sessions built but inert until beta.",
        "category": "reasoning",
        "frequency": "Per ego cycle",
        "model_tier": "frontier",
        "wired": False,
    },
    "7_task_retrospective": {
        "description": "Root-cause classification of completed executor tasks. Retrospective phase wired in executor. Activates when executor goes live.",
        "category": "processing",
        "frequency": "Per task",
        "model_tier": "slm",
        "wired": False,
    },
    "10_cognitive_state": {
        "description": "Disabled. Compressed cognitive state summary regeneration. Planned for V4.",
        "category": "reasoning",
        "frequency": "Daily",
        "model_tier": "frontier",
        "wired": False,
    },
    "17_fresh_eyes_review": {
        "description": "Cross-vendor quality review of executor deliverables (Gate 2). Wired into executor pipeline. Activates when executor goes live.",
        "category": "assessment",
        "frequency": "Per major decision",
        "model_tier": "frontier",
        "wired": False,
    },
    "18_meta_prompting": {
        "description": "Disabled. Pre-reflection prompt engineering. Planned for V4 adaptive prompting.",
        "category": "calibration",
        "frequency": "Per reflection",
        "model_tier": "slm",
        "wired": False,
    },
    "20_adversarial_counterargument": {
        "description": "Devil's advocate review of executor deliverables (Gate 3). Wired into executor pipeline. Activates when executor goes live.",
        "category": "assessment",
        "frequency": "Per major decision",
        "model_tier": "frontier",
        "wired": False,
    },
    "22_tagging": {
        "description": "Disabled. Entity extraction and metadata tagging. Planned for V4 knowledge graph.",
        "category": "classification",
        "frequency": "Per input",
        "model_tier": "slm",
        "wired": False,
    },
    "28_observation_sweep": {
        "description": "Disabled. Environment scanning for noteworthy changes. Awareness loop handles this directly.",
        "category": "processing",
        "frequency": "Per awareness tick",
        "model_tier": "frontier",
        "wired": False,
    },
    "37_infrastructure_monitor": {
        "description": "Disabled. Surplus-scheduled infrastructure trend detection. Probes built but not wired to surplus scheduler.",
        "category": "surplus",
        "frequency": "Periodic (surplus scheduler)",
        "model_tier": "slm",
        "wired": False,
    },
    "outreach_fallback": {
        "description": "Deferred outreach delivery retry. Enqueue path active (pipeline.py:335), but consumer NOT BUILT — deferred messages silently marked completed without retry. CRITICAL BUG: needs consumer implementation.",
        "category": "content",
        "frequency": "On outreach failure",
        "model_tier": "slm",
    },
    "autonomous_executor_reasoning": {
        "description": "Non-tooling reasoning for autonomous executor steps. Wired into executor engine. Activates when executor goes live.",
        "category": "reasoning",
        "frequency": "Per executor step",
        "model_tier": "frontier",
        "wired": False,
    },
}
