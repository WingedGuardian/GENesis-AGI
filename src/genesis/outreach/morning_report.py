"""Morning report generator — daily system state synthesis."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from genesis.content.drafter import ContentDrafter
from genesis.content.types import DraftRequest, FormatTarget
from genesis.outreach.types import OutreachCategory, OutreachRequest

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "identity" / "MORNING_REPORT.md"

# Observation types that are relevant to the user (vs Genesis-internal telemetry).
_USER_OBS_TYPES = ("finding", "user_model_gap", "pattern", "user_feedback")


class MorningReportGenerator:
    """Synthesizes system state into a daily morning report."""

    def __init__(
        self,
        health_data: object,
        db: aiosqlite.Connection,
        drafter: ContentDrafter,
        *,
        event_bus: object | None = None,
    ) -> None:
        self._health = health_data
        self._db = db
        self._drafter = drafter
        self._event_bus = event_bus

    async def generate(self) -> OutreachRequest:
        context = await self._assemble_context()
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        topic = f"Morning Report — {today}"

        system_prompt = self._load_system_prompt()

        draft = await self._drafter.draft(
            DraftRequest(
                topic=topic,
                context=context,
                target=FormatTarget.GENERIC,
                tone="concise and informative",
                max_length=None,
                system_prompt=system_prompt,
            ),
            call_site_id="13_morning_report",
        )

        return OutreachRequest(
            category=OutreachCategory.DIGEST,
            topic=topic,
            context=draft.content.text,
            salience_score=0.0,
            signal_type="morning_report",
        )

    @staticmethod
    def _load_system_prompt() -> str | None:
        try:
            return _PROMPT_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.warning("MORNING_REPORT.md not found at %s", _PROMPT_PATH)
            return None

    async def _assemble_context(self) -> str:
        sections: list[str] = []

        # 1. System Health
        try:
            health = await self._health.snapshot()
            sections.append(self._format_health(health))
        except Exception as exc:
            sections.append(f"## System Health\nData unavailable: {exc}")
            await self._emit_warning("health_snapshot", "Health snapshot unavailable")

        # 2. Activity (sessions, user-relevant observations)
        try:
            activity = await self._get_activity_summary()
            sections.append(f"## Activity (last 24h)\n{activity}")
        except Exception:
            logger.warning("Morning report: activity summary unavailable", exc_info=True)
            sections.append("## Activity (last 24h)\nNo data")

        # 3. Cognitive State (with audience context for the LLM)
        try:
            cog = await self._get_cognitive_state()
            sections.append(f"## Cognitive State\n{cog}")
        except Exception:
            logger.warning("Morning report: cognitive state unavailable", exc_info=True)
            await self._emit_warning("cognitive_state", "Cognitive state section unavailable")

        # 4. Pending Items (user-actionable only)
        try:
            pending = await self._get_pending_items()
            sections.append(f"## Pending Items\n{pending}")
        except Exception:
            logger.warning("Morning report: pending items unavailable", exc_info=True)
            await self._emit_warning("pending_items", "Pending items section unavailable")

        # 5. Outreach summary (just total count, no self-analysis)
        try:
            engagement = await self._get_engagement_summary()
            sections.append(f"## Outreach (7 days)\n{engagement}")
        except Exception:
            logger.warning("Morning report: engagement summary unavailable", exc_info=True)
            await self._emit_warning("engagement_summary", "Engagement summary section unavailable")

        # 6. Critical Issues (only if WARNING+ alerts are active)
        try:
            critical_issues = await self._get_critical_issues()
            if critical_issues:
                sections.append(f"## Critical Issues\n{critical_issues}")
        except Exception as exc:
            logger.warning("Morning report: critical issues check failed", exc_info=True)
            sections.append(f"## Critical Issues\nFailed to query health alerts: {exc}")

        return "\n\n".join(sections)

    async def _emit_warning(self, section: str, message: str) -> None:
        if not self._event_bus:
            return
        try:
            from genesis.observability.types import Severity, Subsystem

            await self._event_bus.emit(
                Subsystem.OUTREACH, Severity.WARNING,
                "morning_report.section_failed",
                message,
                section=section,
            )
        except Exception:
            pass  # Don't let observability failures break the report

    def _format_health(self, health: dict) -> str:
        cost = health.get("cost", {})
        queues = health.get("queues", {})
        infra = health.get("infrastructure", {})
        surplus = health.get("surplus", {})
        awareness = health.get("awareness", {})
        cc = health.get("cc_sessions", {})
        lines = [
            "## System Health",
            f"- Cost: ${cost.get('daily_usd', 0):.2f} today, ${cost.get('monthly_usd', 0):.2f} month",
            f"- Infrastructure: DB={infra.get('genesis.db', {}).get('status', '?')}, Qdrant={infra.get('qdrant', {}).get('status', '?')}",
            f"- Queues: deferred={queues.get('deferred_work', 0)}, dead_letters={queues.get('dead_letters', 0)}, pending_embeddings={queues.get('pending_embeddings', 0)}",
            f"- Surplus: {surplus.get('status', '?')}, queue_depth={surplus.get('queue_depth', 0)}",
            f"- Awareness: ticks_24h={awareness.get('ticks_24h', '?')}",
            f"- CC Sessions: foreground={cc.get('foreground_active', 0)}, background={cc.get('background_active', 0)}, failed_24h={cc.get('failed_24h', 0)}",
        ]
        return "\n".join(lines)

    async def _get_activity_summary(self) -> str:
        lines = []

        # CC Sessions in last 24h
        cursor = await self._db.execute(
            "SELECT status, COUNT(*) FROM cc_sessions "
            "WHERE started_at >= datetime('now', '-24 hours') "
            "GROUP BY status"
        )
        rows = await cursor.fetchall()
        if rows:
            parts = [f"{r[0]}={r[1]}" for r in rows]
            lines.append(f"- CC sessions: {', '.join(parts)}")
        else:
            lines.append("- CC sessions: none in last 24h")

        # Inbox items — pending count from filesystem, plus DB status breakdown
        inbox_dir = os.path.expanduser("~/inbox")
        pending_fs = len(os.listdir(inbox_dir)) if os.path.isdir(inbox_dir) else 0
        cursor = await self._db.execute(
            "SELECT status, COUNT(*) FROM inbox_items "
            "WHERE created_at >= datetime('now', '-24 hours') "
            "GROUP BY status"
        )
        rows = await cursor.fetchall()
        if rows:
            parts = [f"{r[0]}={r[1]}" for r in rows]
            lines.append(f"- Inbox items: {pending_fs} pending (filesystem), recent: {', '.join(parts)}")
        else:
            lines.append(f"- Inbox items: {pending_fs} pending (filesystem), none processed in last 24h")

        # User-relevant observations with content preview (top 5)
        placeholders = ",".join("?" for _ in _USER_OBS_TYPES)
        cursor = await self._db.execute(
            f"SELECT id, priority, type, content FROM observations "
            f"WHERE resolved = 0 AND type IN ({placeholders}) "
            "ORDER BY CASE priority "
            "  WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, "
            "created_at DESC LIMIT 5",
            _USER_OBS_TYPES,
        )
        rows = await cursor.fetchall()
        if rows:
            lines.append("- User-relevant observations (unresolved):")
            obs_ids = []
            for r in rows:
                obs_ids.append(r[0])
                content_preview = (r[3] or "?")[:120]
                lines.append(f"  - [{r[1]}] {r[2]}: {content_preview}")

            # Track retrieval and influence (displayed to user = influenced awareness)
            try:
                from genesis.db.crud import observations as obs_crud
                await obs_crud.increment_retrieved_batch(self._db, obs_ids)
                await obs_crud.mark_influenced_batch(self._db, obs_ids)
            except Exception:
                logger.warning("Failed to track morning report observation consumption", exc_info=True)

        # Genesis-internal observation count (single summary line)
        cursor = await self._db.execute(
            f"SELECT COUNT(*) FROM observations "
            f"WHERE resolved = 0 AND type NOT IN ({placeholders})",
            _USER_OBS_TYPES,
        )
        row = await cursor.fetchone()
        internal_count = row[0] if row else 0
        if internal_count:
            lines.append(
                f"- Genesis internal: {internal_count} items tracked "
                "(reflections, awareness events, etc. — no user action needed)"
            )

        return "\n".join(lines) if lines else "No activity data."

    async def _get_cognitive_state(self) -> str:
        cursor = await self._db.execute(
            "SELECT section, content, created_at FROM cognitive_state "
            "WHERE (expires_at IS NULL OR expires_at > datetime('now')) "
            "AND created_at > datetime('now', '-48 hours') "
            "ORDER BY section, created_at DESC LIMIT 10"
        )
        rows = await cursor.fetchall()
        if not rows:
            return "No active cognitive state entries."
        header = (
            "Note: These are Genesis's internal state entries. Only surface items\n"
            "that explicitly require user input or awareness. Items Genesis can\n"
            "handle autonomously should be noted as 'Genesis will handle.'\n"
        )
        entries = "\n".join(f"- [{r[0]}] {r[1][:300]} (as of {r[2]})" for r in rows)
        return header + "\n" + entries

    async def _get_pending_items(self) -> str:
        cursor = await self._db.execute(
            "SELECT message_type, source, priority, content, created_at "
            "FROM message_queue "
            "WHERE responded_at IS NULL AND expired_at IS NULL "
            "AND content NOT LIKE '%Untitled%' "
            "ORDER BY priority, created_at LIMIT 10"
        )
        rows = await cursor.fetchall()
        if not rows:
            return "No pending items."
        return "\n".join(
            f"- [{r[0]}] priority={r[2]}, from={r[1] or '?'}, created={r[4]}: {r[3][:200]}"
            for r in rows
        )

    async def _get_critical_issues(self) -> str | None:
        """Return critical issues text ONLY if WARNING+ alerts are active.

        Returns None (not empty string) when all clear — caller skips the
        section entirely. This is not a standing checklist.
        """
        try:
            from genesis.mcp.health_mcp import _impl_health_alerts

            alerts = await _impl_health_alerts(active_only=True)
            critical = [
                a for a in alerts
                if a.get("severity", "").upper() in ("WARNING", "ERROR", "CRITICAL")
            ]
            if not critical:
                return None
            lines = []
            for a in critical:
                lines.append(
                    f"- **{a.get('severity', '?')}**: {a.get('message', 'Unknown')} "
                    f"(id: {a.get('id', '?')})"
                )
            return "\n".join(lines)
        except Exception:
            logger.warning("Failed to query health alerts for morning report", exc_info=True)
            return None

    async def _get_engagement_summary(self) -> str:
        try:
            from genesis.db.crud.outreach import get_engagement_stats
            stats = await get_engagement_stats(self._db, days=7)
        except Exception:
            # Fallback to simple count
            cursor = await self._db.execute(
                "SELECT COUNT(*) FROM outreach_history "
                "WHERE created_at >= datetime('now', '-7 days')"
            )
            row = await cursor.fetchone()
            total = row[0] if row else 0
            return f"- {total} messages sent in last 7 days." if total else "- No outreach in last 7 days."

        total = stats["total"]
        if not total:
            return "- No outreach in last 7 days."

        engaged = stats["engaged"]
        ignored = stats["ignored"]
        ambivalent = stats["ambivalent"]
        pending = stats["pending"]

        lines = [f"- {total} messages sent in last 7 days."]
        if engaged:
            lines.append(f"- {engaged} received a reply ({engaged * 100 // total}% engagement).")
        if ignored:
            lines.append(f"- {ignored} went unread ({ignored * 100 // total}% ignore rate).")
        if ambivalent:
            lines.append(f"- {ambivalent} had implicit activity (user active but no direct reply).")
        if pending:
            lines.append(f"- {pending} awaiting engagement signal.")

        # Note if throttle is active
        if total >= 5 and ignored / total > 0.8:
            lines.append("- **Engagement throttle active**: surplus outreach reduced due to low engagement.")

        return "\n".join(lines)
