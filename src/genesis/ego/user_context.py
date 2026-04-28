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
    2. Recent conversations — what the user is working on
    3. User-world observations — email, inbox, findings
    4. Genesis ego escalations — things Genesis can't resolve alone
    5. Capabilities — what Genesis CAN do but ISN'T doing
    6. Minimal system status — one-line health summary
    7. Open threads — pending follow-ups
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
        sections.append(await self._recent_conversations_section())
        sections.append(await self._user_world_observations_section())
        sections.append(await self._genesis_escalations_section())
        sections.append(await self._capabilities_section())
        sections.append(await self._system_status_section())
        sections.append(await self._follow_ups_section())
        sections.append(await self._proposal_board_section())
        sections.append(await self._execution_outcomes_section())
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
            # Build category filter
            placeholders = ",".join("?" for _ in _USER_WORLD_CATEGORIES)
            cursor = await self._db.execute(
                f"SELECT source, type, category, content, priority, created_at "
                f"FROM observations "
                f"WHERE resolved = 0 "
                f"AND category IN ({placeholders}) "
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
            '      "action_type": "investigate|outreach|maintenance|dispatch|config",\n'
            '      "action_category": "category for tracking",\n'
            '      "content": "what you want to do (specific and actionable)",\n'
            '      "rationale": "why this helps the user",\n'
            '      "confidence": 0.85,\n'
            '      "urgency": "low|normal|high|critical",\n'
            '      "alternatives": "what else you considered"\n'
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
