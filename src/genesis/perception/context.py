"""ContextAssembler — builds relevance-based context per reflection depth."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.awareness.types import Depth, TickResult
from genesis.db.crud import cognitive_state, observations, predictions, signal_weights
from genesis.identity.loader import IdentityLoader
from genesis.memory.user_model import UserModelEvolver
from genesis.perception.types import LIGHT_FOCUS_ROTATION, PromptContext

logger = logging.getLogger(__name__)

# Minimum sample count per domain before injecting calibration feedback
_DEFAULT_MIN_SAMPLES = 10

_AGE_GUARD_HOURS: dict = {
    "light": 12,
    "deep": 48,
    "strategic": 168,  # 7 days
}

# ── Source category mapping for citation-grouped rendering ──────────────

_SOURCE_CATEGORIES: dict[str, str] = {
    "sentinel": "Sentinel",
    "sentinel_dispatch": "Sentinel",
    "guardian": "Guardian",
    "guardian_watchdog": "Guardian",
    "recon": "Recon",
    "recon_pipeline": "Recon",
    "recon_mcp": "Recon",
    "surplus_promotion": "Surplus",
    "surplus": "Surplus",
    "surplus_scheduler": "Surplus",
    "reflection": "Reflections",
    "light_reflection": "Reflections",
    "deep_reflection": "Reflections",
    "cc_reflection_deep": "Reflections",
    "cc_reflection_light": "Reflections",
    "cc_reflection_strategic": "Reflections",
    "strategic_reflection": "Reflections",
    "conversation": "Conversation",
    "conversation_intent": "Conversation",
    "conversation_analysis": "Conversation",
    "conversation_followup": "Conversation",
    "inbox_evaluation": "Inbox",
}

_CATEGORY_ORDER = [
    "Sentinel", "Guardian", "Recon", "Reflections",
    "Surplus", "Conversation", "Inbox", "Other",
]


def _categorize_source(source: str) -> str:
    """Map an observation source to its display category."""
    return _SOURCE_CATEGORIES.get(source, "Other")


def _relative_age(created_at: str) -> str:
    """Return a human-readable relative time string."""
    try:
        created = datetime.fromisoformat(created_at)
        delta = datetime.now(UTC) - created
        hours = delta.total_seconds() / 3600
        if hours < 1:
            return f"{int(delta.total_seconds() / 60)}m ago"
        if hours < 24:
            return f"{hours:.0f}h ago"
        return f"{hours / 24:.0f}d ago"
    except (ValueError, TypeError):
        return ""


def _format_observations_grouped(obs_list: list[dict]) -> list[str]:
    """Group observations by source subsystem with citation anchors.

    Returns formatted lines ready for prompt injection.
    """
    if not obs_list:
        return []

    # Group by category
    groups: dict[str, list[dict]] = {}
    for obs in obs_list:
        cat = _categorize_source(obs.get("source", ""))
        groups.setdefault(cat, []).append(obs)

    lines: list[str] = ["## Subsystem Activity"]
    for cat in _CATEGORY_ORDER:
        if cat not in groups:
            continue
        lines.append(f"### {cat}")
        for obs in groups[cat]:
            oid = obs.get("id", "?")
            short_id = oid[:8] if len(oid) > 8 else oid
            priority = obs.get("priority", "?")
            obs_type = obs.get("type", "?")
            content = obs.get("content", "")
            age = _relative_age(obs.get("created_at", ""))
            age_suffix = f" ({age})" if age else ""
            lines.append(f"- [#{short_id}] [{priority}] {obs_type}: {content}{age_suffix}")
        lines.append("")  # blank line between groups

    return lines


class ContextAssembler:
    """Assembles context for prompt rendering based on depth.

    Scopes context by relevance, not budget. Never truncates.
    Micro: signals only (no identity — cheap model gets task instruction via template).
    Light: full identity + user profile + cognitive state + user model.
    Deep/Strategic: + calibration feedback.
    """

    def __init__(
        self,
        *,
        identity_loader: IdentityLoader,
        user_model_evolver: UserModelEvolver | None = None,
        calibration_min_samples: int = _DEFAULT_MIN_SAMPLES,
    ) -> None:
        self._identity = identity_loader
        self._user_model_evolver = user_model_evolver
        self._calibration_min_samples = calibration_min_samples

    async def assemble(
        self,
        depth: Depth,
        tick: TickResult,
        *,
        db: aiosqlite.Connection,
        prior_context: str | None = None,
    ) -> PromptContext:
        # Micro: no identity (cheap model overwhelmed by SOUL.md — just task instruction).
        # Light+: full identity block (SOUL.md + USER.md + STEERING.md).
        identity = "" if depth == Depth.MICRO else self._identity.identity_block()

        # Micro: exclude signals registered in signal_weights but NOT scoped
        # to Micro via feeds_depths.  This prevents the LLM from seeing (and
        # flagging as anomalous) signals like user_goal_staleness that don't
        # belong at this depth.  Unregistered signals pass through (safe for
        # tests and future signal collectors not yet in signal_weights).
        excluded_signals: set[str] | None = None
        if depth == Depth.MICRO:
            try:
                all_rows = await signal_weights.list_all(db)
                micro_rows = await signal_weights.list_by_depth(db, "Micro")
                if all_rows:
                    all_names = {r["signal_name"] for r in all_rows}
                    micro_names = {r["signal_name"] for r in micro_rows}
                    excluded_signals = all_names - micro_names or None
            except Exception:
                logger.debug("Could not load signal_weights for Micro filtering")

        signals_text = self._format_signals(tick, excluded_signals=excluded_signals)
        tick_number = self._extract_tick_number(tick)

        user_profile = None
        cog_state = None
        user_model = None
        calibration_text = None

        if depth in (Depth.LIGHT, Depth.DEEP, Depth.STRATEGIC):
            user_text = self._identity.user()
            user_profile = user_text if user_text else None
            cog_state = await cognitive_state.render(db)
            if self._user_model_evolver:
                user_model = await self._user_model_evolver.get_model_summary()
            else:
                user_model = None  # No evolver injected yet

        # Calibration feedback — deep/strategic only, when sufficient data
        if depth in (Depth.DEEP, Depth.STRATEGIC):
            calibration_text = await self._build_calibration_text(db)

        # Recent observations — light and above, for reflection context
        memory_hits = None
        if depth in (Depth.LIGHT, Depth.DEEP, Depth.STRATEGIC):
            memory_hits = await self._build_memory_hits(db, depth.value.lower())

        # Light focus rotation: tick-based, same pattern as micro's template rotation.
        # Previously suggested_focus was never set (always None → always "situation").
        suggested_focus = None
        if depth == Depth.LIGHT:
            suggested_focus = LIGHT_FOCUS_ROTATION[tick_number % len(LIGHT_FOCUS_ROTATION)]
            # Focus-aware context: only include what this focus needs.
            # Keeps parity with CC bridge path (reflection_bridge._build_light_prompt_enriched).
            if suggested_focus == "situation":
                user_profile = None
                user_model = None
                memory_hits = None
            elif suggested_focus == "user_impact":
                memory_hits = None
            elif suggested_focus == "anomaly":
                user_profile = None
                user_model = None
        else:
            suggested_focus = getattr(tick, "suggested_focus", None)

        return PromptContext(
            depth=depth.value,
            identity=identity,
            signals_text=signals_text,
            tick_number=tick_number,
            user_profile=user_profile,
            cognitive_state=cog_state,
            memory_hits=memory_hits,
            prior_context=prior_context,
            user_model=user_model,
            suggested_focus=suggested_focus,
            calibration_text=calibration_text,
        )

    async def _build_memory_hits(self, db: aiosqlite.Connection, depth_value: str = "deep") -> str | None:
        """Build recent observation context for reflection prompts.

        Two-pass diversity query ensures reflection-source observations always
        get representation even under high-volume noise from other sources.
        For Deep/Strategic: prepends a dedicated Light assessment section
        grouped by focus area (the cumulative chain).
        Tracks retrieval via increment_retrieved_batch.
        """
        _REFLECTION_SOURCES = [
            "reflection", "light_reflection", "deep_reflection",
            "cc_reflection_deep", "cc_reflection_light",
        ]
        # Depth-aware caps: light gets focused context (7 obs),
        # deep/strategic get full context (up to 20 obs).
        _MEMORY_CAPS: dict[str, tuple[int, int]] = {
            "light": (3, 4),       # 3 reflection-source + 4 other = 7 total
            "deep": (10, 20),      # existing behavior
            "strategic": (10, 20),
        }
        refl_cap, other_cap = _MEMORY_CAPS.get(depth_value, (10, 20))

        # Chain context: Deep/Strategic get all Light assessments since last
        # run, grouped by focus area.  This is the cumulative chain — each
        # level builds on the analysis below it rather than re-evaluating
        # raw signals from scratch.
        chain_section = ""
        if depth_value in ("deep", "strategic"):
            chain_section = await self._build_light_chain_context(db, depth_value)

        try:
            # Pass 1: reflection-origin observations
            reflection_obs = await observations.query(
                db, resolved=False, source_in=_REFLECTION_SOURCES, limit=refl_cap,
            )
            # Pass 2: everything else
            other_obs = await observations.query(
                db, resolved=False, limit=other_cap,
            )

            # Depth-specific age guard — drop observations older than the cutoff
            cutoff_hours = _AGE_GUARD_HOURS.get(depth_value, 48)
            cutoff = (datetime.now(UTC) - timedelta(hours=cutoff_hours)).isoformat()
            reflection_obs = [o for o in reflection_obs if o.get("created_at", "") >= cutoff]
            other_obs = [o for o in other_obs if o.get("created_at", "") >= cutoff]
            logger.debug("Age guard (%s, %dh): %d obs after filtering", depth_value, cutoff_hours, len(reflection_obs) + len(other_obs))
        except Exception:
            logger.warning("Failed to query observations for memory_hits", exc_info=True)
            return None

        # Merge and dedup by ID
        seen_ids: set[str] = set()
        merged: list[dict] = []
        for obs in reflection_obs:
            oid = obs.get("id")
            if oid and oid not in seen_ids:
                seen_ids.add(oid)
                merged.append(obs)
        for obs in other_obs:
            oid = obs.get("id")
            if oid and oid not in seen_ids:
                seen_ids.add(oid)
                merged.append(obs)

        # Depth-aware total cap
        merged = merged[:refl_cap + other_cap]

        if not merged:
            return None

        # Sort by priority: high > medium > low
        priority_order = {"high": 0, "medium": 1, "low": 2}
        merged.sort(key=lambda o: priority_order.get(o.get("priority", "low"), 3))

        obs_ids: list[str] = [obs.get("id", "") for obs in merged if obs.get("id")]

        # Group by source subsystem for structured rendering
        lines = _format_observations_grouped(merged)

        # Track retrieval and influence (displayed in prompt = influenced awareness)
        if obs_ids:
            try:
                await observations.increment_retrieved_batch(db, obs_ids)
                await observations.mark_influenced_batch(db, obs_ids)
            except Exception:
                logger.warning("Failed to track observation retrieval in memory_hits", exc_info=True)

        result = "\n".join(lines)
        if chain_section:
            result = chain_section + "\n\n" + result
        return result

    async def _build_light_chain_context(
        self, db: aiosqlite.Connection, depth_value: str,
    ) -> str:
        """Build Light assessment chain context for Deep/Strategic.

        Fetches all light_reflection observations since the last Deep/Strategic
        tick, groups them by focus_area, and formats as a chain context section.
        """
        from genesis.db.crud import awareness_ticks

        # Find cutoff: last tick at this depth
        last_tick = await awareness_ticks.last_at_depth(db, depth_value.title())
        if last_tick and last_tick.get("created_at"):
            since = last_tick["created_at"]
        else:
            # No prior tick — use 48h for deep, 7d for strategic
            hours = 48 if depth_value == "deep" else 168
            since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()

        try:
            light_obs = await observations.query(
                db, resolved=False, type="light_reflection", limit=15,
            )
            # Filter to observations since last run at this depth
            light_obs = [o for o in light_obs if o.get("created_at", "") >= since]
        except Exception:
            logger.warning("Failed to query Light assessments for chain context", exc_info=True)
            return ""

        if not light_obs:
            return ""

        # Group by focus_area from content JSON
        by_focus: dict[str, list[str]] = {}
        for obs in light_obs:
            try:
                content = json.loads(obs.get("content", "{}"))
                focus = content.get("focus_area", "unknown")
                assessment = content.get("assessment", "")[:300]
                if assessment:
                    by_focus.setdefault(focus, []).append(assessment)
            except (json.JSONDecodeError, TypeError):
                pass

        if not by_focus:
            return ""

        lines = ["## Light Reflection Findings Since Last Run"]
        for focus, assessments in sorted(by_focus.items()):
            lines.append(f"\n### [{focus}]")
            for a in assessments:
                lines.append(f"- {a}")

        return "\n".join(lines)

    async def _build_calibration_text(self, db: aiosqlite.Connection) -> str | None:
        """Build calibration feedback text from calibration_curves table.

        Only includes domains with enough samples. Returns None if no data.
        """
        try:
            # Get all domains that have calibration data
            cursor = await db.execute(
                "SELECT DISTINCT domain FROM calibration_curves"
            )
            domains = [row[0] for row in await cursor.fetchall()]
        except Exception:
            logger.debug("Could not query calibration domains", exc_info=True)
            return None

        if not domains:
            return None

        lines: list[str] = []
        for domain in domains:
            try:
                curves = await predictions.get_calibration_curves(db, domain)
            except Exception:
                logger.debug("Could not load calibration for domain=%s", domain, exc_info=True)
                continue

            # Filter to buckets with enough samples
            relevant = [
                c for c in curves
                if c.get("sample_count", 0) >= self._calibration_min_samples
            ]
            if not relevant:
                continue

            domain_lines: list[str] = []
            for curve in relevant:
                predicted = curve.get("predicted_confidence", 0)
                actual = curve.get("actual_success_rate", 0)
                samples = curve.get("sample_count", 0)
                predicted_pct = int(predicted * 100)
                actual_pct = int(actual * 100)
                domain_lines.append(
                    f"  - When you report ~{predicted_pct}% confidence, "
                    f"you're historically right ~{actual_pct}% of the time "
                    f"(n={samples})"
                )
            if domain_lines:
                lines.append(f"Domain: {domain}")
                lines.extend(domain_lines)

        if not lines:
            return None

        header = (
            "Historical calibration (adjust your confidence accordingly):"
        )
        return header + "\n" + "\n".join(lines)

    def _format_signals(
        self,
        tick: TickResult,
        excluded_signals: set[str] | None = None,
    ) -> str:
        staleness = tick.signal_staleness or {}
        tick_interval_min = 5  # awareness loop tick interval
        lines = []
        for s in tick.signals:
            if excluded_signals is not None and s.name in excluded_signals:
                continue
            line = f"{s.name}: {s.value} (source={s.source})"
            if s.normal_max is not None:
                status = (
                    "CRITICAL" if s.critical_threshold is not None and s.value >= s.critical_threshold
                    else "WARNING" if s.warning_threshold is not None and s.value >= s.warning_threshold
                    else "normal"
                )
                line += (
                    f" [{status}; normal<={s.normal_max},"
                    f" warn>={s.warning_threshold}, crit>={s.critical_threshold}]"
                )
            if s.baseline_note:
                line += f" -- baseline: {s.baseline_note}"
            unchanged = staleness.get(s.name, 0)
            if unchanged >= 2:
                hours = unchanged * tick_interval_min / 60
                line += f" (persistent ~{hours:.1f}h)"
            lines.append(line)
        return "\n".join(lines)

    def _extract_tick_number(self, tick: TickResult) -> int:
        """Derive a deterministic tick number from tick_id for template rotation.

        Uses UUID int conversion (stable across processes) rather than hash()
        which is randomized per-process via PYTHONHASHSEED.
        """
        import uuid as _uuid

        try:
            return _uuid.UUID(tick.tick_id).int % 10000
        except ValueError:
            # Fallback for non-UUID tick_ids
            return int.from_bytes(tick.tick_id.encode()[:8], "big") % 10000
