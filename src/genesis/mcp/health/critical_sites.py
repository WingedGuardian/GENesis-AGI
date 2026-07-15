"""Curated critical call sites — the "red if down" allowlist.

The unified call-site health detector (``mcp/health/errors.py``) reads the
``call_site_last_run`` table and, for any site whose last run failed within
:data:`CALLSITE_DOWN_RECENCY_HOURS`, emits a ``callsite:down:<id>`` alert. A
site in :data:`CRITICAL_CALL_SITES` renders **CRITICAL/red**; every other
failing site renders **WARNING/yellow** (watched, not alarming). The detector
is dashboard-only: ``callsite:down:`` is never on the outreach escalation
whitelist and is UNMAPPED in the Sentinel remediation map, so it never pages
Telegram and never wakes the firefighter (both fail-closed by design).

Why a curated constant and NOT ``ESSENTIAL_CLOUD_SITES``:
``routing/essential.py`` is calibrated for *cloud-coverage degradation* (it
includes ``3_micro_reflection`` because losing cloud coverage there matters for
the routing question) — a different lens from "is this important if the whole
call site goes down." A fast-shedding eval site (``judge``) or a routine
micro-reflection is NOT critical-if-down; memory formation and the ego cycles
are. So this list is purpose-built for the availability question.

There is deliberately no ``critical`` attribute on the call_site config: the
runtime superset of call-site ids (this table) is larger than the routing YAML
(it includes ``cc``-provider sites like the ego cycles and ``embedding``-provider
sites), and criticality here is an operator judgment, not a routing property.

GROUNDWORK(sentinel-auto-topup): this set is also the intended anchor for a
future, user-gated auto-credit-top-up — the Sentinel could one day offer to
refill credits ONLY when an exhausted provider is the sole route for a site in
this set. Not built; the per-provider credit signal lives in ``errors.py`` and
the exclusion rationale in ``sentinel/remediation_map.py``.
"""

from __future__ import annotations

# How recently a site's LAST run must have failed for its failure to count as
# a *current* problem. ``call_site_last_run`` is INSERT-OR-REPLACE (one row per
# site = its latest run), so an old failed row means "hasn't succeeded since"
# — but a weeks-old failure is almost always an abandoned/one-off site
# (e.g. dream_cycle_* / models_md_synthesis were 600h+ stale in prod), not an
# actionable outage. 24h is chosen from live cadence data: the frequently
# cycling critical sites (embeddings, fact/procedure extraction, ego focus) and
# the ~18h ego cycles all refresh within a day, while every observed stale
# failure sat far outside it. Tunable — widen if slow-cadence critical sites
# (weekly strategic/deep reflection) start under-surfacing. Dashboard-only, so
# the cost of a miss is low and the cost of noise is low.
CALLSITE_DOWN_RECENCY_HOURS = 24

# Sites that render CRITICAL/red when their last run failed. Everything else
# failing renders WARNING/yellow. Ids are runtime ``call_site_id`` values as
# written to ``call_site_last_run`` (verified live 2026-07-12), NOT the neural-
# monitor labels in ``_call_site_meta.py`` (which unify the two ego cycles under
# ``7_ego_cycle``). ``autonomous_executor_reasoning`` is deliberately EXCLUDED
# — it has a CC fallback, so chain exhaustion there is watched (yellow), not red.
CRITICAL_CALL_SITES: frozenset[str] = frozenset(
    {
        # Memory formation & retrieval — losing these silently corrupts what
        # Genesis learns and can recall.
        "9_fact_extraction",
        "40_knowledge_distillation",
        "38_procedure_extraction",
        "21_embeddings",
        "21b_query_embedding",
        # Memory continuity — the ambient drift arbiter decides which
        # cross-session drift memories surface (entity/drift system).
        "ambient_arbiter",
        # Ego — the two live ego cycles (COO/Genesis + CEO/user) and focus
        # selection. NOTE: 8_ego_compaction is deliberately EXCLUDED — the ego
        # moved to ephemeral sessions (ego/compaction.py: "no LLM compaction";
        # its call_site_id arg is a legacy-ignored param), so that site never
        # routes and never records a last_run row — a dead id in a red set would
        # be misleading. (8_ego_compaction + 7_ego_cycle were removed from
        # model_routing.yaml + ESSENTIAL_CLOUD_SITES and marked DEPRECATED_REMOVED
        # in _call_site_meta.py on 2026-07-13.)
        "7_genesis_ego_cycle",
        "7_user_ego_cycle",
        "40_ego_focus_selection",
        # Core cognition — the deep and strategic reflection depths.
        "5_deep_reflection",
        "6_strategic_reflection",
    }
)
