"""Genesis ego context builder — assembles Genesis-internal operational briefing.

The Genesis ego (COO) sees only infrastructure and system state. It handles
self-maintenance so the user doesn't have to. When it encounters issues
beyond its scope, it escalates to the user ego via observations.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import aiosqlite

from genesis.ego.user_context import _USER_WORLD_CATEGORIES

logger = logging.getLogger(__name__)


class GenesisEgoContextBuilder:
    """Builds the Genesis-internal operational briefing.

    Reads from:
    - HealthDataService.snapshot() for live subsystem state
    - capabilities.json for registered capabilities
    - awareness_ticks for recent signal values
    - observations for unresolved Genesis-internal items
    - follow_ups for maintenance threads
    - cost_events for budget tracking
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
        """Assemble the full Genesis ego context."""
        sections: list[str] = []
        sections.append("# GENESIS_EGO_CONTEXT — Operations Briefing\n")
        sections.append(
            "*You are Genesis's operations intelligence. "
            "Keep the system healthy so the user doesn't have to.*\n"
        )

        sections.append(await self._system_health_section())
        sections.append(await self._signals_section())
        sections.append(await self._observations_section())
        sections.append(await self._follow_ups_section())
        sections.append(await self._cost_section())
        sections.append(await self._proposal_history_section())
        sections.append(await self._proposal_board_section())
        sections.append(await self._execution_outcomes_section())
        sections.append(self._output_contract_section())

        return "\n".join(sections)

    # -- Section builders --

    async def _system_health_section(self) -> str:
        """Live system health from health_data snapshot."""
        lines = ["## System Health\n"]

        if not self._health_data:
            lines.append("*Health data service not available.*\n")
            return "\n".join(lines)

        try:
            snap = await self._health_data.snapshot()
        except Exception:
            logger.error("Failed to get health snapshot", exc_info=True)
            lines.append("*Health snapshot failed.*\n")
            return "\n".join(lines)

        # Infrastructure
        infra = snap.get("infrastructure", {})
        lines.append("### Infrastructure")
        for key, val in sorted(infra.items()):
            if isinstance(val, dict):
                status = val.get("status", "?")
                latency = val.get("latency_ms")
                detail = f"{status}"
                if latency is not None:
                    detail += f" ({latency:.0f}ms)"
                lines.append(f"- **{key}**: {detail}")
            else:
                lines.append(f"- **{key}**: {val}")

        resilience = snap.get("resilience", "unknown")
        lines.append(f"\n**Composite state**: {resilience}")

        # Queues
        queues = snap.get("queues", {})
        if queues:
            lines.append("\n### Queues")
            deferred = queues.get("deferred_work_queue", {})
            dead_letter = queues.get("dead_letter_queue", {})
            if isinstance(deferred, dict):
                lines.append(
                    f"- Deferred work: {deferred.get('pending', 0)} pending"
                )
            if isinstance(dead_letter, dict):
                lines.append(
                    f"- Dead letter: {dead_letter.get('count', 0)} items"
                )

        # Surplus
        surplus = snap.get("surplus", {})
        if surplus:
            lines.append("\n### Surplus")
            lines.append(
                f"- Queue depth: {surplus.get('queue_depth', 0)}"
            )
            last_dispatch = surplus.get("last_dispatch")
            if last_dispatch:
                lines.append(f"- Last dispatch: {last_dispatch}")

        lines.append("")
        return "\n".join(lines)

    async def _signals_section(self) -> str:
        """Recent awareness loop signal values with trend indicators."""
        lines = ["## Awareness Signals (latest tick)\n"]

        try:
            cursor = await self._db.execute(
                "SELECT signals_json, classified_depth, created_at "
                "FROM awareness_ticks "
                "ORDER BY created_at DESC LIMIT 3"
            )
            rows = await cursor.fetchall()
        except Exception:
            logger.error("Failed to query awareness_ticks", exc_info=True)
            lines.append("*No awareness data available.*\n")
            return "\n".join(lines)

        if not rows:
            lines.append("*No awareness ticks recorded.*\n")
            return "\n".join(lines)

        # Parse signals from the most recent tick (display) and previous
        # tick (trend comparison).
        current_row = rows[0]
        signals_json, depth, created_at = current_row
        lines.append(f"**Last tick**: {created_at} (depth: {depth})\n")

        signals = self._parse_signals_json(signals_json)

        # Build previous tick's signal values for trend comparison
        prev_values: dict[str, float] = {}
        if len(rows) >= 2:
            prev_signals = self._parse_signals_json(rows[1][0])
            for name, info in prev_signals.items():
                if isinstance(info, dict):
                    v = info.get("value")
                    if isinstance(v, (int, float)):
                        prev_values[name] = float(v)

        if signals:
            lines.append("| Signal | Value | Trend | Source |")
            lines.append("|--------|-------|-------|--------|")
            for sig_name, sig_info in sorted(signals.items()):
                if isinstance(sig_info, dict):
                    val = sig_info.get("value", "?")
                    src = sig_info.get("source", "?")
                    trend = self._signal_trend(sig_name, val, prev_values)
                    if isinstance(val, float):
                        val = f"{val:.3f}"
                    lines.append(
                        f"| {sig_name} | {val} | {trend} | {src} |"
                    )
                else:
                    lines.append(f"| {sig_name} | {sig_info} | \u2192 | ? |")

        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _parse_signals_json(signals_json: str | None) -> dict:
        """Parse signals_json (list or dict format) into a name-keyed dict."""
        try:
            signals = json.loads(signals_json) if signals_json else {}
        except (json.JSONDecodeError, TypeError):
            return {}
        if isinstance(signals, list):
            return {
                s["name"]: s
                for s in signals
                if isinstance(s, dict) and "name" in s
            }
        return signals if isinstance(signals, dict) else {}

    @staticmethod
    def _signal_trend(
        name: str,
        current_value: object,
        prev_values: dict[str, float],
    ) -> str:
        """Compute trend arrow: \u2191 (up), \u2193 (down), \u2192 (stable/new)."""
        if not isinstance(current_value, (int, float)):
            return "\u2192"
        curr = float(current_value)
        prev = prev_values.get(name)
        if prev is None:
            return "\u2192"  # New signal, no history
        delta = curr - prev
        if abs(delta) < 0.01:  # Within noise threshold
            return "\u2192"
        return "\u2191" if delta > 0 else "\u2193"

    async def _observations_section(self) -> str:
        """Genesis-internal observations — system issues needing attention."""
        lines = ["## Unresolved Observations (last 48h, max 20)\n"]

        try:
            # Exclude user-world categories and user-only relevance tags.
            # Genesis ego sees: NULL category, :genesis, :both, and anything
            # not in _USER_WORLD_CATEGORIES that isn't :user tagged.
            exclude_placeholders = ",".join("?" for _ in _USER_WORLD_CATEGORIES)
            cursor = await self._db.execute(
                f"SELECT source, type, category, content, priority, created_at "
                f"FROM observations "
                f"WHERE resolved = 0 "
                f"AND created_at >= datetime('now', '-48 hours') "
                f"AND (category IS NULL "
                f"     OR (category NOT IN ({exclude_placeholders}) "
                f"         AND category NOT LIKE '%:user')) "
                f"AND type != 'escalation_to_user_ego' "
                f"ORDER BY "
                f"  CASE priority "
                f"    WHEN 'critical' THEN 0 "
                f"    WHEN 'high' THEN 1 "
                f"    WHEN 'medium' THEN 2 "
                f"    ELSE 3 "
                f"  END, "
                f"  created_at DESC "
                f"LIMIT 20",
                tuple(_USER_WORLD_CATEGORIES),
            )
            rows = await cursor.fetchall()
        except Exception:
            logger.error("Failed to query observations", exc_info=True)
            lines.append("*Could not query observations.*\n")
            return "\n".join(lines)

        if not rows:
            lines.append("*No unresolved Genesis-internal observations.*\n")
            return "\n".join(lines)

        lines.append(f"**{len(rows)} items** (sorted by priority):\n")
        for source, obs_type, category, content, priority, _created_at in rows:
            short = content[:200] + "..." if len(content) > 200 else content
            short = short.replace("\n", " ")
            cat_str = f"/{category}" if category else ""
            lines.append(
                f"- [{priority}] **{source}{cat_str}** ({obs_type}): {short}"
            )

        lines.append("")
        return "\n".join(lines)

    async def _follow_ups_section(self) -> str:
        """Maintenance follow-ups for the Genesis ego."""
        lines = ["## Maintenance Follow-ups\n"]

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

        # Filter to maintenance/infrastructure follow-ups
        genesis_relevant = [
            fu for fu in actionable
            if fu.get("strategy") in ("scheduled_task", "surplus_task")
            or fu.get("source", "").startswith(("sentinel", "guardian", "health"))
        ]

        if not genesis_relevant:
            lines.append(
                f"*{len(actionable)} follow-ups exist but none are "
                f"Genesis-maintenance.*\n"
            )
            return "\n".join(lines)

        lines.append(f"**{len(genesis_relevant)} items**:\n")
        for fu in genesis_relevant[:10]:
            fid = fu.get("id", "?")
            status = fu.get("status", "?")
            priority = fu.get("priority", "medium")
            content = fu.get("content", "")[:200]
            content = content.replace("\n", " ")
            strategy = fu.get("strategy", "?")

            pin_tag = " [pinned]" if fu.get("pinned") else ""
            lines.append(f"- [id:{fid}] [{priority}/{status}]{pin_tag} **{strategy}**: {content}")

        lines.append("")
        return "\n".join(lines)

    async def _cost_section(self) -> str:
        """Daily spend and budget status."""
        lines = ["## Cost Status\n"]

        try:
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            cursor = await self._db.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM cost_events "
                "WHERE created_at >= ?",
                (today,),
            )
            row = await cursor.fetchone()
            daily_spend = row[0] if row else 0.0
        except Exception:
            logger.error("Failed to query cost_events", exc_info=True)
            lines.append("*Cost data unavailable.*\n")
            return "\n".join(lines)

        lines.append(f"- **Today's spend**: ${daily_spend:.4f}")

        try:
            cursor = await self._db.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) FROM ego_cycles "
                "WHERE created_at >= ?",
                (today,),
            )
            row = await cursor.fetchone()
            ego_spend = row[0] if row else 0.0
            lines.append(f"- **Ego spend today**: ${ego_spend:.4f}")
        except Exception:
            pass

        lines.append("")
        return "\n".join(lines)

    async def _proposal_history_section(self) -> str:
        """Recent proposal outcomes for self-calibration."""
        lines = ["## Recent Proposals (last 7 days)\n"]

        try:
            cursor = await self._db.execute(
                "SELECT action_type, action_category, content, status, "
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

        lines.append("| Action | Content | Status | Response |")
        lines.append("|--------|---------|--------|----------|")
        for action_type, _category, content, status, response, _created in rows:
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

    @staticmethod
    def _output_contract_section() -> str:
        """Remind the Genesis ego of its output format.

        Includes the escalation field for issues beyond its scope.
        """
        return (
            "## Output Contract\n\n"
            "You MUST respond with valid JSON matching this structure:\n"
            "```json\n"
            "{\n"
            '  "proposals": [\n'
            "    {\n"
            '      "action_type": "investigate|maintenance|config",\n'
            '      "action_category": "system_health|infrastructure|performance",\n'
            '      "content": "what you want to do",\n'
            '      "rationale": "why",\n'
            '      "confidence": 0.85,\n'
            '      "urgency": "low|normal|high|critical",\n'
            '      "alternatives": "what else you considered"\n'
            "    }\n"
            "  ],\n"
            '  "escalations": [\n'
            "    {\n"
            '      "content": "issue description the user ego should see",\n'
            '      "context": "what you tried, why it needs user ego",\n'
            '      "suggested_action": "what you recommend"\n'
            "    }\n"
            "  ],\n"
            '  "focus_summary": "one-line: what Genesis is focused on",\n'
            '  "follow_ups": ["NEW open thread (not already tracked)"],\n'
            '  "resolved_follow_ups": [{"id": "follow_up_id", "resolution": "why resolved"}]\n'
            "}\n"
            "```\n\n"
            "If you cannot resolve an issue, add it to escalations — "
            "the user ego will see it and decide what the user needs to know.\n\n"
            "No morning_report — that belongs to the user ego.\n"
        )
