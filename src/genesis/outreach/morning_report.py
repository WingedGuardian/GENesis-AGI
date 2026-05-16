"""Morning report generator — daily system state synthesis."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from genesis.content.drafter import ContentDrafter
from genesis.content.types import DraftRequest, FormatTarget
from genesis.db.crud.observations import INTERNAL_OBS_TYPES as _INTERNAL_OBS_TYPES_SET
from genesis.outreach.types import OutreachCategory, OutreachRequest

logger = logging.getLogger(__name__)

# Convert to tuple for db.execute() compatibility (requires sequence, not frozenset)
_INTERNAL_OBS_TYPES = tuple(_INTERNAL_OBS_TYPES_SET)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "identity" / "MORNING_REPORT.md"


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
            # 13_morning_report — daily morning report generation. Free-chain.
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

        # 5. Follow-ups (user-actionable + blocked + recently completed)
        try:
            followups = await self._get_follow_ups_summary()
            if followups:
                sections.append(f"## Follow-ups\n{followups}")
        except Exception:
            logger.warning("Morning report: follow-ups unavailable", exc_info=True)

        # 6. Outreach summary (just total count, no self-analysis)
        try:
            engagement = await self._get_engagement_summary()
            sections.append(f"## Outreach (7 days)\n{engagement}")
        except Exception:
            logger.warning("Morning report: engagement summary unavailable", exc_info=True)
            await self._emit_warning("engagement_summary", "Engagement summary section unavailable")

        # 7. Critical Issues (only if WARNING+ alerts are active)
        try:
            critical_issues = await self._get_critical_issues()
            if critical_issues:
                sections.append(f"## Critical Issues\n{critical_issues}")
        except Exception as exc:
            logger.warning("Morning report: critical issues check failed", exc_info=True)
            sections.append(f"## Critical Issues\nFailed to query health alerts: {exc}")

        # 8. What I Noticed (unsurfaced observations worth user attention)
        try:
            noticed = await self._get_observation_insights()
            if noticed:
                sections.append(f"## What I Noticed\n{noticed}")
        except Exception:
            logger.warning("Morning report: observation insights unavailable", exc_info=True)

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
        pending_embed = queues.get('pending_embeddings', 0)
        if pending_embed and pending_embed > 100:
            lines.append(f"- **Embedding queue elevated**: {pending_embed} pending")
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
        placeholders = ",".join("?" for _ in _INTERNAL_OBS_TYPES)
        cursor = await self._db.execute(
            f"SELECT id, priority, type, content FROM observations "
            f"WHERE resolved = 0 AND type NOT IN ({placeholders}) "
            "ORDER BY CASE priority "
            "  WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, "
            "created_at DESC LIMIT 5",
            _INTERNAL_OBS_TYPES,
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
            f"WHERE resolved = 0 AND type IN ({placeholders})",
            _INTERNAL_OBS_TYPES,
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
            "AND created_at > datetime('now', '-24 hours') "
            "ORDER BY section, created_at DESC LIMIT 10"
        )
        rows = await cursor.fetchall()
        if not rows:
            return "No active cognitive state entries (all >24h old — skipped)."
        header = (
            "Note: These are Genesis's INTERNAL state entries. Do NOT present\n"
            "them as action items or quote them. At most, summarize in one line:\n"
            "'Genesis is tracking N internal items.' Skip entirely if nothing\n"
            "requires user awareness.\n"
        )
        entries = "\n".join(f"- [{r[0]}] {r[1][:300]} (as of {r[2]})" for r in rows)
        return header + "\n" + entries

    async def _get_pending_items(self) -> str:
        lines: list[str] = []

        # Message queue items
        cursor = await self._db.execute(
            "SELECT message_type, source, priority, content, created_at "
            "FROM message_queue "
            "WHERE responded_at IS NULL AND expired_at IS NULL "
            "AND content NOT LIKE '%Untitled%' "
            "ORDER BY priority, created_at LIMIT 10"
        )
        rows = await cursor.fetchall()
        for r in rows:
            lines.append(
                f"- [{r[0]}] priority={r[2]}, from={r[1] or '?'}, "
                f"created={r[4]}: {r[3][:200]}"
            )

        # Pending ego proposals (user needs to approve/reject on dashboard)
        try:
            cursor = await self._db.execute(
                "SELECT id, content, urgency, created_at FROM ego_proposals "
                "WHERE status = 'pending' ORDER BY created_at DESC LIMIT 5"
            )
            proposals = await cursor.fetchall()
            if proposals:
                lines.append(
                    f"- {len(proposals)} pending ego proposal(s) "
                    "(approve/reject on dashboard):"
                )
                for p in proposals:
                    lines.append(
                        f"  - {(p[1] or '?')[:150]} "
                        f"(urgency={p[2] or 'normal'})"
                    )
        except Exception:
            logger.warning(
                "Morning report: ego proposals query failed", exc_info=True,
            )

        # Pending approval requests
        try:
            cursor = await self._db.execute(
                "SELECT id, description, created_at FROM approval_requests "
                "WHERE status = 'pending' ORDER BY created_at DESC LIMIT 5"
            )
            approvals = await cursor.fetchall()
            if approvals:
                lines.append(
                    f"- {len(approvals)} pending approval request(s):"
                )
                for a in approvals:
                    lines.append(f"  - {(a[1] or '?')[:150]}")
        except Exception:
            logger.warning(
                "Morning report: approval requests query failed",
                exc_info=True,
            )

        return "\n".join(lines) if lines else "No pending items."

    async def _get_follow_ups_summary(self) -> str | None:
        """Return follow-ups needing user attention + recently completed."""
        from genesis.db.crud import follow_ups

        lines = []

        # User-input-needed items
        user_items = await follow_ups.get_pending(
            self._db, strategy="user_input_needed",
        )
        if user_items:
            lines.append("**Needs your input:**")
            for fu in user_items[:5]:
                lines.append(f"- {fu['content'][:200]}")

        # Blocked/failed items
        blocked = await follow_ups.get_by_status(self._db, "failed")
        blocked += await follow_ups.get_by_status(self._db, "blocked")
        if blocked:
            lines.append("**Blocked/failed:**")
            for fu in blocked[:5]:
                reason = fu.get("blocked_reason", "") or "no reason recorded"
                lines.append(f"- {fu['content'][:150]} — {reason[:100]}")

        # Recently completed (last 24h)
        cursor = await self._db.execute(
            "SELECT content, resolution_notes FROM follow_ups "
            "WHERE status = 'completed' "
            "AND completed_at >= datetime('now', '-24 hours') "
            "ORDER BY completed_at DESC LIMIT 5"
        )
        completed = await cursor.fetchall()
        if completed:
            lines.append("**Completed (24h):**")
            for row in completed:
                lines.append(f"- ✓ {row[0][:200]}")

        return "\n".join(lines) if lines else None

    async def _get_critical_issues(self) -> str | None:
        """Return critical issues text ONLY if WARNING+ alerts are active.

        Returns None (not empty string) when all clear — caller skips the
        section entirely. This is not a standing checklist.
        """
        try:
            from genesis.mcp.health_mcp import _impl_health_alerts

            alerts = await _impl_health_alerts(active_only=True)
            # Filter to genuinely urgent issues only:
            # - CRITICAL/ERROR always included (something is actually down)
            # - WARNING only if NOT a degraded call site (fallback routing
            #   is normal operation, not worth morning-report space)
            critical = []
            for a in alerts:
                severity = a.get("severity", "").upper()
                alert_id = a.get("id", "")
                if (
                    severity in ("ERROR", "CRITICAL")
                    or (severity == "WARNING" and not alert_id.startswith("call_site:"))
                ):
                    critical.append(a)
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

        pending = stats["pending"]

        # Just total count — no engagement self-analysis per guidelines
        lines = [f"- {total} messages sent in last 7 days."]
        if pending:
            lines.append(f"- {pending} awaiting engagement signal.")

        return "\n".join(lines)

    async def _get_observation_insights(self) -> str | None:
        """Surface unsurfaced observations that deserve user attention.

        Returns None if no unsurfaced observations exist. Marks delivered
        observations as surfaced to prevent re-delivery.
        """
        from datetime import UTC, datetime

        from genesis.db.crud.observations import (
            get_unsurfaced,
            increment_retrieved_batch,
            mark_influenced_batch,
            mark_surfaced,
        )

        observations = await get_unsurfaced(
            self._db,
            priority_filter=("critical", "high", "medium"),
            exclude_types=_INTERNAL_OBS_TYPES,
            limit=10,
        )
        if not observations:
            return None

        lines = []
        for obs in observations:
            prio = obs["priority"]
            badge = {"critical": "🔴", "high": "🟠", "medium": "🟡"}.get(prio, "")
            content = obs["content"][:200].replace("\n", " ")
            lines.append(f"- {badge} **{prio}**: {content}")

        # Mark surfaced during assembly (before delivery confirmation).
        # Trade-off: if delivery fails, these observations won't re-surface.
        # Acceptable for v1 — the dashboard can always show them.
        ids = [obs["id"] for obs in observations]
        now = datetime.now(UTC).isoformat()
        await mark_surfaced(self._db, ids, now)
        await increment_retrieved_batch(self._db, ids)
        await mark_influenced_batch(self._db, ids)

        return "\n".join(lines)
