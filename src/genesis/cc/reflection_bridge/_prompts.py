"""Prompt building for reflection sessions.

All functions are standalone — they accept explicit parameters rather than
a class instance. The CCReflectionBridge class delegates to these.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.awareness.types import Depth, SignalReading, TickResult
from genesis.cc.types import CCModel
from genesis.perception.types import LIGHT_FOCUS_ROTATION

if TYPE_CHECKING:
    from genesis.perception.context import ContextAssembler
    from genesis.reflection.context_gatherer import ContextGatherer

logger = logging.getLogger(__name__)


# ── Signal formatting ────────────────────────────────────────────────


_TICK_INTERVAL_MINUTES = 5  # awareness loop tick interval


def _format_signal(s: SignalReading, *, unchanged_ticks: int = 0) -> str:
    """Format a signal with threshold and staleness annotations."""
    base = f"{s.name}={s.value}"
    if s.normal_max is not None:
        status = (
            "CRITICAL" if s.critical_threshold is not None and s.value >= s.critical_threshold
            else "WARNING" if s.warning_threshold is not None and s.value >= s.warning_threshold
            else "normal"
        )
        base += f" [{status}]"
    if unchanged_ticks >= 2:
        hours = unchanged_ticks * _TICK_INTERVAL_MINUTES / 60
        base += f" (persistent ~{hours:.1f}h)"
    return base


def _format_signals_from_tick(tick: TickResult, *, limit: int = 10) -> str:
    """Format signals with staleness annotations from a TickResult."""
    staleness = tick.signal_staleness or {}
    parts = [
        _format_signal(s, unchanged_ticks=staleness.get(s.name, 0))
        for s in tick.signals[:limit]
    ]
    return ", ".join(parts) if parts else "none"


# ── Light reflection focus rotation ──────────────────────────────────


LIGHT_FOCUS_INSTRUCTIONS: dict[str, str] = {
    "situation": (
        "## Focus: Situation Assessment\n"
        "Assess current system state. Every claim MUST cite a specific signal value.\n"
        "Do NOT produce user_model_updates (set to empty list).\n"
        "Do NOT produce surplus_candidates (set to empty list).\n"
        "Focus on: assessment, patterns, recommendations, escalation.\n\n"
        "Also include a \"context_update\" field (string): a 2-3 sentence summary of "
        "current system state and any active work or pending items. This keeps the "
        "cognitive state fresh between deep reflections. Be factual and specific."
    ),
    "user_impact": (
        "## Focus: User Impact Analysis\n"
        "Analyze how current conditions affect the user's goals and work.\n"
        "This is the ONLY rotation that produces user_model_updates.\n"
        "Each delta MUST have confidence >= 0.9 and cite specific evidence.\n"
        "Do NOT produce surplus_candidates (set to empty list).\n"
        "Focus on: assessment, user_model_updates, recommendations."
    ),
    "anomaly": (
        "## Focus: Pattern Detection & Anomaly Investigation\n"
        "Look for unusual patterns, unexpected correlations, emerging trends.\n"
        "Flag investigation-worthy items as surplus_candidates.\n"
        "Do NOT produce user_model_updates (set to empty list).\n"
        "Focus on: assessment, patterns, surplus_candidates, escalation."
    ),
}


def _light_focus_area(tick: TickResult) -> str:
    """Derive focus area from tick_id, matching perception engine rotation."""
    import uuid as _uuid

    try:
        tick_number = _uuid.UUID(tick.tick_id).int % 10000
    except ValueError:
        tick_number = int.from_bytes(tick.tick_id.encode()[:8], "big") % 10000
    return LIGHT_FOCUS_ROTATION[tick_number % len(LIGHT_FOCUS_ROTATION)]


# ── Observation formatting ───────────────────────────────────────────


_OBS_TOTAL_CHAR_BUDGET = 25_000  # ~6K tokens
_OBS_MAX_COUNT = 50
_OBS_MIN_PER_ITEM = 200


def _format_observations_json(observations: list[dict], *, max_count: int = _OBS_MAX_COUNT) -> str:
    """Format observations as JSON with a total character budget."""
    obs = observations[:max_count]
    if not obs:
        return "[]"
    per_item = max(_OBS_MIN_PER_ITEM, _OBS_TOTAL_CHAR_BUDGET // len(obs))
    return json.dumps([
        {"id": o.get("id", ""), "type": o.get("type", ""),
         "source": o.get("source", ""), "priority": o.get("priority", ""),
         "content": o.get("content", "")[:per_item], "created_at": o.get("created_at", "")}
        for o in obs
    ], indent=2)


# Source category mapping — mirrors perception/context.py categories.
# Acceptable duplication (10 lines) to avoid cross-module coupling.
_SOURCE_CATEGORIES: dict[str, str] = {
    "sentinel": "Sentinel", "sentinel_dispatch": "Sentinel",
    "guardian": "Guardian", "guardian_watchdog": "Guardian",
    "recon": "Recon", "recon_pipeline": "Recon", "recon_mcp": "Recon",
    "surplus_promotion": "Surplus", "surplus": "Surplus", "surplus_scheduler": "Surplus",
    "reflection": "Reflections", "light_reflection": "Reflections",
    "deep_reflection": "Reflections", "cc_reflection_deep": "Reflections",
    "cc_reflection_light": "Reflections", "cc_reflection_strategic": "Reflections",
    "strategic_reflection": "Reflections",
    "conversation": "Conversation", "conversation_intent": "Conversation",
    "conversation_analysis": "Conversation", "conversation_followup": "Conversation",
    "inbox_evaluation": "Inbox",
}
_CATEGORY_ORDER = [
    "Sentinel", "Guardian", "Recon", "Reflections",
    "Surplus", "Conversation", "Inbox", "Other",
]


def _format_observations_grouped(
    observations: list[dict],
    *,
    max_count: int = _OBS_MAX_COUNT,
) -> str:
    """Format observations grouped by source subsystem with citation anchors.

    Replaces flat JSON rendering for Deep/Strategic prompts with structured
    markdown that makes subsystem provenance clear at a glance.
    """
    obs = observations[:max_count]
    if not obs:
        return ""

    per_item = max(_OBS_MIN_PER_ITEM, _OBS_TOTAL_CHAR_BUDGET // len(obs))

    # Group by category
    groups: dict[str, list[dict]] = {}
    for o in obs:
        cat = _SOURCE_CATEGORIES.get(o.get("source", ""), "Other")
        groups.setdefault(cat, []).append(o)

    lines: list[str] = []
    for cat in _CATEGORY_ORDER:
        if cat not in groups:
            continue
        lines.append(f"### {cat}")
        for o in groups[cat]:
            oid = o.get("id", "?")
            short_id = oid[:8] if len(oid) > 8 else oid
            priority = o.get("priority", "?")
            obs_type = o.get("type", "?")
            content = o.get("content", "")[:per_item]
            created = o.get("created_at", "?")
            lines.append(f"- [#{short_id}] [{priority}] {obs_type} ({created}): {content}")
        lines.append("")

    return "\n".join(lines)


# ── Data pointers ────────────────────────────────────────────────────


def build_data_pointers() -> str:
    """Build a section pointing reflections to raw data they can access."""
    from genesis.env import cc_project_dir
    project_id = cc_project_dir()
    return (
        "\n## Available Data Sources\n\n"
        "You have full tool access. Use these to investigate claims and "
        "gather context beyond what's provided above:\n\n"
        f"- **Session transcripts**: `~/.claude/projects/{project_id}/*.jsonl` "
        "(JSONL, one per session)\n"
        "- **Database**: `~/genesis/data/genesis.db` — use `db_schema` MCP tool "
        "to discover tables before querying\n"
        "- **Observations**: use `observation_query` or query `observations` table "
        "in SQLite for full unresolved set\n"
        "- **Sessions**: query `cc_sessions` table for session topics, keywords, "
        "and timestamps\n"
        "- **Health**: `health_status`, `health_errors`, `job_health`, "
        "`subsystem_heartbeats` MCP tools\n"
        "- **Memory**: `memory_recall` for semantic search across all stored knowledge\n"
        "- **Recon**: `recon_findings` for external intelligence gathered by "
        "background research\n"
    )


# ── Prompt builders ──────────────────────────────────────────────────


async def build_reflection_prompt(
    depth: Depth,
    tick: TickResult,
    *,
    db,
    context_gatherer: ContextGatherer | None,
    context_assembler: ContextAssembler | None,
    prompt_dir: Path,
) -> tuple[str, tuple[str, ...]]:
    """Build prompt — enriched if context_gatherer available, simple otherwise.

    Returns (prompt_text, gathered_observation_ids). The IDs are used by
    the caller to mark observations as influenced AFTER reflection produces
    substantive output (not before).
    """

    # Enriched paths — context_gatherer provides observations, surplus, etc.
    if context_gatherer and depth == Depth.STRATEGIC:
        return await build_strategic_prompt_enriched(tick, db=db, context_gatherer=context_gatherer)
    if context_gatherer and depth == Depth.DEEP:
        return await build_enriched_prompt(tick, db=db, context_gatherer=context_gatherer)

    # Legacy simple path
    from genesis.db.crud import cognitive_state

    signals_summary = _format_signals_from_tick(tick)

    scores_summary = ", ".join(
        f"{s.depth.value}={s.final_score:.2f}" for s in tick.scores
    ) if tick.scores else "none"

    cog_state = await cognitive_state.render(db)

    # Light: focus-aware prompt — enriched if context_assembler available
    if depth == Depth.LIGHT:
        focus = _light_focus_area(tick)
        focus_instruction = LIGHT_FOCUS_INSTRUCTIONS.get(focus, LIGHT_FOCUS_INSTRUCTIONS["situation"])

        if context_assembler:
            prompt = await build_light_prompt_enriched(
                tick, focus, focus_instruction, db=db, context_assembler=context_assembler,
            )
            return prompt, ()

        # Fallback: thin prompt (when context_assembler not injected)
        return (
            f"Perform a Light reflection.\n\n"
            f"Tick ID: {tick.tick_id}\n"
            f"Timestamp: {tick.timestamp}\n"
            f"Trigger: {tick.trigger_reason or 'scheduled'}\n"
            f"Signals: {signals_summary}\n"
            f"Depth scores: {scores_summary}\n\n"
            f"## Current Cognitive State\n\n{cog_state}\n\n"
            f"{focus_instruction}\n\n"
            f'Set "focus_area": "{focus}" in your JSON output.'
        ), ()

    return (
        f"Perform a {depth.value} reflection.\n\n"
        f"Tick ID: {tick.tick_id}\n"
        f"Timestamp: {tick.timestamp}\n"
        f"Trigger: {tick.trigger_reason or 'scheduled'}\n"
        f"Signals: {signals_summary}\n"
        f"Depth scores: {scores_summary}\n\n"
        f"## Current Cognitive State\n\n{cog_state}\n\n"
        f"Analyze the current state, identify patterns and observations, "
        f"and provide actionable insights."
    ), ()


async def build_strategic_prompt_enriched(
    tick: TickResult,
    *,
    db,
    context_gatherer: ContextGatherer,
) -> tuple[str, tuple[str, ...]]:
    """Build enriched strategic prompt with observations and data pointers."""
    bundle = await context_gatherer.gather(db)

    from genesis.db.crud import cognitive_state
    cog_state = await cognitive_state.render(db)

    signals_summary = _format_signals_from_tick(tick)

    from genesis.env import user_timezone
    tz_name = user_timezone()

    parts = [
        "Perform a Strategic reflection.\n",
        f"Tick ID: {tick.tick_id}",
        f"Timestamp: {tick.timestamp}",
        f"User timezone: {tz_name}",
        f"Trigger: {tick.trigger_reason or 'scheduled'}",
        f"Signals: {signals_summary}\n",
        f"## Current Cognitive State\n\n{cog_state}\n",
    ]

    if bundle.recent_observations:
        obs_summary = _format_observations_grouped(bundle.recent_observations)
        parts.append(f"\n## Recent Observations\n{obs_summary}")

    cost = bundle.cost_summary
    parts.append(
        f"\n## Cost Summary\n"
        f"Today: ${cost.daily_usd:.4f} ({cost.daily_budget_pct:.0%} of daily budget)\n"
        f"This week: ${cost.weekly_usd:.4f} ({cost.weekly_budget_pct:.0%} of weekly)\n"
        f"This month: ${cost.monthly_usd:.4f} ({cost.monthly_budget_pct:.0%} of monthly)"
    )

    stats = bundle.procedure_stats
    if stats.total_active > 0:
        parts.append(
            f"\n## Procedure Stats\n"
            f"Active: {stats.total_active}, Quarantined: {stats.total_quarantined}, "
            f"Avg success rate: {stats.avg_success_rate:.1%}"
        )

    parts.append(build_data_pointers())
    return "\n".join(parts), bundle.gathered_observation_ids


async def build_enriched_prompt(
    tick: TickResult,
    *,
    db,
    context_gatherer: ContextGatherer,
) -> tuple[str, tuple[str, ...]]:
    """Build rich prompt using ContextGatherer data.

    Returns (prompt_text, gathered_observation_ids).
    """
    bundle = await context_gatherer.gather(db)

    from genesis.env import user_timezone
    tz_name = user_timezone()

    parts = [
        "Perform a Deep reflection.\n",
        f"Tick ID: {tick.tick_id}",
        f"Timestamp: {tick.timestamp}",
        f"User timezone: {tz_name}",
        f"Trigger: {tick.trigger_reason or 'scheduled'}",
    ]

    if tick.signals:
        signals = _format_signals_from_tick(tick)
        parts.append(f"\n## Signals\n{signals}")

    parts.append(f"\n## Current Cognitive State\n{bundle.cognitive_state}")

    jobs = bundle.pending_work.active_jobs
    if jobs:
        parts.append("\n## Active Jobs\n" + ", ".join(j.value for j in jobs))

    if bundle.recent_observations and bundle.pending_work.memory_consolidation:
        obs_summary = _format_observations_grouped(bundle.recent_observations)
        parts.append(f"\n## Recent Observations (for consolidation)\n{obs_summary}")

    if bundle.surplus_staging_items:
        _surplus_limit = max(200, _OBS_TOTAL_CHAR_BUDGET // max(len(bundle.surplus_staging_items), 1))
        surplus_summary = json.dumps([
            {"id": s.get("id", ""), "content": s.get("content", "")[:_surplus_limit],
             "confidence": s.get("confidence", 0), "drive": s.get("drive_alignment", "")}
            for s in bundle.surplus_staging_items[:10]
        ], indent=2)
        parts.append(f"\n## Pending Surplus Items (decide: promote or discard)\n```json\n{surplus_summary}\n```")

    stats = bundle.procedure_stats
    if stats.total_active > 0:
        parts.append(
            f"\n## Procedure Stats\n"
            f"Active: {stats.total_active}, Quarantined: {stats.total_quarantined}, "
            f"Avg success rate: {stats.avg_success_rate:.1%}"
        )
        if stats.low_performers:
            parts.append(f"Low performers: {json.dumps(stats.low_performers)}")

    cost = bundle.cost_summary
    parts.append(
        f"\n## Cost Summary\n"
        f"Today: ${cost.daily_usd:.4f} ({cost.daily_budget_pct:.0%} of daily budget)\n"
        f"This week: ${cost.weekly_usd:.4f} ({cost.weekly_budget_pct:.0%} of weekly)\n"
        f"This month: ${cost.monthly_usd:.4f} ({cost.monthly_budget_pct:.0%} of monthly)"
    )

    if bundle.recent_conversations:
        conv_lines = []
        for turn in bundle.recent_conversations:
            ts = turn.get("timestamp", "")
            text = turn.get("text", "")
            conv_lines.append(f"[{ts}] {text}")
        parts.append(
            "\n## Recent User Conversation\n"
            "The user has been discussing the following in the current CLI session:\n"
            + "\n".join(conv_lines)
        )

    # Cross-interaction evaluation context for pattern synthesis
    try:
        eval_ctx = await context_gatherer.gather_evaluation_context(db)
        counts = eval_ctx.get("signal_counts", {})
        has_signals = any(counts.get(k, 0) > 0 for k in counts)
        if has_signals:
            ctx_parts = ["\n## Cross-Interaction Signals (for pattern synthesis)"]
            ctx_parts.append(
                f"Signal counts: {json.dumps(counts)}"
            )
            if eval_ctx["user_signals"]:
                ctx_parts.append("\n### User Signals")
                for sig in eval_ctx["user_signals"][:10]:
                    ctx_parts.append(
                        f"- [{sig['source']}] {sig['content']}"
                    )
            if eval_ctx["architecture_insights"]:
                ctx_parts.append("\n### Architecture Insights")
                for ins in eval_ctx["architecture_insights"][:10]:
                    ctx_parts.append(
                        f"- [{ins['source']}] {ins['content']}"
                    )
            if eval_ctx["interaction_themes"]:
                ctx_parts.append("\n### Previous Interaction Themes")
                for theme in eval_ctx["interaction_themes"]:
                    ctx_parts.append(f"- {theme['content']}")
            if eval_ctx["inbox_findings"]:
                ctx_parts.append("\n### Recent Inbox Findings")
                for finding in eval_ctx["inbox_findings"][:10]:
                    ctx_parts.append(
                        f"- [{finding['type']}] {finding['content']}"
                    )
            parts.append("\n".join(ctx_parts))
    except Exception:
        logger.error("Failed to gather evaluation context for deep reflection", exc_info=True)

    parts.append(build_data_pointers())
    return "\n".join(parts), bundle.gathered_observation_ids


async def build_light_prompt_enriched(
    tick: TickResult,
    focus: str,
    focus_instruction: str,
    *,
    db,
    context_assembler: ContextAssembler,
) -> str:
    """Build light reflection prompt using ContextAssembler."""
    ctx = await context_assembler.assemble(Depth.LIGHT, tick, db=db)

    parts = [
        "Perform a Light reflection.\n",
        f"Tick ID: {tick.tick_id}",
        f"Timestamp: {tick.timestamp}",
        f"Trigger: {tick.trigger_reason or 'scheduled'}\n",
        f"## Signals\n{ctx.signals_text}",
        f"\n## Current Cognitive State\n{ctx.cognitive_state or '(none)'}",
    ]

    if focus == "user_impact":
        if ctx.user_profile:
            parts.append(f"\n## User Profile\n{ctx.user_profile}")
        if ctx.user_model:
            parts.append(f"\n## User Model\n{ctx.user_model}")
    elif focus == "anomaly":
        if ctx.memory_hits:
            parts.append(f"\n## Recent Observations\n{ctx.memory_hits}")

    parts.append(f"\n{focus_instruction}")
    parts.append(f'\nSet "focus_area": "{focus}" in your JSON output.')
    return "\n".join(parts)


# ── Prompt file loading ──────────────────────────────────────────────


_DEPTH_MODEL = {
    Depth.LIGHT: CCModel.HAIKU,
    Depth.DEEP: CCModel.SONNET,
    Depth.STRATEGIC: CCModel.OPUS,
}

_PROMPT_FILES = {
    Depth.LIGHT: "REFLECTION_LIGHT.md",
    Depth.DEEP: "REFLECTION_DEEP.md",
    Depth.STRATEGIC: "REFLECTION_STRATEGIC.md",
}

_FALLBACK_PROMPTS = {
    Depth.LIGHT: (
        "You are Genesis performing a Light reflection — a quick sanity check. "
        "Note anomalies, check if escalation is needed. "
        "Output JSON with 'observations', 'escalate_to_deep', 'summary'."
    ),
    Depth.DEEP: (
        "You are Genesis performing a Deep reflection. "
        "Analyze recent signals and observations for meaningful patterns. "
        "Output structured JSON with 'observations', 'patterns', 'recommendations'."
    ),
    Depth.STRATEGIC: (
        "You are Genesis performing a Strategic reflection. "
        "Think broadly about long-term patterns, goals, and system evolution. "
        "Output structured JSON with 'observations', 'patterns', 'recommendations'."
    ),
}


def system_prompt_for_depth(depth: Depth, prompt_dir: Path) -> str:
    """Load the system prompt for a given depth, with model-specific variant support."""
    filename = _PROMPT_FILES.get(depth)
    if filename:
        model = _DEPTH_MODEL.get(depth)
        if model:
            stem = filename.rsplit(".", 1)[0]
            model_file = f"{stem}_{model.value.upper()}.md"
            model_path = prompt_dir / model_file
            if model_path.exists():
                return model_path.read_text()
        path = prompt_dir / filename
        if path.exists():
            return path.read_text()
    return _FALLBACK_PROMPTS.get(depth, _FALLBACK_PROMPTS[Depth.DEEP])


def load_prompt_file(filename: str, prompt_dir: Path) -> str:
    """Load a CAPS markdown prompt file with fallback."""
    path = prompt_dir / filename
    if path.exists():
        return path.read_text()
    logger.warning("Prompt file %s not found, using minimal fallback", filename)
    return "Perform the task described in the user message. Output valid JSON."
