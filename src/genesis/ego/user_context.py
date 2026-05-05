"""User ego context builder — assembles user-focused operational briefing.

The user ego sees the world through the user's eyes: their conversations,
their interests, their pending work, and what Genesis can do for them.
Genesis-internal details are compressed to a one-line status.

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

logger = logging.getLogger(__name__)

# Observation categories that represent user-world signals
_USER_WORLD_CATEGORIES = frozenset({
    "email_recon", "inbox", "finding", "interest", "interests",
    "contribution", "user_model_delta",
})


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
    7. Minimal system status — one-line health summary
    8. Open threads — pending follow-ups
    """

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        health_data: Any | None = None,
        capabilities: dict[str, str] | None = None,
    ) -> None:
        self._db = db
        self._health_data = health_data
        self._capabilities = capabilities or {}

    async def build(self) -> str:
        """Assemble the full user ego context."""
        sections: list[str] = []
        sections.append("# USER_EGO_CONTEXT — What Does the User Need?\n")
        sections.append(
            "*You are Genesis's user-facing intelligence. "
            "This is your user's world. What would they ask you "
            "to do if they were here right now?*\n"
        )

        sections.append(await self._user_model_section())
        sections.append(await self._user_activity_pulse_section())
        sections.append(await self._recent_conversations_section())
        sections.append(await self._user_world_observations_section())
        sections.append(await self._backlog_summary_section())
        sections.append(await self._genesis_escalations_section())
        sections.append(await self._capabilities_section())
        sections.append(await self._system_status_section())
        sections.append(await self._follow_ups_section())
        sections.append(await self._proposal_history_section())
        sections.append(await self._proposal_board_section())
        sections.append(await self._execution_outcomes_section())
        sections.append(await self._autonomy_readiness_section())
        sections.append(await self._recurring_patterns_section())
        sections.append(self._output_contract_section())

        return "\n".join(sections)

    # -- Section builders --

    async def _user_model_section(self) -> str:
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

    async def _user_activity_pulse_section(self) -> str:
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

    async def _recent_conversations_section(self) -> str:
        """What the user has been working on — from cc_sessions."""
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
            lines.append(f"- [{ts}] ({model}) {short_topic}")

        lines.append(
            "\nThese show what the user is actively working on. "
            "Unfinished threads are opportunities to help.\n"
        )
        return "\n".join(lines)

    async def _user_world_observations_section(self) -> str:
        """External signals — email, inbox, findings."""
        lines = ["## User-World Signals (last 7 days, max 15)\n"]

        try:
            # Build category filter — match exact user-world categories
            # plus composite relevance tags ending in :user or :both
            placeholders = ",".join("?" for _ in _USER_WORLD_CATEGORIES)
            cursor = await self._db.execute(
                f"SELECT source, type, category, content, priority, created_at "
                f"FROM observations "
                f"WHERE resolved = 0 "
                f"AND (category IN ({placeholders}) "
                f"     OR category LIKE '%:user' "
                f"     OR category LIKE '%:both') "
                f"AND created_at >= datetime('now', '-7 days') "
                f"ORDER BY "
                f"  CASE priority "
                f"    WHEN 'critical' THEN 0 "
                f"    WHEN 'high' THEN 1 "
                f"    WHEN 'medium' THEN 2 "
                f"    ELSE 3 "
                f"  END, "
                f"  created_at DESC "
                f"LIMIT 15",
                tuple(_USER_WORLD_CATEGORIES),
            )
            rows = await cursor.fetchall()
        except Exception:
            logger.error("Failed to query user-world observations", exc_info=True)
            lines.append("*Could not query user-world observations.*\n")
            return "\n".join(lines)

        if not rows:
            lines.append("*No user-world observations in last 7 days.*\n")
            return "\n".join(lines)

        lines.append(f"**{len(rows)} signals** (sorted by priority):\n")
        for source, obs_type, category, content, priority, _created_at in rows:
            short = content[:200] + "..." if len(content) > 200 else content
            short = short.replace("\n", " ")
            cat_str = f"/{category}" if category else ""
            lines.append(
                f"- [{priority}] **{source}{cat_str}** ({obs_type}): {short}"
            )

        lines.append("")
        return "\n".join(lines)

    async def _backlog_summary_section(self) -> str:
        """Inbox, recon, and pending item backlogs."""
        lines = ["## Backlogs\n"]

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
                "AND strategy = 'user_input_needed'"
            )
            row = await cursor.fetchone()
            if row and row[0] > 0:
                age = self._days_ago(row[1])
                counts.append(("Awaiting user input", row[0], age))
        except Exception:
            pass

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

    async def _genesis_escalations_section(self) -> str:
        """Escalations from the Genesis ego that need user ego attention."""
        lines = ["## Genesis Ego Escalations\n"]

        try:
            cursor = await self._db.execute(
                "SELECT source, content, priority, created_at "
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
                "  created_at DESC "
                "LIMIT 10"
            )
            rows = await cursor.fetchall()
        except Exception:
            logger.error("Failed to query escalations", exc_info=True)
            lines.append("*Could not query escalations.*\n")
            return "\n".join(lines)

        if not rows:
            lines.append("*No escalations from Genesis ego.*\n")
            return "\n".join(lines)

        lines.append(f"**{len(rows)} escalations** needing your attention:\n")
        for _source, content, priority, _created_at in rows:
            short = content[:300] + "..." if len(content) > 300 else content
            short = short.replace("\n", " ")
            lines.append(f"- [{priority}] {short}")

        lines.append(
            "\nThese are issues the Genesis ego couldn't resolve alone. "
            "Decide: fix it, escalate to user, or defer.\n"
        )
        return "\n".join(lines)

    async def _capabilities_section(self) -> str:
        """What Genesis CAN do — prompts stretch thinking."""
        lines = ["## Genesis Capabilities\n"]

        if not self._capabilities:
            lines.append("*No capabilities registered.*\n")
            return "\n".join(lines)

        # Just list capabilities briefly — the ego can explore via MCP
        for name, description in sorted(self._capabilities.items()):
            lines.append(f"- **{name}**: {description}")

        lines.append(
            "\nThink about which capabilities could serve the user "
            "that aren't being used enough.\n"
        )
        return "\n".join(lines)

    async def _system_status_section(self) -> str:
        """Compressed one-liner — only RED items matter to the user ego."""
        lines = ["## System Status (summary)\n"]

        if not self._health_data:
            lines.append("*Health data not available.*\n")
            return "\n".join(lines)

        try:
            snap = await self._health_data.snapshot()
        except Exception:
            lines.append("*Health snapshot failed.*\n")
            return "\n".join(lines)

        resilience = snap.get("resilience", "unknown")
        infra = snap.get("infrastructure", {})

        # Only surface degraded/down items
        problems = []
        for key, val in sorted(infra.items()):
            if isinstance(val, dict):
                status = val.get("status", "?")
                if status not in ("ok", "healthy"):
                    problems.append(f"{key}: {status}")
            elif isinstance(val, str) and val not in ("ok", "healthy"):
                problems.append(f"{key}: {val}")

        if problems:
            lines.append(f"**State**: {resilience} — issues: {', '.join(problems)}")
        else:
            lines.append(f"**State**: {resilience} — all systems nominal.")

        lines.append(
            "\nGenesis ego handles infrastructure. Only act on system "
            "issues if they directly impact the user.\n"
        )

        # Cost status (brief)
        try:
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            cursor = await self._db.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM cost_events "
                "WHERE created_at >= ?",
                (today,),
            )
            row = await cursor.fetchone()
            daily_spend = row[0] if row else 0.0
            lines.append(f"**Today's spend**: ${daily_spend:.2f}")
        except Exception:
            pass

        lines.append("")
        return "\n".join(lines)

    async def _follow_ups_section(self) -> str:
        """Pending follow-ups the user ego should track."""
        lines = ["## Open Threads\n"]

        try:
            from genesis.db.crud import follow_ups as follow_up_crud

            actionable = await follow_up_crud.get_actionable(self._db)
        except Exception:
            logger.error("Failed to query follow-ups", exc_info=True)
            lines.append("*Could not query follow-ups.*\n")
            return "\n".join(lines)

        if not actionable:
            lines.append("*No follow-ups requiring attention.*\n")
            return "\n".join(lines)

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

    async def _proposal_history_section(self) -> str:
        """Recent proposal outcomes for self-calibration."""
        lines = ["## Recent Proposals (last 7 days)\n"]

        try:
            cursor = await self._db.execute(
                "SELECT action_type, content, status, "
                "user_response, created_at "
                "FROM ego_proposals "
                "WHERE created_at >= datetime('now', '-7 days') "
                "ORDER BY created_at DESC "
                "LIMIT 15"
            )
            rows = await cursor.fetchall()
        except Exception:
            lines.append("*No proposal history available.*\n")
            return "\n".join(lines)

        if not rows:
            lines.append("*No proposals in last 7 days.*\n")
            return "\n".join(lines)

        # Summary line for quick calibration
        from collections import Counter
        status_counts = Counter(r[2] for r in rows)
        parts = [f"{status_counts[s]} {s}" for s in
                 ("approved", "rejected", "executed", "pending", "failed",
                  "expired", "tabled", "withdrawn")
                 if status_counts.get(s)]
        lines.append(f"**{len(rows)} proposals**: {', '.join(parts)}\n")

        lines.append("| Action | Content | Status | Response |")
        lines.append("|--------|---------|--------|----------|")
        for action_type, content, status, response, _created in rows:
            short = content[:80] + "..." if len(content) > 80 else content
            short = short.replace("\n", " ").replace("|", "/")
            resp = (response or "\u2014")[:50]
            lines.append(
                f"| {action_type} | {short} | {status} | {resp} |"
            )

        lines.append("")
        return "\n".join(lines)

    async def _proposal_board_section(self) -> str:
        """Current board state + approved proposals ready for execution."""
        from genesis.db.crud import ego as ego_crud

        lines = ["## Proposal Board\n"]

        try:
            board = await ego_crud.get_board(self._db, board_size=10)
        except Exception:
            logger.error("Failed to query proposal board", exc_info=True)
            lines.append("*Could not query proposal board.*\n")
            return "\n".join(lines)

        if not board:
            lines.append("*Board is empty — no pending proposals.*\n")
        else:
            lines.append(f"**{len(board)} pending proposals** (your active board):\n")
            for p in board:
                rank = p.get("rank")
                rank_str = f"#{rank}" if rank else "unranked"
                content = (p.get("content") or "")[:150]
                content = content.replace("\n", " ")
                lines.append(
                    f"- [{rank_str}] **{p.get('action_type', '?')}** "
                    f"(id:{p['id']}): {content}"
                )

        # Approved proposals ready for execution
        try:
            approved = await ego_crud.list_proposals(self._db, status="approved", limit=5)
        except Exception:
            approved = []

        if approved:
            lines.append(f"\n**{len(approved)} approved proposals** (ready for execution):\n")
            for p in approved:
                content = (p.get("content") or "")[:150]
                content = content.replace("\n", " ")
                lines.append(
                    f"- **{p.get('action_type', '?')}** (id:{p['id']}): {content}"
                )
            lines.append(
                "\nYou can execute these by outputting execution_briefs "
                "with the proposal_id and dispatch instructions.\n"
            )
        else:
            lines.append("\n*No approved proposals awaiting execution.*\n")

        lines.append("")
        return "\n".join(lines)

    async def _execution_outcomes_section(self) -> str:
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

        lines.append(f"**{len(rows)} outcomes**:\n")
        for content, priority, created_at in rows:
            short = (content or "")[:200].replace("\n", " ")
            ts = (created_at or "?")[:16]
            lines.append(f"- [{ts}] [{priority}] {short}")

        lines.append("")
        return "\n".join(lines)

    async def _autonomy_readiness_section(self) -> str:
        """Show autonomy posteriors — informs promotion proposals."""
        from genesis.db.crud import autonomy as autonomy_crud
        from genesis.db.crud.autonomy import bayesian_level, bayesian_posterior

        try:
            states = await autonomy_crud.list_all(self._db)
        except Exception:
            return ""
        if not states:
            return ""

        lines = ["## Autonomy Readiness\n"]
        for row in states:
            cat = row["category"]
            level = row["current_level"]
            earned = row["earned_level"]
            successes = row["total_successes"]
            corrections = row["total_corrections"]
            posterior = bayesian_posterior(successes, corrections)
            target = bayesian_level(successes, corrections)
            status = f"L{level}"
            if earned > level:
                status += f" (earned L{earned})"
            readiness = ""
            if target > level:
                readiness = f" — **ready for L{min(level + 1, target)}** (posterior={posterior:.3f})"
            else:
                readiness = f" (posterior={posterior:.3f})"
            lines.append(f"- {cat}: {status}{readiness} [{successes}S/{corrections}C]")

        lines.append("")
        return "\n".join(lines)

    async def _recurring_patterns_section(self) -> str:
        """Detect recurring observation patterns (3+ occurrences in 72h).

        Groups unresolved observations by (type, category) and surfaces
        clusters that may warrant automation or systematic response.
        """
        lines = ["## Recurring Patterns (72h)\n"]

        try:
            cursor = await self._db.execute(
                "SELECT type, category, COUNT(*) AS cnt, "
                "  MAX(content) AS sample, MAX(created_at) AS latest "
                "FROM observations "
                "WHERE created_at >= datetime('now', '-3 days') "
                "  AND resolved = 0 "
                "GROUP BY type, category "
                "HAVING cnt >= 3 "
                "ORDER BY cnt DESC "
                "LIMIT 5"
            )
            rows = await cursor.fetchall()
        except Exception:
            logger.error("Failed to query recurring patterns", exc_info=True)
            lines.append("*Could not query patterns.*\n")
            return "\n".join(lines)

        if not rows:
            lines.append("*No recurring patterns detected.*\n")
            return "\n".join(lines)

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
    def _output_contract_section() -> str:
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
            '  "focus_summary": "one-line: what you are focused on for the user",\n'
            '  "follow_ups": ["NEW open thread (not already tracked)"],\n'
            '  "resolved_follow_ups": [{"id": "follow_up_id", "resolution": "why resolved"}],\n'
            '  "morning_report": "only if this is a morning trigger"\n'
            "}\n"
            "```\n\n"
            "If you have nothing to propose, return an empty proposals "
            "array. The focus_summary is always required.\n"
        )
