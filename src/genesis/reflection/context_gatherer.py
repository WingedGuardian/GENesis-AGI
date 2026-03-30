"""Context gathering for deep reflection — queries multiple data sources."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.db.crud import (
    cognitive_state,
    cost_events,
    observations,
    surplus,
)
from genesis.reflection.types import (
    ContextBundle,
    CostSummary,
    PendingWorkSummary,
    ProcedureStats,
)

logger = logging.getLogger(__name__)

# Thresholds for "pending work" detection
_MIN_OBSERVATIONS_FOR_CONSOLIDATION = 10
_COGNITIVE_STATE_STALE_HOURS = 24


class ContextGatherer:
    """Assembles comprehensive context for deep reflection from multiple DB sources."""

    def __init__(self, *, budget_daily: float = 2.0, budget_weekly: float = 10.0,
                 budget_monthly: float = 30.0):
        self._budget_daily = budget_daily
        self._budget_weekly = budget_weekly
        self._budget_monthly = budget_monthly

    async def gather(self, db: aiosqlite.Connection) -> ContextBundle:
        """Gather all context needed for a deep reflection invocation."""
        cog_state = await cognitive_state.render(db)
        recent_obs = await self._recent_observations(db)
        proc_stats = await self._procedure_stats(db)
        surplus_items = await surplus.list_pending(db, limit=20)
        cost = await self._cost_summary(db)
        pending = await self.detect_pending_work(db)
        conversations = self._recent_conversation_turns()

        return ContextBundle(
            cognitive_state=cog_state,
            recent_observations=recent_obs,
            procedure_stats=proc_stats,
            surplus_staging_items=surplus_items,
            cost_summary=cost,
            pending_work=pending,
            recent_conversations=conversations,
        )

    async def detect_pending_work(self, db: aiosqlite.Connection) -> PendingWorkSummary:
        """Determine which deep reflection jobs have pending work."""
        now = datetime.now(UTC)

        # Memory consolidation: unresolved observations above threshold
        unresolved = await observations.query(db, resolved=False, limit=200)
        obs_backlog = len(unresolved)
        has_memory_work = obs_backlog >= _MIN_OBSERVATIONS_FOR_CONSOLIDATION

        # Surplus review: pending staging items
        pending_surplus = await surplus.list_pending(db, limit=1)
        surplus_count = len(pending_surplus)
        # Get actual count if any exist
        if surplus_count > 0:
            all_pending = await surplus.list_pending(db, limit=100)
            surplus_count = len(all_pending)

        # Cognitive state staleness
        current_cog = await cognitive_state.get_current(db, "active_context")
        cog_stale = True
        if current_cog and current_cog.get("created_at"):
            try:
                created = datetime.fromisoformat(current_cog["created_at"])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                cog_stale = (now - created) > timedelta(hours=_COGNITIVE_STATE_STALE_HOURS)
            except (ValueError, TypeError):
                cog_stale = True

        # Lessons extraction: always true if there are recent observations
        # (deep reflection should always look for lessons)
        has_lessons = obs_backlog > 0

        # Skill health check — dynamically query for declining skills
        skills_needing_review = 0
        try:
            from genesis.learning.skills.effectiveness import SkillEffectivenessAnalyzer

            analyzer = SkillEffectivenessAnalyzer()
            reports = await analyzer.analyze_all(db)
            skills_needing_review = sum(1 for r in reports if analyzer.needs_review(r))
        except Exception:
            logger.warning("Skill analysis failed in context_gatherer", exc_info=True)

        return PendingWorkSummary(
            memory_consolidation=has_memory_work,
            surplus_review=surplus_count > 0,
            skill_review=skills_needing_review > 0,
            cost_reconciliation=True,  # Always include cost summary
            lessons_extraction=has_lessons,
            cognitive_regeneration=cog_stale,
            observation_backlog=obs_backlog,
            surplus_pending=surplus_count,
            skills_needing_review=skills_needing_review,
        )

    async def gather_for_assessment(self, db: aiosqlite.Connection) -> dict:
        """Gather data for the 6 weekly self-assessment dimensions."""
        now = datetime.now(UTC)
        week_ago = (now - timedelta(days=7)).isoformat()
        two_weeks_ago = (now - timedelta(days=14)).isoformat()

        # Dimension 1: Reflection quality
        # Note: NOT incrementing retrieved_count here — this query is for
        # analysis/metrics, not for feeding content into an LLM prompt.
        # Incrementing would inflate the quality metric this code measures.
        recent_obs = await observations.query(db, source="cc_reflection_deep", limit=50)
        obs_with_retrievals = [o for o in recent_obs if o.get("retrieved_count", 0) > 0]
        obs_with_influence = [o for o in recent_obs if o.get("influenced_action", 0) > 0]

        # Dimension 2: Procedure effectiveness
        proc_stats = await self._procedure_stats(db)

        # Dimension 3: Outreach calibration (sparse in V3)
        # Query outreach_history if available
        outreach_data = await self._outreach_stats(db)

        # Dimension 4: Learning velocity
        # Note: intentionally includes resolved observations — this measures
        # total observation volume for velocity metrics, not current issues.
        this_week_obs = await observations.query(db, limit=200)
        this_week_obs = [o for o in this_week_obs if o.get("created_at", "") >= week_ago]
        last_week_obs_all = await observations.query(db, limit=200)
        last_week_obs = [
            o for o in last_week_obs_all
            if two_weeks_ago <= o.get("created_at", "") < week_ago
        ]

        # Dimension 5: Resource efficiency
        surplus_items = await surplus.list_pending(db, limit=100)
        # Count promoted/discarded this week
        promoted_count = await self._surplus_status_count(db, "promoted", since=week_ago)
        discarded_count = await self._surplus_status_count(db, "discarded", since=week_ago)

        # Dimension 6: Blind spots — topic distribution
        topic_dist = {}
        for obs in this_week_obs:
            cat = obs.get("category") or obs.get("type", "unknown")
            topic_dist[cat] = topic_dist.get(cat, 0) + 1

        return {
            "reflection_quality": {
                "total_observations": len(recent_obs),
                "retrieved_count": len(obs_with_retrievals),
                "influenced_count": len(obs_with_influence),
            },
            "procedure_effectiveness": {
                "total_active": proc_stats.total_active,
                "avg_success_rate": proc_stats.avg_success_rate,
                "low_performers": proc_stats.low_performers[:5],
            },
            "outreach_calibration": outreach_data,
            "engagement_by_category_30d": await self._engagement_by_category(db),
            "learning_velocity": {
                "observations_this_week": len(this_week_obs),
                "observations_last_week": len(last_week_obs),
            },
            "resource_efficiency": {
                "surplus_pending": len(surplus_items),
                "promoted_this_week": promoted_count,
                "discarded_this_week": discarded_count,
            },
            "blind_spots": {
                "topic_distribution": topic_dist,
            },
        }

    async def gather_for_calibration(self, db: aiosqlite.Connection) -> dict:
        """Gather data for weekly quality calibration."""
        proc_stats = await self._procedure_stats(db)
        cost = await self._cost_summary(db)

        # Get recent self-assessment scores for trend comparison
        assessments = await observations.query(
            db, type="self_assessment", limit=4,
        )

        return {
            "procedure_stats": {
                "total_active": proc_stats.total_active,
                "avg_success_rate": proc_stats.avg_success_rate,
                "low_performers": proc_stats.low_performers,
                "quarantined": proc_stats.total_quarantined,
            },
            "cost_summary": {
                "daily_usd": cost.daily_usd,
                "weekly_usd": cost.weekly_usd,
                "monthly_usd": cost.monthly_usd,
            },
            "recent_assessments": [
                {"created_at": a["created_at"], "content": a["content"][:500]}
                for a in assessments
            ],
        }

    # ── Private helpers ───────────────────────────────────────────────

    async def _recent_observations(self, db: aiosqlite.Connection) -> list[dict]:
        """Get observations since last deep reflection (or last 48h)."""
        last_deep = await observations.query(
            db, source="cc_reflection_deep", limit=1,
        )
        if last_deep:
            since = last_deep[0].get("created_at", "")
            all_obs = await observations.query(db, resolved=False, limit=100)
            result = [o for o in all_obs if o.get("created_at", "") > since]
        else:
            # No prior deep reflection — get last 48h
            cutoff = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
            all_obs = await observations.query(db, resolved=False, limit=100)
            result = [o for o in all_obs if o.get("created_at", "") >= cutoff]

        # Hard age cap — no observation older than 48h enters deep reflection
        hard_cutoff = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        result = [o for o in result if o.get("created_at", "") >= hard_cutoff]

        # Track that these observations were retrieved for deep reflection context
        obs_ids = [o["id"] for o in result if o.get("id")]
        if obs_ids:
            try:
                await observations.increment_retrieved_batch(db, obs_ids)
                await observations.mark_influenced_batch(db, obs_ids)
            except Exception:
                logger.warning("Failed to track observation retrieval in deep reflection", exc_info=True)

        return result

    async def _procedure_stats(self, db: aiosqlite.Connection) -> ProcedureStats:
        """Compute aggregate procedure statistics."""
        cursor = await db.execute(
            "SELECT * FROM procedural_memory WHERE deprecated = 0"
        )
        rows = [dict(r) for r in await cursor.fetchall()]

        quarantined_cursor = await db.execute(
            "SELECT COUNT(*) FROM procedural_memory WHERE quarantined = 1"
        )
        quarantined_row = await quarantined_cursor.fetchone()
        quarantined_count = quarantined_row[0] if quarantined_row else 0

        if not rows:
            return ProcedureStats(total_quarantined=quarantined_count)

        active = [r for r in rows if not r.get("quarantined", 0)]
        total_uses = sum(r["success_count"] + r["failure_count"] for r in active)
        total_successes = sum(r["success_count"] for r in active)
        avg_rate = total_successes / total_uses if total_uses > 0 else 0.0

        # Low performers: 3+ uses, <50% success
        low = []
        for r in active:
            uses = r["success_count"] + r["failure_count"]
            if uses >= 3:
                rate = r["success_count"] / uses
                if rate < 0.5:
                    low.append({
                        "id": r["id"],
                        "task_type": r["task_type"],
                        "success_rate": round(rate, 2),
                        "uses": uses,
                    })

        return ProcedureStats(
            total_active=len(active),
            total_quarantined=quarantined_count,
            avg_success_rate=round(avg_rate, 3),
            low_performers=sorted(low, key=lambda x: x["success_rate"]),
        )

    async def _cost_summary(self, db: aiosqlite.Connection) -> CostSummary:
        """Compute cost summary for current day/week/month."""
        now = datetime.now(UTC)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

        daily = await cost_events.sum_cost(db, since=day_start)
        weekly = await cost_events.sum_cost(db, since=week_start)
        monthly = await cost_events.sum_cost(db, since=month_start)

        return CostSummary(
            daily_usd=round(daily, 4),
            weekly_usd=round(weekly, 4),
            monthly_usd=round(monthly, 4),
            daily_budget_pct=round(daily / self._budget_daily, 3) if self._budget_daily > 0 else 0,
            weekly_budget_pct=round(weekly / self._budget_weekly, 3) if self._budget_weekly > 0 else 0,
            monthly_budget_pct=round(monthly / self._budget_monthly, 3) if self._budget_monthly > 0 else 0,
        )

    async def _outreach_stats(self, db: aiosqlite.Connection) -> dict:
        """Query outreach_history for engagement stats."""
        try:
            cursor = await db.execute(
                "SELECT engagement_outcome, COUNT(*) as cnt "
                "FROM outreach_history "
                "WHERE engagement_outcome IS NOT NULL "
                "GROUP BY engagement_outcome"
            )
            rows = await cursor.fetchall()
            return {row[0]: row[1] for row in rows} if rows else {"note": "no outreach data yet"}
        except Exception:
            return {"note": "no outreach data yet"}

    async def _engagement_by_category(self, db: aiosqlite.Connection) -> dict:
        """30-day engagement feedback by category for Deep reflection context."""
        try:
            cursor = await db.execute(
                "SELECT category, engagement_outcome, COUNT(*) as cnt "
                "FROM outreach_history "
                "WHERE engagement_outcome IS NOT NULL "
                "  AND engagement_outcome != 'ignored' "
                "  AND created_at >= strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now', '-30 days') "
                "GROUP BY category, engagement_outcome"
            )
            rows = await cursor.fetchall()
            if not rows:
                return {}
            result: dict[str, dict[str, int]] = {}
            for row in rows:
                cat = row[0]
                outcome = row[1]
                if cat not in result:
                    result[cat] = {}
                result[cat][outcome] = row[2]
            return result
        except Exception:
            return {}

    async def _surplus_status_count(
        self, db: aiosqlite.Connection, status: str, *, since: str
    ) -> int:
        """Count surplus items with a given promotion_status since a date."""
        try:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM surplus_insights "
                "WHERE promotion_status = ? AND created_at >= ?",
                (status, since),
            )
            row = await cursor.fetchone()
            return row[0] if row else 0
        except Exception:
            return 0

    def _recent_conversation_turns(self) -> list[dict]:
        """Extract recent user messages from the active CC session JSONL."""
        import glob
        import json
        import os

        from genesis.observability.health_data import CC_JSONL_DIR

        jsonl_dir = os.path.expanduser(CC_JSONL_DIR)
        try:
            files = sorted(
                glob.glob(f"{jsonl_dir}/*.jsonl"),
                key=os.path.getmtime,
                reverse=True,
            )
        except OSError:
            return []
        if not files:
            return []

        latest = files[0]
        turns: list[dict] = []
        try:
            with open(latest, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                # Read last 200KB; fall back to 500KB if no user messages found
                read_size = 200_000
                f.seek(max(0, size - read_size))
                if size > read_size:
                    f.readline()  # skip partial line
                for line in f:
                    try:
                        d = json.loads(line)
                        if d.get("type") == "user":
                            content = d.get("message", {}).get("content", "")
                            if isinstance(content, str) and content.strip():
                                turns.append({
                                    "text": content[:500],
                                    "timestamp": d.get("timestamp", ""),
                                })
                    except (json.JSONDecodeError, KeyError):
                        continue

                # Retry with larger window if no turns found
                if not turns and size > read_size:
                    turns = []
                    f.seek(max(0, size - 500_000))
                    f.readline()
                    for line in f:
                        try:
                            d = json.loads(line)
                            if d.get("type") == "user":
                                content = d.get("message", {}).get("content", "")
                                if isinstance(content, str) and content.strip():
                                    turns.append({
                                        "text": content[:500],
                                        "timestamp": d.get("timestamp", ""),
                                    })
                        except (json.JSONDecodeError, KeyError):
                            continue
        except OSError:
            logger.debug("Could not read JSONL file %s", latest)
            return []

        return turns[-10:]  # Last 10 user turns
