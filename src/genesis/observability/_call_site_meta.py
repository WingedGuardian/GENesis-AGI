"""Call site metadata — descriptions, categories, cost policy.

Sourced from docs/architecture/genesis-v3-model-routing-registry.md.
Served via the health API so the neural monitor doesn't hardcode descriptions.
"""

from __future__ import annotations

_CALL_SITE_META: dict[str, dict[str, str]] = {
    "2_triage": {
        "description": "Classify signals → ignore/log/micro/light/deep/critical",
        "category": "classification",
        "frequency": "Every 5 min",
        "cost_policy": "Free primary",
    },
    "3_micro_reflection": {
        "description": "Quick pattern check on recent signals",
        "category": "reflection",
        "frequency": "Every 5 min",
        "cost_policy": "Free primary",
    },
    "4_light_reflection": {
        "description": "Quick assessment of flagged signals",
        "category": "reflection",
        "frequency": "On elevated urgency",
        "cost_policy": "Paid primary",
    },
    "5_deep_reflection": {
        "description": "Journal-quality analysis of patterns and trends",
        "category": "reflection",
        "frequency": "Weekly + high urgency",
        "cost_policy": "CC background (Sonnet)",
    },
    "6_strategic_reflection": {
        "description": "Quarterly-depth strategic analysis",
        "category": "reflection",
        "frequency": "4-8/month",
        "cost_policy": "CC background (Opus)",
    },
    "7_task_retrospective": {
        "description": "Root-cause classification of completed tasks",
        "category": "processing",
        "frequency": "Per task",
        "cost_policy": "Paid (outsized impact)",
    },
    "8_memory_consolidation": {
        "description": "Deduplicate, compress, merge related memories",
        "category": "processing",
        "frequency": "Daily",
        "cost_policy": "Free primary",
    },
    "9_fact_extraction": {
        "description": "Pull structured facts from unstructured input",
        "category": "processing",
        "frequency": "Per ingestion",
        "cost_policy": "Free primary",
    },
    "10_cognitive_state": {
        "description": "Regenerate compressed cognitive state summary",
        "category": "reasoning",
        "frequency": "Daily",
        "cost_policy": "Paid primary",
    },
    "11_user_model_synthesis": {
        "description": "Update user preference/behavior model",
        "category": "reasoning",
        "frequency": "Weekly",
        "cost_policy": "CC background (Sonnet)",
    },
    "12_surplus_brainstorm": {
        "description": "Creative exploration using idle compute",
        "category": "content",
        "frequency": "Opportunistic",
        "cost_policy": "Free only (never pays)",
    },
    "13_morning_report": {
        "description": "Compile overnight observations into morning report",
        "category": "content",
        "frequency": "Daily",
        "cost_policy": "Free primary",
    },
    "14_weekly_self_assessment": {
        "description": "Honest evaluation of own performance",
        "category": "reasoning",
        "frequency": "Weekly",
        "cost_policy": "CC background (Opus)",
    },
    "15_triage_calibration": {
        "description": "Calibrate triage accuracy (shapes most frequent call)",
        "category": "calibration",
        "frequency": "Weekly",
        "cost_policy": "Paid (outsized impact)",
    },
    "16_quality_calibration": {
        "description": "Audit recent outputs for quality regression",
        "category": "calibration",
        "frequency": "Weekly",
        "cost_policy": "CC background (Opus)",
    },
    "17_fresh_eyes_review": {
        "description": "Cross-vendor review of reasoning",
        "category": "assessment",
        "frequency": "Per major decision",
        "cost_policy": "Paid (cross-vendor)",
    },
    "18_meta_prompting": {
        "description": "Determine what expensive models should think about",
        "category": "calibration",
        "frequency": "Per deep/strategic reflection",
        "cost_policy": "Paid (outsized impact)",
    },
    "19_outreach_draft": {
        "description": "Draft surplus insight / blocker / alert messages",
        "category": "content",
        "frequency": "Per outreach",
        "cost_policy": "Free primary",
    },
    "20_adversarial_counterargument": {
        "description": "Devil's advocate review (must be different vendor)",
        "category": "assessment",
        "frequency": "Per major decision",
        "cost_policy": "Paid (cross-vendor)",
    },
    "21_embeddings": {
        "description": "Generate 1024-dim vectors for Qdrant memory",
        "category": "embedding",
        "frequency": "On write",
        "cost_policy": "Free (Ollama)",
    },
    "22_tagging": {
        "description": "Entity extraction and metadata tagging",
        "category": "classification",
        "frequency": "Per input",
        "cost_policy": "Free primary",
    },
    "23_fresh_eyes_review": {
        "description": "Additional cross-vendor review of code decisions",
        "category": "assessment",
        "frequency": "Per decision",
        "cost_policy": "Free primary",
    },
    "27_pre_execution_assessment": {
        "description": "Sanity check before task execution",
        "category": "reasoning",
        "frequency": "Per task",
        "cost_policy": "CC foreground",
    },
    "28_observation_sweep": {
        "description": "Scan environment for noteworthy changes",
        "category": "processing",
        "frequency": "Per awareness tick",
        "cost_policy": "Paid primary",
    },
    "29_retrospective_triage": {
        "description": "Re-evaluate triage decisions after outcome is known",
        "category": "classification",
        "frequency": "Per outcome",
        "cost_policy": "Free primary",
    },
    "30_triage_calibration": {
        "description": "Secondary triage calibration with local models",
        "category": "calibration",
        "frequency": "Weekly",
        "cost_policy": "Paid primary",
    },
    "31_outcome_classification": {
        "description": "Classify task outcomes for learning pipeline",
        "category": "processing",
        "frequency": "Per outcome",
        "cost_policy": "Paid primary",
    },
    "32_delta_assessment": {
        "description": "Assess changes between cognitive state snapshots",
        "category": "processing",
        "frequency": "Daily",
        "cost_policy": "Paid primary",
    },
    "33_skill_refiner": {
        "description": "LLM-driven skill improvement proposals",
        "category": "content",
        "frequency": "Periodic",
        "cost_policy": "Free primary",
    },
    "34_research_synthesis": {
        "description": "Synthesize multi-source search results",
        "category": "content",
        "frequency": "On demand",
        "cost_policy": "Free primary",
    },
    "35_content_draft": {
        "description": "Draft content for various platforms",
        "category": "content",
        "frequency": "On demand",
        "cost_policy": "Free primary",
    },
    "36_code_auditor": {
        "description": "Evaluate codebase for bugs and quality issues",
        "category": "surplus",
        "frequency": "Opportunistic (idle time)",
        "cost_policy": "Free only (never pays)",
    },
    "37_infrastructure_monitor": {
        "description": "Proactive infrastructure health — trend detection and forecasting",
        "category": "surplus",
        "frequency": "Periodic (surplus scheduler)",
        "cost_policy": "Free primary, cheap paid fallback",
    },
}
