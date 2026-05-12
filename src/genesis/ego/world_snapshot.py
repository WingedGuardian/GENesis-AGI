"""World snapshot — deterministic synthesis of the user's world for the ego.

Assembles goals, upcoming events, active contacts, and user-world signals
into a structured briefing. No LLM calls — pure query + template render.

Called by UserEgoContextBuilder to produce the "User's World" section.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass
class WorldSnapshot:
    """Structured snapshot of the user's world."""

    goals: list[dict] = field(default_factory=list)
    upcoming_events: list[dict] = field(default_factory=list)
    active_contacts: list[dict] = field(default_factory=list)
    user_signals: list[dict] = field(default_factory=list)

    def render(self) -> str:
        """Render the world snapshot as markdown for the ego context."""
        parts: list[str] = []

        # Goals
        if self.goals:
            parts.append("### Active Goals\n")
            for g in self.goals:
                priority_tag = f"[{g['priority'].upper()}]" if g.get("priority") else ""
                timeline = f" — {g['timeline']}" if g.get("timeline") else ""
                cat = g.get("category", "")
                parts.append(
                    f"- {priority_tag} **{g['title'][:120]}** "
                    f"({cat}{timeline})"
                )
                # Latest progress note
                try:
                    notes = json.loads(g.get("progress_notes") or "[]")
                    if notes:
                        latest = notes[-1]
                        parts.append(
                            f"  - Latest: {latest.get('note', '')[:100]} "
                            f"({latest.get('date', '?')})"
                        )
                except (json.JSONDecodeError, TypeError):
                    pass
            parts.append("")

        # Upcoming events
        if self.upcoming_events:
            parts.append("### Upcoming Events\n")
            now = datetime.now(UTC)
            for evt in self.upcoming_events:
                date_str = evt.get("event_date", "")[:10]
                subj = evt.get("subject", "?")
                verb = evt.get("verb", "?")
                obj = evt.get("object", "")

                # Urgency marker
                urgency = ""
                try:
                    evt_date = datetime.fromisoformat(
                        evt.get("event_date", "")
                    )
                    if evt_date.tzinfo is None:
                        evt_date = evt_date.replace(tzinfo=UTC)
                    days_until = (evt_date - now).days
                    if days_until < 0:
                        urgency = " **OVERDUE**"
                    elif days_until <= 2:
                        urgency = " **IMMINENT**"
                    elif days_until <= 7:
                        urgency = " *APPROACHING*"
                except (ValueError, TypeError):
                    pass

                parts.append(
                    f"- [{date_str}] {subj} {verb} {obj}{urgency}"
                )
            parts.append("")

        # Active contacts
        if self.active_contacts:
            parts.append("### Recently Active Contacts\n")
            for c in self.active_contacts[:10]:
                name = c.get("name", "?")
                org = f" ({c['organization']})" if c.get("organization") else ""
                rel = f" — {c['relationship']}" if c.get("relationship") else ""
                mentions = c.get("interaction_count", 1)
                parts.append(
                    f"- **{name}**{org}{rel} ({mentions} mentions)"
                )
            parts.append("")

        # User-world signals (observations)
        if self.user_signals:
            parts.append("### Recent Signals\n")
            for sig in self.user_signals[:10]:
                priority = sig.get("priority", "medium")
                content = sig.get("content", "")[:150]
                content = content.replace("\n", " ")
                parts.append(f"- [{priority}] {content}")
            parts.append("")

        if not parts:
            return "*No world model data yet — goals, events, and contacts " \
                   "will populate as conversations continue.*\n"

        return "\n".join(parts)


async def build(db: aiosqlite.Connection) -> WorldSnapshot:
    """Build a world snapshot from current DB state.

    Queries are defensive — each section degrades independently.
    """
    snapshot = WorldSnapshot()

    # 1. Active goals
    try:
        from genesis.db.crud import user_goals
        snapshot.goals = await user_goals.list_active(db, limit=10)
    except Exception:
        logger.debug("Failed to query goals for world snapshot", exc_info=True)

    # 2. Upcoming user-world events (next 30 days)
    try:
        from genesis.db.crud import memory_events
        snapshot.upcoming_events = await memory_events.upcoming_user_events(
            db, days=30, limit=10,
        )
    except Exception:
        logger.debug("Failed to query events for world snapshot", exc_info=True)

    # 3. Recently active contacts (last 14 days)
    try:
        from genesis.db.crud import user_contacts
        snapshot.active_contacts = await user_contacts.recently_active(
            db, days=14,
        )
    except Exception:
        logger.debug("Failed to query contacts for world snapshot", exc_info=True)

    # 4. User-world observation signals (unresolved, last 7 days)
    try:
        cursor = await db.execute(
            "SELECT source, type, category, content, priority, created_at "
            "FROM observations "
            "WHERE resolved = 0 "
            "AND type IN ('user_signal', 'finding', 'interaction_theme') "
            "AND created_at >= datetime('now', '-7 days') "
            "ORDER BY "
            "  CASE priority "
            "    WHEN 'critical' THEN 0 "
            "    WHEN 'high' THEN 1 "
            "    WHEN 'medium' THEN 2 "
            "    ELSE 3 "
            "  END, "
            "  created_at DESC "
            "LIMIT 10",
        )
        rows = await cursor.fetchall()
        snapshot.user_signals = [
            {
                "source": r[0], "type": r[1], "category": r[2],
                "content": r[3], "priority": r[4], "created_at": r[5],
            }
            for r in rows
        ]
    except Exception:
        logger.debug("Failed to query signals for world snapshot", exc_info=True)

    return snapshot
