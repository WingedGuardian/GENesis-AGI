"""User ego context builder — assembles user-focused operational briefing.

The user ego sees the world through the user's eyes: their conversations,
their interests, their pending work, and what Genesis can do for them.
Genesis infrastructure is intentionally excluded — system issues reach
the user ego ONLY via genesis ego escalations.

This is an automated prompt generator — it builds the prompt the user
WOULD give if they were sitting here. The quality of this prompt
determines the quality of the ego's autocomplete.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from genesis.ego.domain_classifier import is_genesis_internal as _is_genesis_internal
from genesis.ego.types import NEUTRAL_STATUS

logger = logging.getLogger(__name__)

# Observation categories that represent user-world signals.
# Used by GenesisEgoContextBuilder to EXCLUDE these from its system-focused view.
_USER_WORLD_CATEGORIES = frozenset({
    "email_recon", "inbox", "finding", "interest", "interests",
    "contribution", "user_model_delta",
    # User-domain categories — genesis ego should not see these
    "career", "career_advancement", "career_application",
    "content", "content_publishing", "content_distribution",
    "goal_management", "goal_review", "portfolio",
    "marketing", "outreach", "networking",
})

# Genesis-internal keyword detection now lives in genesis.ego.domain_classifier
# (shared with follow-up domain classification); imported above as
# _is_genesis_internal.


class UserEgoContextBuilder:
    """Builds the user-focused operational briefing for the user ego.

    Current-state only — no historical lookups. The ego uses
    memory_recall on demand for historical context.

    Data sources (ordered by signal value):
    1. User model — who the user is, what they care about
    2. User activity pulse — user-facing awareness signals
    3. Recent conversations — what the user is working on
    4. User-world observations — email, inbox, findings
    5. Genesis ego escalations — things Genesis can't resolve alone
    6. Capabilities — what Genesis CAN do but ISN'T doing
    7. Open threads — pending follow-ups

    NOTE: System health is intentionally EXCLUDED. The user ego has no
    jurisdiction over Genesis infrastructure. System issues reach it ONLY
    via genesis ego escalations (source 5 above).
    """

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        health_data: Any | None = None,  # Retained for API compat; unused
        capabilities: dict[str, str] | None = None,
    ) -> None:
        self._db = db
        self._capabilities = capabilities or {}

    async def build(
        self,
        *,
        context_weights: dict[str, str] | None = None,
        focus_id: str | None = None,
    ) -> str:
        """Assemble the full user ego context.

        Parameters
        ----------
        context_weights:
            Per-section weight dict from the focus selector.
            Values: "always", "deep", "light", "skip".
            When None, all sections render at full depth (backward compat).
        focus_id:
            Target ID from the focus selector (e.g., a goal_id for
            goal_review cycles). Used by _goal_deep_dive_section to
            render focused context on a specific goal.
        """
        from genesis.ego.focus import _ALWAYS_SECTIONS

        self._current_focus_id = focus_id
        weights = dict(context_weights) if context_weights else {}
        # Primary enforcement is in compaction.assemble_context(). This
        # is a defense-in-depth guard for direct build() callers.
        for section in _ALWAYS_SECTIONS:
            if weights.get(section) in ("skip", "light"):
                weights[section] = "deep"

        sections: list[str] = []
        sections.append("# USER_EGO_CONTEXT — What Does the User Need?\n")
        sections.append(
            "*You are Genesis's user-facing intelligence. "
            "This is your user's world. What would they ask you "
            "to do if they were here right now?*\n"
        )

        # Map weight keys → section methods. Order matches the original
        # build() order for deterministic output.
        section_map: list[tuple[str, Any]] = [
            ("user_model", self._user_model_section),
            ("intentions", self._intentions_section),
            ("goals", self._user_goals_section),
            ("directives", self._user_directives_section),
            ("world_snapshot", self._world_snapshot_section),
            ("activity_pulse", self._user_activity_pulse_section),
            ("recent_conversations", self._recent_conversations_section),
            ("backlog_summary", self._backlog_summary_section),
            ("escalations", self._genesis_escalations_section),
            ("capabilities", self._capabilities_section),
            # System status removed — user ego has no jurisdiction over
            # Genesis health. Issues reach it ONLY via escalations.
            ("follow_ups", self._follow_ups_section),
            ("proposal_history", self._proposal_history_section),
            ("proposal_board", self._proposal_board_section),
            ("execution_outcomes", self._execution_outcomes_section),
            ("goal_progress", self._goal_progress_section),
            ("goal_deep_dive", self._goal_deep_dive_section),
            ("capability_performance", self._capability_performance_section),
            ("recurring_patterns", self._recurring_patterns_section),
            ("output_contract", self._output_contract_section),
        ]

        import asyncio

        for key, method in section_map:
            depth = weights.get(key, "deep")
            if depth == "skip":
                continue
            is_async = asyncio.iscoroutinefunction(method)
            if depth == "light":
                result = (
                    await method(depth="light") if is_async
                    else method(depth="light")
                )
            else:
                # "deep" or "always" — full depth (default behavior)
                result = await method() if is_async else method()
            sections.append(result)

        return "\n".join(sections)

    # -- Section builders --

    async def _user_model_section(self, *, depth: str = "deep") -> str:
        """Who the user is — from user_model_cache."""
        lines = ["## User Profile\n"]

        try:
            cursor = await self._db.execute(
                "SELECT model_json, version, synthesized_at, evidence_count "
                "FROM user_model_cache WHERE id = 'current'"
            )
            row = await cursor.fetchone()
        except Exception:
            logger.error("Failed to query user_model_cache", exc_info=True)
            lines.append("*User model not available.*\n")
            return "\n".join(lines)

        if not row:
            lines.append("*No user model synthesized yet.*\n")
            return "\n".join(lines)

        model_json, version, synthesized_at, evidence_count = row

        if depth == "light":
            return (
                f"## User Profile\n"
                f"*v{version}, {evidence_count} evidence points, "
                f"synthesized {synthesized_at[:10]}*\n"
            )

        lines.append(
            f"*v{version}, {evidence_count} evidence points, "
            f"last synthesized {synthesized_at[:10]}*\n"
        )

        # 3d: Model freshness warning — flag when model is stale
        # relative to recent conversation activity.
        try:
            synth_dt = datetime.fromisoformat(synthesized_at)
            if synth_dt.tzinfo is None:
                synth_dt = synth_dt.replace(tzinfo=UTC)
            age_days = (datetime.now(UTC) - synth_dt).days
            cursor = await self._db.execute(
                "SELECT COUNT(*) FROM cc_sessions "
                "WHERE source_tag = 'foreground' "
                "AND started_at > ?",
                (synthesized_at,),
            )
            freshness_row = await cursor.fetchone()
            sessions_since = freshness_row[0] if freshness_row else 0
            if age_days >= 3 and sessions_since >= 3:
                lines.append(
                    f"**Warning: Model may be stale**: {age_days}d old, "
                    f"{sessions_since} conversations since last synthesis\n"
                )
        except Exception:
            pass  # Non-critical enrichment

        try:
            model = json.loads(model_json) if model_json else {}
        except (json.JSONDecodeError, TypeError):
            model = {}

        if not model:
            lines.append("*Model data empty.*\n")
            return "\n".join(lines)

        # Extract the most actionable fields for ego decision-making
        priority_keys = [
            "active_projects", "current_focus", "priorities",
            "goals", "professional_role", "expertise_areas",
            "communication_preferences", "autonomy_preferences",
            "decision_making_style", "interests", "active_investigations",
            "binding_constraints", "bottleneck_awareness",
        ]

        for key in priority_keys:
            if key in model:
                val = model[key]
                if isinstance(val, str) and len(val) > 300:
                    val = val[:300] + "..."
                elif isinstance(val, (list, dict)):
                    val = json.dumps(val, default=str)
                    if len(val) > 300:
                        val = val[:300] + "..."
                lines.append(f"- **{key}**: {val}")

        # Count remaining fields the ego can explore via MCP
        shown = len([k for k in priority_keys if k in model])
        remaining = len(model) - shown
        if remaining > 0:
            lines.append(
                f"\n*{remaining} more fields available via memory_recall.*"
            )

        lines.append("")
        return "\n".join(lines)

    async def _intentions_section(self, *, depth: str = "deep") -> str:
        """Deferred intentions for review.

        depth is accepted for interface consistency but ignored — intentions
        are in _ALWAYS_SECTIONS and always render at full depth.
        """
        from genesis.ego.intentions_context import build_intentions_section
        return await build_intentions_section(self._db, "user_ego_cycle")

    async def _user_goals_section(self, *, depth: str = "deep") -> str:
        """Active user goals — the bedrock of the world model."""
        lines = ["## User Goals\n"]

        try:
            from genesis.db.crud import user_goals
            goals = await user_goals.list_active(self._db, limit=20)
        except Exception:
            logger.debug("Failed to query user goals", exc_info=True)
            lines.append("*Goal tracking not yet populated.*\n")
            return "\n".join(lines)

        if not goals:
            lines.append(
                "*No goals tracked yet. Goals will be detected from "
                "conversations and stored automatically.*\n"
            )
            return "\n".join(lines)

        if depth == "light":
            from datetime import UTC, datetime
            now = datetime.now(UTC)
            stale = 0
            for g in goals:
                updated_at = g.get("updated_at") or g.get("created_at") or ""
                if updated_at:
                    try:
                        days = (now - datetime.fromisoformat(updated_at)).days
                        if days >= 7:
                            stale += 1
                    except (ValueError, TypeError):
                        pass
            summary = f"{len(goals)} active goals"
            if stale:
                summary += f" ({stale} stale)"
            titles = ", ".join(
                g.get("title", "?")[:40] for g in goals[:3]
            )
            if len(goals) > 3:
                titles += f", +{len(goals) - 3} more"
            return f"## User Goals\n{summary}: {titles}\n"

        from datetime import UTC, datetime

        from genesis.db.crud import ego as ego_crud

        now = datetime.now(UTC)

        # Group goals into hierarchy: top-level, children, orphans
        active_ids = {g["id"] for g in goals}
        top_level = []
        children_by_parent: dict[str, list[dict]] = {}
        for g in goals:
            pid = g.get("parent_goal_id")
            if not pid:
                top_level.append(g)
            elif pid in active_ids:
                children_by_parent.setdefault(pid, []).append(g)
            else:
                # Orphan: parent not in active list — render at top level
                top_level.append(g)

        async def _render_goal(g: dict, prefix: str = "- ") -> None:
            goal_id = g.get("id", "?")
            priority = g.get("priority", "medium")
            title = g.get("title", "?")[:120]
            category = g.get("category", "")
            goal_type = g.get("goal_type", "milestone")
            type_tag = " [continuous]" if goal_type == "continuous" else ""

            # Staleness
            updated_at = g.get("updated_at") or g.get("created_at") or ""
            stale_str = ""
            if updated_at:
                try:
                    updated = datetime.fromisoformat(updated_at)
                    days_since = (now - updated).days
                    if days_since >= 7:
                        stale_str = f" STALE ({days_since}d)"
                except (ValueError, TypeError):
                    pass

            # Latest progress note
            progress_str = ""
            progress_notes_raw = g.get("progress_notes", "[]")
            try:
                import json
                notes = (
                    json.loads(progress_notes_raw)
                    if isinstance(progress_notes_raw, str)
                    else progress_notes_raw
                )
                if notes and isinstance(notes, list):
                    latest = notes[-1]
                    note_text = (
                        latest.get("note", str(latest))
                        if isinstance(latest, dict)
                        else str(latest)
                    )
                    progress_str = f' | Last: "{note_text[:60]}"'
            except (json.JSONDecodeError, TypeError):
                pass

            # Proposal outcome summary
            proposal_str = ""
            try:
                summary = await ego_crud.get_goal_proposal_summary(
                    self._db, goal_id,
                )
                if summary:
                    parts = [
                        f"{count} {status}"
                        for status, count in summary.items()
                    ]
                    proposal_str = f" | Proposals: {', '.join(parts)}"
            except Exception:
                pass

            header = (
                f"{prefix}[{priority.upper()}] **{title}** "
                f"(id={goal_id}, {category}){type_tag}"
            )
            detail_parts = [
                p for p in [stale_str, progress_str, proposal_str] if p
            ]
            if detail_parts:
                detail = "".join(detail_parts).lstrip(" |")
                indent = "  " if prefix == "- " else "    "
                lines.append(f"{header}\n{indent}{detail}")
            else:
                lines.append(header)

        # Render: top-level goals with children indented beneath
        for g in top_level:
            await _render_goal(g, prefix="- ")
            for child in children_by_parent.get(g["id"], []):
                await _render_goal(child, prefix="  ↳ ")

        lines.append("")
        return "\n".join(lines)

    async def _user_directives_section(self, *, depth: str = "deep") -> str:
        """User directives — explicit user instructions for the ego.

        Only rendered if there are active directives. Returns empty string
        otherwise to avoid polluting context with empty sections.
        """
        try:
            from genesis.db.crud import ego as ego_crud

            directives = await ego_crud.list_active_directives(
                self._db, ego_target="user_ego", limit=5,
            )
        except Exception:
            logger.debug("Failed to query ego directives", exc_info=True)
            return ""

        if not directives:
            return ""

        lines = ["## User Directives\n"]
        lines.append(
            "*The user flagged these as important. Factor them into your "
            "thinking — but you decide what to propose.*\n"
        )

        from datetime import UTC, datetime

        now = datetime.now(UTC)
        for d in directives:
            priority = d.get("priority", "normal").upper()
            content = d.get("content", "?")[:200]
            directive_id = d.get("id", "?")
            created_at = d.get("created_at", "")
            # Compute age
            age_str = ""
            if created_at:
                try:
                    created = datetime.fromisoformat(created_at)
                    delta = now - created
                    if delta.days > 0:
                        age_str = f"{delta.days}d ago"
                    else:
                        hours = int(delta.total_seconds() / 3600)
                        age_str = f"{hours}h ago" if hours > 0 else "just now"
                except (ValueError, TypeError):
                    pass
            age_part = f", {age_str}" if age_str else ""
            lines.append(
                f"- [{priority}] {content}\n"
                f"  (id={directive_id}{age_part})"
            )

        lines.append("")
        return "\n".join(lines)

    async def _world_snapshot_section(self, *, depth: str = "deep") -> str:
        """Synthesized view of the user's world — events, contacts, signals.

        Replaces the raw user-world observations section with a structured
        world snapshot that connects goals to events, contacts, and signals.
        """
        if depth == "light":
            try:
                from genesis.ego.world_snapshot import build as build_snapshot
                snapshot = await build_snapshot(self._db)
                rendered = snapshot.render()
                # Count non-empty lines as content proxy
                items = len([ln for ln in rendered.split("\n") if ln.strip()])
                return f"## User's World\n{items} items in world snapshot.\n"
            except Exception:
                return "## User's World\n*Snapshot not available.*\n"

        lines = ["## User's World\n"]

        try:
            from genesis.ego.world_snapshot import build as build_snapshot
            snapshot = await build_snapshot(self._db)
            rendered = snapshot.render()
            lines.append(rendered)
        except Exception:
            logger.debug("Failed to build world snapshot", exc_info=True)
            lines.append("*World snapshot not available.*\n")

        return "\n".join(lines)

    # Signals that track user activity — used to filter awareness tick
    # signals for the user ego's activity pulse section.
    _USER_FACING_SIGNALS = frozenset({
        "conversations_since_reflection",
        "task_completion_quality",
        "recon_findings_pending",
        "stale_pending_items",
        "user_goal_staleness",
        "user_session_pattern",
    })

    # Human-readable interpretations for user-facing signal ranges.
    _SIGNAL_INTERPRETATIONS: dict[str, list[tuple[float, str]]] = {
        "user_goal_staleness": [
            (0.7, "oldest pending goal is significantly stale (14+ days)"),
            (0.3, "oldest pending goal is moderately stale (7-14 days)"),
            (0.0, "pending goals are relatively fresh"),
        ],
        "user_session_pattern": [
            (0.7, "user activity is significantly below their baseline"),
            (0.3, "user activity is somewhat below their baseline"),
            (0.0, "user activity is near their normal pattern"),
        ],
        "stale_pending_items": [
            (0.7, "several pending items are aging (3+ days)"),
            (0.3, "some pending items are aging"),
            (0.0, "pending items are fresh"),
        ],
    }

    @classmethod
    def _interpret_signal(cls, name: str, value: float) -> str | None:
        """Return a human-readable interpretation, or None to skip."""
        thresholds = cls._SIGNAL_INTERPRETATIONS.get(name)
        if thresholds:
            for threshold, text in thresholds:
                if value >= threshold:
                    return text
            return None
        # Default: just show the raw value for known user-facing signals
        if isinstance(value, float):
            return f"{value:.2f}"
        return str(value)

    async def _user_activity_pulse_section(self, *, depth: str = "deep") -> str:
        """User activity signals — interpreted for ego decision-making.

        Queries the latest awareness tick and surfaces user-facing signal
        values as prose. Signals at 0.0 are skipped (nothing noteworthy).
        """
        lines = ["## User Activity Pulse\n"]

        try:
            cursor = await self._db.execute(
                "SELECT signals_json, created_at "
                "FROM awareness_ticks "
                "ORDER BY created_at DESC LIMIT 1"
            )
            row = await cursor.fetchone()
        except Exception:
            logger.error("Failed to query awareness_ticks", exc_info=True)
            lines.append("*No awareness data available.*\n")
            return "\n".join(lines)

        if not row:
            lines.append("*No awareness ticks recorded.*\n")
            return "\n".join(lines)

        signals_json, created_at = row

        if depth == "light":
            return f"## User Activity Pulse\nLatest tick: {created_at[:16]}\n"

        try:
            signals = json.loads(signals_json) if signals_json else {}
        except (json.JSONDecodeError, TypeError):
            signals = {}

        # Normalize list format to dict (same as genesis_context.py)
        if isinstance(signals, list):
            signals = {
                s["name"]: s
                for s in signals
                if isinstance(s, dict) and "name" in s
            }

        # Filter to user-facing signals with non-zero values
        pulse_items: list[str] = []
        for sig_name in sorted(self._USER_FACING_SIGNALS):
            sig_info = signals.get(sig_name)
            if not sig_info:
                continue
            value = sig_info.get("value", 0.0) if isinstance(sig_info, dict) else sig_info
            if not isinstance(value, (int, float)) or value == 0.0:
                continue
            interpretation = self._interpret_signal(sig_name, float(value))
            if interpretation:
                label = sig_name.replace("_", " ").title()
                pulse_items.append(
                    f"- **{label}**: {interpretation} (signal: {value:.2f})"
                )

        if not pulse_items:
            lines.append("*All user activity signals nominal.*\n")
        else:
            ts = created_at[:16] if created_at else "?"
            lines.append(f"*From latest awareness tick ({ts}):*\n")
            lines.extend(pulse_items)

        lines.append("")
        return "\n".join(lines)

    async def _recent_conversations_section(self, *, depth: str = "deep") -> str:
        """What the user has been working on — from cc_sessions."""
        if depth == "light":
            try:
                cursor = await self._db.execute(
                    "SELECT COUNT(*) FROM cc_sessions "
                    "WHERE source_tag = 'foreground' "
                    "AND started_at >= datetime('now', '-48 hours') "
                    "AND topic != ''"
                )
                row = await cursor.fetchone()
                count = row[0] if row else 0
                return f"## Recent Conversations (48h)\n{count} sessions.\n"
            except Exception:
                return "## Recent Conversations (48h)\n*Not available.*\n"

        lines = ["## Recent Conversations (48h)\n"]

        try:
            cursor = await self._db.execute(
                "SELECT topic, source_tag, model, started_at "
                "FROM cc_sessions "
                "WHERE source_tag = 'foreground' "
                "AND started_at >= datetime('now', '-48 hours') "
                "AND topic != '' "
                "ORDER BY started_at DESC "
                "LIMIT 10"
            )
            rows = await cursor.fetchall()
        except Exception:
            logger.error("Failed to query cc_sessions", exc_info=True)
            lines.append("*Could not query recent sessions.*\n")
            return "\n".join(lines)

        if not rows:
            lines.append("*No foreground sessions in last 48h.*\n")
            return "\n".join(lines)

        lines.append(f"**{len(rows)} sessions** (most recent first):\n")
        for topic, _source_tag, model, started_at in rows:
            short_topic = topic[:200] + "..." if len(topic) > 200 else topic
            short_topic = short_topic.replace("\n", " ")
            ts = started_at[:16] if started_at else "?"
            domain = " [Genesis internal]" if _is_genesis_internal(short_topic) else ""
            lines.append(f"- [{ts}] ({model}) {short_topic}{domain}")

        lines.append(
            "\nSessions marked [Genesis internal] are system development work — "
            "the Genesis ego's domain. Focus on user-facing threads only.\n"
        )
        return "\n".join(lines)

    async def _backlog_summary_section(self, *, depth: str = "deep") -> str:
        """Inbox, recon, and pending item backlogs."""
        counts: list[tuple[str, int, str | None]] = []  # (label, count, oldest)

        # Inbox: pending/processing items
        try:
            cursor = await self._db.execute(
                "SELECT COUNT(*), MIN(created_at) FROM inbox_items "
                "WHERE status NOT IN ('completed', 'failed')"
            )
            row = await cursor.fetchone()
            if row and row[0] > 0:
                age = self._days_ago(row[1])
                counts.append(("Inbox", row[0], age))
        except Exception:
            pass  # Table may not exist

        # Recon findings: unresolved
        try:
            cursor = await self._db.execute(
                "SELECT COUNT(*), MIN(created_at) FROM observations "
                "WHERE type = 'finding' AND resolved = 0"
            )
            row = await cursor.fetchone()
            if row and row[0] > 0:
                age = self._days_ago(row[1])
                counts.append(("Recon findings", row[0], age))
        except Exception:
            pass

        # Follow-ups awaiting user input
        try:
            cursor = await self._db.execute(
                "SELECT COUNT(*), MIN(created_at) FROM follow_ups "
                "WHERE status = 'pending' "
                "AND strategy = 'user_input_needed' "
                # Strict user_world: only items genuinely awaiting the USER's
                # input (excludes internal-dev backlog parked under this strategy
                # and any tabled rows).
                "AND domain = 'user_world' AND kind = 'follow_up'"
            )
            row = await cursor.fetchone()
            if row and row[0] > 0:
                age = self._days_ago(row[1])
                counts.append(("Awaiting user input", row[0], age))
        except Exception:
            pass

        if depth == "light":
            if not counts:
                return "## Backlogs\n*All clear.*\n"
            parts = [f"{label}: {count}" for label, count, _ in counts]
            return f"## Backlogs\n{' | '.join(parts)}\n"

        lines = ["## Backlogs\n"]
        if not counts:
            lines.append("*All backlogs clear.*\n")
        else:
            for label, count, oldest in counts:
                age_str = f" (oldest: {oldest})" if oldest else ""
                lines.append(f"- **{label}**: {count} pending{age_str}")

        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _days_ago(iso_timestamp: str | None) -> str | None:
        """Convert ISO timestamp to 'Xd ago' string."""
        if not iso_timestamp:
            return None
        try:
            dt = datetime.fromisoformat(iso_timestamp)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            days = (datetime.now(UTC) - dt).days
            if days == 0:
                return "today"
            if days == 1:
                return "1d ago"
            return f"{days}d ago"
        except (ValueError, TypeError):
            return None

    # Pure-infrastructure keywords — escalations containing these are
    # filtered from the user ego's view.  The genesis ego handles infra;
    # the user ego only sees user-impacting escalations.
    _INFRA_ESCALATION_KEYWORDS = frozenset({
        "cost_unknown", "dream cycle", "deepseek",
        "provider fail", "circuit breaker", "qdrant", "heartbeat",
        "dead letter", "watchdog", "systemd", "memory growth",
    })

    async def _genesis_escalations_section(self, *, depth: str = "deep") -> str:
        """Escalations from the Genesis ego that need user ego attention.

        Pure infrastructure escalations (matching ``_INFRA_ESCALATION_KEYWORDS``)
        are filtered out — the genesis ego owns those.  The user ego only sees
        escalations with potential user impact (outreach failures, dispatch
        issues, etc.).
        """
        lines = ["## Genesis Ego Escalations\n"]

        try:
            cursor = await self._db.execute(
                "SELECT id, source, content, priority, created_at "
                "FROM observations "
                "WHERE resolved = 0 "
                "AND type = 'escalation_to_user_ego' "
                "AND created_at >= datetime('now', '-48 hours') "
                "ORDER BY "
                "  CASE priority "
                "    WHEN 'critical' THEN 0 "
                "    WHEN 'high' THEN 1 "
                "    WHEN 'medium' THEN 2 "
                "    ELSE 3 "
                "  END, "
                "  (retrieved_count > 0), "
                "  created_at DESC "
                "LIMIT 10"
            )
            rows = await cursor.fetchall()
        except Exception:
            logger.error("Failed to query escalations", exc_info=True)
            lines.append("*Could not query escalations.*\n")
            return "\n".join(lines)

        # Filter out pure infrastructure escalations — the genesis ego
        # handles those.  Keep escalations with user-facing impact.
        # (id is r[0] now, so content is r[2].)
        rows = [
            r for r in rows
            if not any(
                kw in r[2].lower() for kw in self._INFRA_ESCALATION_KEYWORDS
            )
        ]

        # Read-receipt (non-fatal): these escalations were pulled into the user
        # ego's context this cycle; already-seen ones demote below unread next
        # cycle. r[0] is the observation id.
        try:
            from genesis.db.crud import observations as _obs_crud

            await _obs_crud.increment_retrieved_batch(self._db, [r[0] for r in rows])
        except Exception:
            logger.debug("Failed to record escalation read-receipts", exc_info=True)

        if not rows:
            lines.append("*No escalations from Genesis ego.*\n")
            return "\n".join(lines)

        if depth == "light":
            crit = sum(1 for r in rows if r[3] == "critical")
            high = sum(1 for r in rows if r[3] == "high")
            parts = [f"{len(rows)} escalations"]
            if crit:
                parts.append(f"{crit} critical")
            if high:
                parts.append(f"{high} high")
            return f"## Genesis Ego Escalations\n{', '.join(parts)}.\n"

        lines.append(f"**{len(rows)} escalations** needing your attention:\n")
        for _id, _source, content, priority, created_at in rows:
            age = self._days_ago(created_at) or "?"
            short = content[:300] + "..." if len(content) > 300 else content
            short = short.replace("\n", " ")
            lines.append(f"- [{age}] [{priority}] {short}")

        lines.append(
            "\nThese are issues the Genesis ego couldn't resolve alone. "
            "Decide: fix it, escalate to user, or defer.\n"
        )
        return "\n".join(lines)

    async def _capabilities_section(self, *, depth: str = "deep") -> str:
        """What Genesis CAN do — prompts stretch thinking."""
        if not self._capabilities:
            return "## Genesis Capabilities\n*No capabilities registered.*\n"

        if depth == "light":
            return (
                f"## Genesis Capabilities\n"
                f"{len(self._capabilities)} capabilities available.\n"
            )

        lines = ["## Genesis Capabilities\n"]

        # Just list capabilities briefly — the ego can explore via MCP
        for name, description in sorted(self._capabilities.items()):
            lines.append(f"- **{name}**: {description}")

        lines.append(
            "\nThink about which capabilities could serve the user "
            "that aren't being used enough.\n"
        )
        return "\n".join(lines)

    async def _follow_ups_section(self, *, depth: str = "deep") -> str:
        """Pending follow-ups the user ego should track."""
        lines = ["## Open Threads\n"]

        try:
            from genesis.db.crud import follow_ups as follow_up_crud

            # Strict user_world: the user (CEO) ego tracks the user's world only.
            # Scoping in SQL (before get_actionable's LIMIT) is what actually
            # surfaces the genuine user items — they're recent, so unscoped they
            # rank past the cap and never appear. Pinned/high-priority internal
            # items intentionally re-home to the cockpit, not the user ego.
            actionable = await follow_up_crud.get_actionable(
                self._db, domain="user_world",
            )
        except Exception:
            logger.error("Failed to query follow-ups", exc_info=True)
            lines.append("*Could not query follow-ups.*\n")
            return "\n".join(lines)

        if not actionable:
            lines.append("*No follow-ups requiring attention.*\n")
            return "\n".join(lines)

        if depth == "light":
            return f"## Open Threads\n{len(actionable)} follow-ups pending.\n"

        # Filter to user-relevant follow-ups (pinned items always shown)
        user_relevant = [
            fu for fu in actionable
            if fu.get("strategy") in ("ego_judgment", "user_input_needed")
            or fu.get("priority") in ("high", "critical")
            or fu.get("pinned")
        ]

        if not user_relevant:
            lines.append(
                f"*{len(actionable)} follow-ups exist but none require "
                f"user ego attention.*\n"
            )
            return "\n".join(lines)

        lines.append(f"**{len(user_relevant)} items** needing attention:\n")
        for fu in user_relevant[:10]:
            fid = fu.get("id", "?")
            status = fu.get("status", "?")
            priority = fu.get("priority", "medium")
            content = fu.get("content", "")[:200]
            content = content.replace("\n", " ")
            strategy = fu.get("strategy", "?")
            blocked = fu.get("blocked_reason", "")

            pin_tag = " [pinned]" if fu.get("pinned") else ""
            line = f"- [id:{fid}] [{priority}/{status}]{pin_tag} **{strategy}**: {content}"
            if blocked:
                line += f" — BLOCKED: {blocked[:100]}"
            lines.append(line)

        lines.append("")
        return "\n".join(lines)

    _NEUTRAL_STATUS = NEUTRAL_STATUS  # single source of truth in ego.types

    async def _proposal_history_section(self, *, depth: str = "deep") -> str:
        """Recent proposal topics with neutral status for context.

        Split into Active and Recently Tried so active proposals are always
        visible. Shows WHAT was proposed and its lifecycle outcome using
        neutral labels (no judgment language). Also shows realist annotations
        when available. No aggregate scores, no user_response text —
        those trigger deference bias.
        """
        if depth == "light":
            try:
                cursor = await self._db.execute(
                    "SELECT COUNT(*) FROM ego_proposals "
                    "WHERE created_at >= datetime('now', '-7 days') "
                    "AND status IN ('pending', 'approved', 'executed') "
                    "AND (ego_source = 'user_ego_cycle' OR ego_source IS NULL)"
                )
                active = (await cursor.fetchone())[0]
                cursor2 = await self._db.execute(
                    "SELECT COUNT(*) FROM ego_proposals "
                    "WHERE created_at >= datetime('now', '-7 days') "
                    "AND status IN ('withdrawn', 'tabled', 'rejected', "
                    "'failed', 'expired') "
                    "AND (ego_source = 'user_ego_cycle' OR ego_source IS NULL)"
                )
                tried = (await cursor2.fetchone())[0]
                return (
                    f"## Proposals\n"
                    f"Active: {active} | Recently tried: {tried}\n"
                )
            except Exception:
                return "## Proposals\n*Not available.*\n"

        lines = ["## Active Proposals\n"]
        table_header = (
            "| Action | Topic | Outcome | Realist |\n"
            "|--------|-------|---------|---------|"
        )

        def _format_row(row) -> str:
            action_type = row["action_type"]
            content = row["content"]
            short = content[:100] + "..." if len(content) > 100 else content
            short = short.replace("\n", " ").replace("|", "/")
            status = self._NEUTRAL_STATUS.get(row["status"], row["status"])
            realist = ""
            if row["realist_verdict"]:
                realist = row["realist_verdict"]
                if row["realist_reasoning"]:
                    reason_short = row["realist_reasoning"][:60]
                    reason_short = reason_short.replace("|", "/")
                    realist = f"{realist}: {reason_short}"
            return f"| {action_type} | {short} | {status} | {realist} |"

        try:
            # Section 1: Active proposals (user ego only)
            cursor = await self._db.execute(
                "SELECT action_type, content, status, realist_verdict, "
                "realist_reasoning, created_at "
                "FROM ego_proposals "
                "WHERE created_at >= datetime('now', '-7 days') "
                "AND status IN ('pending', 'approved', 'executed') "
                "AND (ego_source = 'user_ego_cycle' OR ego_source IS NULL) "
                "ORDER BY created_at DESC "
                "LIMIT 15",
            )
            active_rows = await cursor.fetchall()

            if not active_rows:
                lines.append("*No active proposals.*\n")
            else:
                lines.append(table_header)
                for row in active_rows:
                    lines.append(_format_row(row))
                lines.append("")

            # Section 2: Recently tried (user ego only)
            lines.append("## Recently Tried (do not re-propose)\n")
            cursor2 = await self._db.execute(
                "SELECT action_type, content, status, realist_verdict, "
                "realist_reasoning, created_at "
                "FROM ego_proposals "
                "WHERE created_at >= datetime('now', '-7 days') "
                "AND status IN ('withdrawn', 'tabled', 'rejected', 'failed', 'expired') "
                "AND (ego_source = 'user_ego_cycle' OR ego_source IS NULL) "
                "ORDER BY created_at DESC "
                "LIMIT 10",
            )
            tried_rows = await cursor2.fetchall()

            if not tried_rows:
                lines.append("*No recently tried proposals.*\n")
            else:
                lines.append(table_header)
                for row in tried_rows:
                    lines.append(_format_row(row))
                lines.append("")

        except Exception:
            lines.append("*No proposal history available.*\n")

        return "\n".join(lines)

    @staticmethod
    def _proposal_age(iso_timestamp: str | None) -> str:
        """Short age label for a proposal: '<1h', '4h', '2d', etc."""
        if not iso_timestamp:
            return "?"
        try:
            dt = datetime.fromisoformat(iso_timestamp)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            delta = datetime.now(UTC) - dt
            if delta.days >= 1:
                return f"{delta.days}d"
            hours = int(delta.total_seconds() / 3600)
            if hours >= 1:
                return f"{hours}h"
            return "<1h"
        except (ValueError, TypeError):
            return "?"

    async def _proposal_board_section(self, *, depth: str = "deep") -> str:
        """Board (focus) + queue (pending) + approved + deferred."""
        from genesis.db.crud import ego as ego_crud

        lines: list[str] = []

        # ---- Fetch all pending proposals ----
        try:
            all_pending = await ego_crud.get_pending_queue(self._db, ego_source='user_ego_cycle')
        except Exception:
            logger.error("Failed to query pending proposals", exc_info=True)
            lines.append("## Proposal Board\n")
            lines.append("*Could not query pending proposals.*\n")
            return "\n".join(lines)

        if depth == "light":
            board = sum(1 for p in all_pending if p.get("rank") is not None)
            queue = len(all_pending) - board
            return (
                f"## Proposal Board\n"
                f"{board} on board, {queue} queued, "
                f"{len(all_pending)} total pending.\n"
            )

        board = [p for p in all_pending if p.get("rank") is not None]
        queue = [p for p in all_pending if p.get("rank") is None]

        # ---- Board (ranked focus) ----
        lines.append("## Proposal Board (Focus)\n")
        if board:
            lines.append(
                f"**{len(board)} proposal{'s' if len(board) != 1 else ''}** "
                f"on your board (ranked focus):\n"
            )
            for p in board:
                age = self._proposal_age(p.get("created_at"))
                content = (p.get("content") or "")[:150].replace("\n", " ")
                lines.append(
                    f"- [#{p['rank']}] **{p.get('action_type', '?')}** "
                    f"(id:{p['id']}) [{age}]: {content}"
                )
        else:
            lines.append("*Board is empty — no ranked focus items.*\n")

        # ---- Pending queue (unranked) ----
        if queue:
            lines.append("\n## Pending Queue\n")
            lines.append(
                f"**{len(queue)} more proposal{'s' if len(queue) != 1 else ''}** "
                f"awaiting user decision (unranked):\n"
            )
            for p in queue:
                age = self._proposal_age(p.get("created_at"))
                content = (p.get("content") or "")[:150].replace("\n", " ")
                lines.append(
                    f"- (id:{p['id']}) [{age}] "
                    f"**{p.get('action_type', '?')}**: {content}"
                )

        # Queue health
        total = len(all_pending)
        if total > 0:
            lines.append(f"\nQueue: {total} total pending.")
        if total > 10:
            lines.append(
                "Queue growing — consider tabling items you no longer recommend."
            )

        # ---- Approved proposals ready for execution ----
        try:
            approved = await ego_crud.list_proposals(self._db, status="approved", limit=5, ego_source='user_ego_cycle')
        except Exception:
            approved = []

        if approved:
            lines.append(f"\n**{len(approved)} approved proposals** (ready for execution):\n")
            for p in approved:
                content = (p.get("content") or "")[:150].replace("\n", " ")
                lines.append(
                    f"- **{p.get('action_type', '?')}** (id:{p['id']}): {content}"
                )
            lines.append(
                "\nYou can execute these by outputting execution_briefs "
                "with the proposal_id and dispatch instructions.\n"
            )
        else:
            lines.append("\n*No approved proposals awaiting execution.*\n")

        # ---- Deferred (tabled) proposals ----
        try:
            tabled = await ego_crud.get_tabled(self._db, ego_source='user_ego_cycle')
        except Exception:
            tabled = []

        if tabled:
            shown = tabled[:8]
            lines.append(f"\n**Deferred ({len(tabled)} tabled)**:\n")
            for p in shown:
                content = (p.get("content") or "")[:120].replace("\n", " ")
                lines.append(
                    f"- (id:{p['id']}) **{p.get('action_type', '?')}**: {content}"
                )
            if len(tabled) > 8:
                lines.append(f"- ... and {len(tabled) - 8} more")
            lines.append(
                "\nReview deferred items each cycle. Withdraw if genuinely "
                "stale, resurface to re-board if conditions changed.\n"
            )

        lines.append("")
        return "\n".join(lines)

    async def _execution_outcomes_section(self, *, depth: str = "deep") -> str:
        """Recent outcomes from ego-dispatched background sessions."""
        lines = ["## Recent Execution Outcomes (48h)\n"]

        try:
            cursor = await self._db.execute(
                "SELECT content, priority, created_at FROM observations "
                "WHERE type = 'execution_outcome' AND source = 'ego_dispatch' "
                "AND created_at >= datetime('now', '-48 hours') "
                "ORDER BY created_at DESC LIMIT 10"
            )
            rows = await cursor.fetchall()
        except Exception:
            logger.error("Failed to query execution outcomes", exc_info=True)
            lines.append("*Could not query execution outcomes.*\n")
            return "\n".join(lines)

        if not rows:
            lines.append("*No ego dispatch outcomes in last 48h.*\n")
            return "\n".join(lines)

        if depth == "light":
            return f"## Recent Execution Outcomes (48h)\n{len(rows)} outcomes.\n"

        lines.append(f"**{len(rows)} outcomes**:\n")
        for content, priority, created_at in rows:
            short = (content or "")[:200].replace("\n", " ")
            ts = (created_at or "?")[:16]
            lines.append(f"- [{ts}] [{priority}] {short}")

        lines.append("")
        return "\n".join(lines)

    async def _goal_progress_section(self, *, depth: str = "deep") -> str:
        """Goal pursuit progress — executed proposals grouped by goal."""
        lines = ["## Goal Progress (7d)\n"]

        try:
            cursor = await self._db.execute(
                "SELECT p.goal_id, g.title, p.content, p.status, "
                "  p.user_response, p.created_at "
                "FROM ego_proposals p "
                "JOIN user_goals g ON p.goal_id = g.id "
                "WHERE p.goal_id IS NOT NULL "
                "AND p.status = 'executed' "
                "AND p.created_at >= datetime('now', '-7 days') "
                "ORDER BY p.created_at DESC "
                "LIMIT 15"
            )
            rows = await cursor.fetchall()
        except Exception:
            logger.error("Failed to query goal progress", exc_info=True)
            lines.append("*Could not query goal progress.*\n")
            return "\n".join(lines)

        if not rows:
            lines.append("*No goal-linked proposals executed recently.*\n")
            return "\n".join(lines)

        if depth == "light":
            goal_ids = {r[0] for r in rows}
            return (
                f"## Goal Progress (7d)\n"
                f"{len(rows)} executed proposals across {len(goal_ids)} goals.\n"
            )

        # Group by goal
        by_goal: dict[str, dict] = {}
        for goal_id, title, content, status, response, created in rows:
            by_goal.setdefault(goal_id, {"title": title, "items": []})
            # user_response format: "session:{id}|completed:{summary}" or "|failed:{summary}"
            resp = response or ""
            outcome = "done" if "|completed:" in resp else "failed" if "|failed:" in resp else status
            by_goal[goal_id]["items"].append(
                f"  - [{outcome}] {(content or '')[:100]} ({(created or '')[:10]})"
            )

        for gid, info in by_goal.items():
            lines.append(f"**{info['title']}** (id={gid})")
            lines.extend(info["items"])

        lines.append("")
        return "\n".join(lines)

    async def _goal_deep_dive_section(self, *, depth: str = "deep") -> str:
        """Deep dive on the focused goal — only rendered during goal_review.

        When the focus selector targets a specific goal (via focus_id),
        this section provides the ego with comprehensive context:
        - Full goal metadata
        - Complete progress note history (last 15)
        - All proposals linked to this goal (last 20, all statuses)
        """
        focus_id = getattr(self, "_current_focus_id", None)
        if depth in ("skip", "light") or not focus_id:
            return ""

        try:
            from genesis.db.crud import user_goals
        except ImportError:
            return ""

        try:
            goal = await user_goals.get_by_id(self._db, focus_id)
        except Exception:
            logger.debug("Failed to fetch goal %s for deep dive", focus_id)
            return ""

        if not goal:
            return f"## Goal Deep Dive\n*Goal {focus_id} not found.*\n"

        lines = [f"## Goal Deep Dive: {goal.get('title', '?')}\n"]

        # Full goal metadata
        lines.append(f"- **ID**: {focus_id}")
        lines.append(f"- **Category**: {goal.get('category', '?')}")
        lines.append(f"- **Priority**: {goal.get('priority', '?')}")
        lines.append(f"- **Status**: {goal.get('status', '?')}")
        lines.append(f"- **Timeline**: {goal.get('timeline') or 'None set'}")
        lines.append(f"- **Created**: {(goal.get('created_at') or '?')[:10]}")
        lines.append(f"- **Last Updated**: {(goal.get('updated_at') or '?')[:10]}")
        lines.append(f"- **Confidence**: {goal.get('confidence', '?')}")
        if goal.get("description"):
            lines.append(f"- **Description**: {goal['description'][:300]}")
        lines.append("")

        # Full progress note history (last 15)
        import json as _json

        progress_raw = goal.get("progress_notes", "[]")
        try:
            notes = (
                _json.loads(progress_raw)
                if isinstance(progress_raw, str)
                else progress_raw
            )
        except (_json.JSONDecodeError, TypeError):
            notes = []

        if notes and isinstance(notes, list):
            lines.append("### Progress History\n")
            for note in notes[-15:]:
                if isinstance(note, dict):
                    date = note.get("date", "?")
                    text = note.get("note", str(note))[:200]
                else:
                    date = "?"
                    text = str(note)[:200]
                lines.append(f"- [{date}] {text}")
            if len(notes) > 15:
                lines.append(f"- ... and {len(notes) - 15} earlier entries")
            lines.append("")
        else:
            lines.append("### Progress History\n*No progress notes recorded.*\n")

        # All proposals linked to this goal (last 20, all statuses)
        try:
            cursor = await self._db.execute(
                "SELECT action_type, content, status, rationale, "
                "  created_at, user_response "
                "FROM ego_proposals "
                "WHERE goal_id = ? "
                "ORDER BY created_at DESC "
                "LIMIT 20",
                (focus_id,),
            )
            proposal_rows = await cursor.fetchall()
        except Exception:
            logger.debug("Failed to query proposals for goal %s", focus_id)
            proposal_rows = []

        if proposal_rows:
            from genesis.ego.types import NEUTRAL_STATUS

            lines.append("### Goal-Linked Proposals\n")
            for row in proposal_rows:
                action = row[0] or "?"
                content = (row[1] or "")[:150].replace("\n", " ")
                status = NEUTRAL_STATUS.get(row[2], row[2]) if row[2] else "?"
                created = (row[4] or "")[:10]
                lines.append(f"- [{created}] [{status}] **{action}**: {content}")
            lines.append("")
        else:
            lines.append(
                "### Goal-Linked Proposals\n*No proposals for this goal.*\n"
            )

        # Stuck diagnosis: effort spent (>= N executed proposals) but the goal
        # is still active and hasn't advanced. Prompt the ego to diagnose WHY
        # rather than propose more of the same — framed as a hypothesis, NOT a
        # verdict, and explicitly NOT "just close it" (anti-timidity guardrail).
        # Count via the unbounded per-status summary (NOT the capped display
        # rows above) so this matches the cadence scanner's classification.
        from genesis.db.crud import ego as _ego_crud
        from genesis.ego.types import GOAL_STUCK_EXECUTED_THRESHOLD

        _summary = await _ego_crud.get_goal_proposal_summary(self._db, focus_id)
        executed_count = _summary.get("executed", 0)
        if (
            executed_count >= GOAL_STUCK_EXECUTED_THRESHOLD
            and goal.get("status") == "active"
        ):
            lines.append(
                "### ⚠ Stuck Signal\n"
                f"This goal has **{executed_count} executed proposals** but is "
                "still `active` and hasn't advanced (last updated "
                f"{(goal.get('updated_at') or '?')[:10]}). Effort is being spent "
                "without progress. **Diagnose WHY** before proposing more of the "
                "same: wrong strategy? an unaddressed blocker? does it need "
                "decomposing into subgoals, or a fundamentally different "
                "approach? Do NOT simply recommend closing it to clear this "
                "signal — that avoids the problem rather than solving it.\n"
            )

        # Subgoals: show children if this is a parent goal
        try:
            from genesis.db.crud import user_goals as ug_crud

            children = await ug_crud.list_children(
                self._db, focus_id, include_achieved=True,
            )
            if children:
                lines.append("### Subgoals\n")
                for ch in children:
                    ch_status = ch.get("status", "?")
                    ch_title = (ch.get("title") or "?")[:100]
                    ch_id = ch.get("id", "?")
                    lines.append(
                        f"- [{ch_status}] **{ch_title}** (id={ch_id})"
                    )
                lines.append("")
        except Exception:
            logger.debug("Failed to query subgoals for %s", focus_id)

        # Parent context: if this is a child goal, show parent
        if goal.get("parent_goal_id"):
            try:
                parent = await user_goals.get_by_id(
                    self._db, goal["parent_goal_id"],
                )
                if parent:
                    lines.append(
                        f"### Parent Goal\n"
                        f"**{parent.get('title', '?')}** "
                        f"(id={parent.get('id', '?')}, "
                        f"{parent.get('status', '?')})\n"
                    )
            except Exception:
                pass

        lines.append("")
        return "\n".join(lines)

    async def _capability_performance_section(self, *, depth: str = "deep") -> str:
        """Your track record — domain confidence from multiple data sources.

        Framed as context for better proposals, NOT as a limiter.
        The realist gate handles feasibility; this informs confidence calibration.
        """
        lines = ["## Your Track Record\n"]

        try:
            from genesis.db.crud import capability_map as cap_crud

            entries = await cap_crud.get_all(self._db)
        except Exception:
            return ""

        if not entries:
            lines.append("*No performance data yet.*\n")
            return "\n".join(lines)

        if depth == "light":
            avg_conf = sum(e.get("confidence", 0) for e in entries) / len(entries)
            return (
                f"## Your Track Record\n"
                f"{len(entries)} domains tracked (avg confidence: {avg_conf:.0%}).\n"
            )

        _TREND_ICONS = {"improving": "+", "declining": "-", "stable": "="}

        lines.append("| Domain | Confidence | Trend | Evidence |")
        lines.append("|--------|-----------|-------|----------|")
        for e in entries[:12]:
            domain = e.get("domain", "?")
            conf = e.get("confidence", 0.0)
            trend = e.get("trend", "stable")
            evidence = (e.get("evidence_summary") or "")[:80].replace("|", "/")
            icon = _TREND_ICONS.get(trend, "=")
            lines.append(f"| {domain} | {conf:.0%} | {icon} | {evidence} |")

        lines.append(
            "\nUse this to calibrate confidence on proposals. High-confidence "
            "domains deserve ambitious proposals. Low-confidence domains may "
            "benefit from smaller, incremental actions.\n"
        )
        return "\n".join(lines)


    async def _recurring_patterns_section(self, *, depth: str = "deep") -> str:
        """Detect recurring observation patterns (3+ occurrences in 72h).

        Groups unresolved observations by (type, category) and surfaces
        clusters that may warrant automation or systematic response.
        """
        lines = ["## Recurring Patterns (72h)\n"]

        try:
            # Exclude Genesis-internal observations — infrastructure, system_health,
            # performance, maintenance, security are genesis_ego's jurisdiction.
            # NULL categories pass through (ambiguous, not definitively Genesis-internal).
            # Uses canonical INTERNAL_OBS_TYPES from observations.py for type exclusion.
            from genesis.db.crud.observations import INTERNAL_OBS_TYPES

            _GENESIS_CATEGORIES = (
                "system_health", "infrastructure", "performance",
                "maintenance", "security",
            )
            cat_placeholders = ",".join("?" for _ in _GENESIS_CATEGORIES)
            type_placeholders = ",".join("?" for _ in INTERNAL_OBS_TYPES)
            cursor = await self._db.execute(
                "SELECT type, category, COUNT(*) AS cnt, "
                "  MAX(content) AS sample, MAX(created_at) AS latest "
                "FROM observations "
                "WHERE created_at >= datetime('now', '-3 days') "
                "  AND resolved = 0 "
                "  AND (category IS NULL "
                f"       OR category NOT IN ({cat_placeholders})) "
                f"  AND type NOT IN ({type_placeholders}) "
                "GROUP BY type, category "
                "HAVING cnt >= 3 "
                "ORDER BY cnt DESC "
                "LIMIT 5",
                (*_GENESIS_CATEGORIES, *INTERNAL_OBS_TYPES),
            )
            rows = await cursor.fetchall()
        except Exception:
            logger.error("Failed to query recurring patterns", exc_info=True)
            lines.append("*Could not query patterns.*\n")
            return "\n".join(lines)

        if not rows:
            lines.append("*No recurring patterns detected.*\n")
            return "\n".join(lines)

        if depth == "light":
            return f"## Recurring Patterns (72h)\n{len(rows)} patterns detected.\n"

        lines.append(
            f"**{len(rows)} pattern(s)** appearing 3+ times "
            f"(may warrant automation):\n"
        )
        for obs_type, category, cnt, sample, _latest in rows:
            cat_str = f"/{category}" if category else ""
            short = (sample or "")[:100].replace("\n", " ")
            if len(sample or "") > 100:
                short += "\u2026"
            lines.append(f"- **[{obs_type}{cat_str}]** \u00d7{cnt} \u2014 {short}")

        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _output_contract_section(*, depth: str = "deep") -> str:
        """Remind the ego of its required output format."""
        return (
            "## Output Contract\n\n"
            "You MUST respond with valid JSON matching this structure:\n"
            "```json\n"
            "{\n"
            '  "proposals": [\n'
            "    {\n"
            '      "action_type": "investigate|outreach|maintenance|dispatch|config|recurring_pattern",\n'
            '      "action_category": "category for tracking",\n'
            '      "content": "what you want to do (specific and actionable)",\n'
            '      "rationale": "why this helps the user",\n'
            '      "confidence": 0.85,\n'
            '      "urgency": "low|normal|high|critical",\n'
            '      "alternatives": "what else you considered",\n'
            '      "memory_basis": "non-obvious memory that informed this (optional)"\n'
            "    }\n"
            "  ],\n"
            '  "notifications": [\n'
            "    {\n"
            '      "content": "what to tell the user (informational, no approval needed)",\n'
            '      "urgency": "low|normal|high"\n'
            "    }\n"
            "  ],\n"
            '  "focus_summary": "one-line: what you are focused on for the user",\n'
            '  "resolved_follow_ups": [{"id": "follow_up_id", "resolution": "why resolved"}],\n'
            '  "resolved_directives": [{"id": "directive_id", "resolution": "what you decided"}],\n'
            '  "intentions": {"review": [{"id": "...", "action": "keep|fire|withdraw|renew"}], '
            '"new": [{"content": "...", "trigger_condition": "...", "reasoning": "..."}]},\n'
            '  "goal_assessment": "free-text analysis of the focused goal (goal_review cycles only)",\n'
            '  "goal_status_recommendation": "continue|pause|deprioritize|close (goal_review cycles only)"\n'
            "}\n"
            "```\n\n"
            "If you have nothing to propose, return an empty proposals "
            "array. The focus_summary is always required.\n"
        )
