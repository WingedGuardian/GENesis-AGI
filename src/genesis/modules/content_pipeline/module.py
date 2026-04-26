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

    Sub-features are independently toggleable from the dashboard module
    settings panel. The module starts enabled with all auto-features OFF
    so the user can turn them on individually as they're ready.
    """

    def __init__(self) -> None:
        self._enabled = False
        self._runtime: Any = None
        self.idea_bank: IdeaBank | None = None
        self.planner: ContentPlanner | None = None
        self.script_engine: ScriptEngine | None = None
        self.publisher: PublishManager | None = None
        self.analytics: AnalyticsTracker | None = None

        # Sub-feature toggles (all off by default)
        self._auto_capture_recon: bool = False
        self._auto_capture_trends: bool = False
        self._autonomous_drafting: bool = False
        self._platform_targets: list[str] = ["telegram", "medium", "linkedin"]
        self._engagement_threshold: int = 50

    @property
    def name(self) -> str:
        return "content_pipeline"

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def _drafter(self):
        """Lazy-bind ContentDrafter from runtime.

        The drafter is created during outreach init (after module init),
        so it's not available at register() time. This property resolves
        it on first use.
        """
        if self._runtime is not None:
            return getattr(self._runtime, "content_drafter", None)
        return None

    @property
    def _outreach_pipeline(self):
        """Lazy-bind OutreachPipeline from runtime (created after module init)."""
        if self._runtime is not None:
            return getattr(self._runtime, "_outreach_pipeline", None)
        return None

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

        # Pass drafter=None at register time — ScriptEngine and Planner
        # will use self._drafter (lazy property) when they need it.
        # The drafter is created during outreach init which runs after modules.
        self.idea_bank = IdeaBank(db)
        self.planner = ContentPlanner(db, drafter=None)
        self.script_engine = ScriptEngine(db, drafter=None)
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

    def _inject_drafter(self) -> None:
        """Push the lazy-resolved drafter into ScriptEngine and Planner.

        Called before drafting operations to ensure they have the drafter
        that wasn't available at register time.
        """
        drafter = self._drafter
        if drafter is not None:
            if self.script_engine is not None and self.script_engine._drafter is None:
                self.script_engine._drafter = drafter
            if self.planner is not None and self.planner._drafter is None:
                self.planner._drafter = drafter

    async def handle_opportunity(self, opportunity: dict) -> dict | None:
        """Capture content-relevant opportunities as ideas.

        Gated by sub-feature toggles: auto_capture_recon and
        auto_capture_trends must be enabled for their respective signal
        types. Manual content_idea signals always pass through.
        """
        if self.idea_bank is None:
            return None

        opp_type = opportunity.get("type", "")

        # Gate by sub-feature toggles
        if opp_type == "recon_finding" and not self._auto_capture_recon:
            return None
        if opp_type == "trend" and not self._auto_capture_trends:
            return None
        if opp_type not in {"content_idea", "trend", "recon_finding"}:
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

        if engagement < self._engagement_threshold:
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
        """Return user-editable configuration fields for the dashboard."""
        return [
            {"name": "auto_capture_recon", "label": "Auto-Capture Recon", "type": "bool",
             "value": self._auto_capture_recon, "default": False,
             "description": "Automatically capture recon findings as content ideas"},
            {"name": "auto_capture_trends", "label": "Auto-Capture Trends", "type": "bool",
             "value": self._auto_capture_trends, "default": False,
             "description": "Automatically capture trend signals as content ideas"},
            {"name": "autonomous_drafting", "label": "Autonomous Drafting", "type": "bool",
             "value": self._autonomous_drafting, "default": False,
             "description": "Auto-draft scripts from ideas without explicit request"},
            {"name": "platform_targets", "label": "Platform Targets", "type": "list",
             "value": self._platform_targets, "default": ["telegram", "medium", "linkedin"],
             "description": "Platforms to generate content for (telegram, linkedin, reddit, etc.)"},
            {"name": "engagement_threshold", "label": "Engagement Threshold", "type": "int",
             "value": self._engagement_threshold, "default": 50, "min": 0,
             "description": "Minimum engagement score to extract lessons from outcomes"},
        ]

    def update_config(self, updates: dict) -> dict:
        """Apply configuration updates with type validation."""
        if "auto_capture_recon" in updates:
            self._auto_capture_recon = bool(updates["auto_capture_recon"])
        if "auto_capture_trends" in updates:
            self._auto_capture_trends = bool(updates["auto_capture_trends"])
        if "autonomous_drafting" in updates:
            self._autonomous_drafting = bool(updates["autonomous_drafting"])
        if "platform_targets" in updates:
            val = updates["platform_targets"]
            if not isinstance(val, list):
                raise TypeError("platform_targets must be a list")
            self._platform_targets = [str(t) for t in val]
        if "engagement_threshold" in updates:
            val = int(updates["engagement_threshold"])
            if val < 0:
                raise ValueError("engagement_threshold must be non-negative")
            self._engagement_threshold = val
        return {f["name"]: f["value"] for f in self.configurable_fields()}
