"""ContentPipelineModule — CapabilityModule implementation."""

from __future__ import annotations

import logging
from typing import Any

from genesis.modules.content_pipeline.analytics import AnalyticsTracker
from genesis.modules.content_pipeline.idea_bank import IdeaBank
from genesis.modules.content_pipeline.planner import ContentPlanner
from genesis.modules.content_pipeline.publisher import PublishManager
from genesis.modules.content_pipeline.script_engine import ScriptEngine

logger = logging.getLogger(__name__)


class ContentPipelineModule:
    """Content Pipeline — end-to-end content creation workflow.

    Captures ideas from recon/manual/trends, plans content calendars,
    drafts scripts with voice calibration, manages publishing, and
    tracks analytics. Semi-autonomous: Genesis proposes, user approves.
    """

    def __init__(self) -> None:
        self._enabled = False
        self._runtime: Any = None
        self.idea_bank: IdeaBank | None = None
        self.planner: ContentPlanner | None = None
        self.script_engine: ScriptEngine | None = None
        self.publisher: PublishManager | None = None
        self.analytics: AnalyticsTracker | None = None

    @property
    def name(self) -> str:
        return "content_pipeline"

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    async def register(self, runtime: Any) -> None:
        """Register with Genesis runtime and initialize components."""
        self._runtime = runtime

        db = getattr(runtime, "db", None)
        if db is None:
            logger.warning("No database available — content pipeline components not initialized")
            return

        # Ensure all tables exist
        from genesis.modules.content_pipeline import analytics as analytics_mod
        from genesis.modules.content_pipeline import idea_bank as idea_bank_mod
        from genesis.modules.content_pipeline import planner as planner_mod
        from genesis.modules.content_pipeline import publisher as publisher_mod
        from genesis.modules.content_pipeline import script_engine as script_engine_mod

        await idea_bank_mod.ensure_table(db)
        await planner_mod.ensure_table(db)
        await script_engine_mod.ensure_table(db)
        await publisher_mod.ensure_table(db)
        await analytics_mod.ensure_table(db)

        # Get drafter if available
        drafter = getattr(runtime, "content_drafter", None)

        self.idea_bank = IdeaBank(db)
        self.planner = ContentPlanner(db, drafter=drafter)
        self.script_engine = ScriptEngine(db, drafter=drafter)
        self.publisher = PublishManager(db)
        self.analytics = AnalyticsTracker(db)

        logger.info("Content pipeline module registered")

    async def deregister(self) -> None:
        """Clean shutdown."""
        self._runtime = None
        self.idea_bank = None
        self.planner = None
        self.script_engine = None
        self.publisher = None
        self.analytics = None
        logger.info("Content pipeline module deregistered")

    def get_research_profile_name(self) -> str | None:
        return "content-pipeline"

    async def handle_opportunity(self, opportunity: dict) -> dict | None:
        """Capture content-relevant opportunities as ideas.

        Expects opportunity dicts with 'type' indicating relevance.
        Content-relevant types: 'content_idea', 'trend', 'recon_finding'.
        """
        if self.idea_bank is None:
            return None

        opp_type = opportunity.get("type", "")
        content_types = {"content_idea", "trend", "recon_finding"}
        if opp_type not in content_types:
            return None

        content = opportunity.get("content", opportunity.get("summary", ""))
        if not content:
            return None

        source_map = {
            "content_idea": "manual",
            "trend": "trend",
            "recon_finding": "recon",
        }

        idea = await self.idea_bank.capture(
            source=source_map.get(opp_type, "manual"),
            content=content,
            tags=opportunity.get("tags", []),
            platform_target=opportunity.get("platform"),
        )

        return {
            "type": "content_idea_captured",
            "idea_id": idea.id,
            "source": idea.source,
            "content_preview": content[:100],
            "requires_approval": False,
        }

    async def record_outcome(self, outcome: dict) -> None:
        """Record publish metrics from an outcome dict."""
        if self.analytics is None:
            return

        content_id = outcome.get("content_id")
        platform = outcome.get("platform")
        if not content_id or not platform:
            return

        await self.analytics.record_metrics(
            content_id=content_id,
            platform=platform,
            views=outcome.get("views", 0),
            likes=outcome.get("likes", 0),
            shares=outcome.get("shares", 0),
        )

    async def extract_generalizable(self, outcome: dict) -> list[dict] | None:
        """Extract content strategy lessons from outcomes.

        Looks for patterns like: high engagement correlates with specific
        content types, posting times, or platform choices.
        """
        views = outcome.get("views", 0)
        likes = outcome.get("likes", 0)
        shares = outcome.get("shares", 0)
        engagement = views + likes * 5 + shares * 10

        # Only extract lessons from notably good or bad outcomes
        if engagement < 50:
            return None

        lessons = []
        platform = outcome.get("platform", "unknown")
        content_type = outcome.get("content_type", "unknown")

        if shares > likes:
            lessons.append({
                "source": "module:content_pipeline",
                "lesson": f"Content on {platform} ({content_type}) had high share-to-like ratio — likely resonated beyond core audience.",
                "category": "content_strategy",
            })

        if engagement > 200:
            lessons.append({
                "source": "module:content_pipeline",
                "lesson": f"High-engagement content on {platform}: {content_type}. Worth replicating format.",
                "category": "content_strategy",
            })

        return lessons if lessons else None

    def configurable_fields(self) -> list[dict]:
        """Return user-editable configuration fields."""
        return [
            {"name": "engagement_threshold", "label": "Engagement Threshold", "type": "int",
             "value": getattr(self, "_engagement_threshold", 50),
             "description": "Minimum engagement score to extract lessons from outcomes"},
        ]

    def update_config(self, updates: dict) -> dict:
        """Apply configuration updates with bounds validation."""
        if "engagement_threshold" in updates:
            val = int(updates["engagement_threshold"])
            if val < 0:
                raise ValueError("engagement_threshold must be non-negative")
            self._engagement_threshold = val
        return {f["name"]: f["value"] for f in self.configurable_fields()}
